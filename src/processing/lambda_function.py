"""
Handler de procesamiento de viajes — el "cerebro" del pipeline.

Se empaqueta tal cual (ver infra/main.tf -> aws_lambda_function.risk_processor)
para desplegarse como una AWS Lambda real, disparada por eventos de S3
("s3:ObjectCreated:*"). En el entorno de desarrollo sin Docker de este
repo, el mismo `handler()` se invoca directamente desde
src/ingestion/upload_to_s3.py con un evento S3 sintético idéntico en forma
al que enviaría AWS — por eso esta función NUNCA debe asumir nada sobre
quién la invocó, solo sobre la forma del evento.

Flujo:
    1. Lee el bucket/key del evento S3.
    2. Descarga y parsea el JSON del viaje.
    3. Calcula estadísticas del viaje (distancia, velocidad promedio, etc.)
    4. Detecta eventos de riesgo (src/processing/risk_rules.py).
    5. Persiste el viaje y los eventos en Postgres ("RDS").
    6. Actualiza el puntaje de riesgo "actual" del conductor en DynamoDB
       (lectura rápida, ej. para una futura app móvil).
    7. Si el puntaje supera el umbral, publica una alerta en SNS.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from src.common import db
from src.common.storage import get_object_store, get_kv_store, get_alert_publisher
from src.common.config import settings
from src.processing.risk_rules import (
    detect_events, compute_risk_score, compute_score_from_counts, summarize_events,
)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _trip_stats(trip: dict) -> dict:
    samples = trip["samples"]
    speeds = [s["speed_kmh"] for s in samples if s.get("speed_kmh") is not None]
    rpms = [s["rpm"] for s in samples if s.get("rpm") is not None]
    temps = [s["engine_temp_c"] for s in samples if s.get("engine_temp_c") is not None]

    distance_km = 0.0
    prev = None
    for s in samples:
        if s.get("lat") is not None and s.get("lon") is not None:
            if prev is not None:
                distance_km += _haversine_km(prev["lat"], prev["lon"], s["lat"], s["lon"])
            prev = s
    if distance_km == 0.0 and speeds:
        # sin GPS (típico en captura OBD2 real): estima por velocidad * tiempo
        hz = trip.get("sample_rate_hz", 1) or 1
        distance_km = sum(speeds) / hz / 3600

    return {
        "distance_km": round(distance_km, 2),
        "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
        "max_rpm": max(rpms) if rpms else None,
        "max_engine_temp": max(temps) if temps else None,
    }


def _publish_alert_if_needed(driver_id: str, driver_name: str, risk_score: float, conn):
    if risk_score < settings.risk_alert_threshold:
        return

    message = (
        f"ALERTA DE RIESGO: el conductor {driver_name} ({driver_id}) "
        f"alcanzó un puntaje de riesgo de {risk_score}/100 "
        f"(umbral configurado: {settings.risk_alert_threshold})."
    )

    sns_message_id = None
    try:
        publisher = get_alert_publisher()
        sns_message_id = publisher.publish("Alerta de conducción riesgosa", message)
    except Exception as exc:  # pragma: no cover - no debe tumbar el procesamiento
        print(f"[Alertas] No se pudo publicar la alerta: {exc}")

    db.insert_alert(conn, driver_id, risk_score, settings.risk_alert_threshold, message, sns_message_id)


def _update_current_score(driver_id: str, risk_score: float, counts: dict):
    try:
        kv = get_kv_store()
        kv.put_item(driver_id, {
            "risk_score": str(risk_score),
            "total_events": counts["total_events"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:  # pragma: no cover
        print(f"[KV store] No se pudo actualizar el puntaje actual: {exc}")


def process_trip_object(bucket: str, key: str) -> dict:
    store = get_object_store()
    raw = store.get_object(key)
    trip = json.loads(raw)
    stats = _trip_stats(trip)

    with db.get_connection() as conn:
        # Umbrales propios del vehículo (RPM/velocidad/marcha) — ver la
        # tabla `trucks` y risk_rules.DEFAULT_VEHICLE. Si el truck_id no
        # está en el catálogo, get_truck() devuelve None y detect_events()
        # cae a los valores genéricos, sin romper el procesamiento.
        vehicle = db.get_truck(conn, trip["truck_id"])
        events = detect_events(trip["samples"], vehicle=vehicle)
        trip_risk_score = compute_risk_score(events)
        trip_counts = summarize_events(events)

        db.ensure_driver(conn, trip["driver_id"], trip.get("driver_name", trip["driver_id"]), trip["truck_id"])
        db.insert_trip(conn, trip, key, stats)
        db.insert_risk_events(conn, trip["trip_id"], trip["driver_id"], events)

        # El puntaje que dispara la alerta es el ACUMULADO del período
        # (hoy), no solo el de este viaje — así se respeta el requisito
        # del proyecto: "si un conductor supera cierto umbral de eventos
        # riesgosos en un período, se dispara una alerta automática".
        period_start, period_end = db.current_period_bounds()
        previous_counts = db.get_driver_period_counts(conn, trip["driver_id"], period_start, period_end)
        previous_period_score = compute_score_from_counts(previous_counts)
        merged_counts = {k: previous_counts.get(k, 0) + trip_counts[k] for k in trip_counts}
        period_risk_score = compute_score_from_counts(merged_counts)

        db.set_driver_period_score(conn, trip["driver_id"], period_start, period_end, merged_counts, period_risk_score)

        # Alerta "edge-triggered": solo se dispara la PRIMERA vez que se
        # cruza el umbral dentro del período, no en cada viaje subsiguiente
        # que el conductor siga por encima (evita fatiga de alertas).
        just_crossed_threshold = (
            previous_period_score < settings.risk_alert_threshold <= period_risk_score
        )
        if just_crossed_threshold:
            _publish_alert_if_needed(trip["driver_id"], trip.get("driver_name", trip["driver_id"]), period_risk_score, conn)

    _update_current_score(trip["driver_id"], period_risk_score, merged_counts)

    result = {
        "trip_id": trip["trip_id"],
        "driver_id": trip["driver_id"],
        "trip_risk_score": trip_risk_score,
        "period_risk_score": period_risk_score,
        **trip_counts,
        **stats,
    }
    print(f"[procesado] {result}")
    return result


def handler(event, context):
    """Entry point compatible con AWS Lambda: handler(event, context)."""
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        results.append(process_trip_object(bucket, key))

    return {"statusCode": 200, "processed": len(results), "results": results}
