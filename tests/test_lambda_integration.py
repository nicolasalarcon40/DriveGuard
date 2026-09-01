"""
Test de integración end-to-end del pipeline completo:

    LocalObjectStore ("S3")  --evento-->  lambda_function.handler
        --> detección de riesgo --> Postgres ("RDS")
        --> LocalKeyValueStore ("DynamoDB")
        --> LocalAlertPublisher ("SNS")

A diferencia de tests/test_risk_rules.py y tests/test_storage.py (unitarios,
sin dependencias externas), este test SÍ necesita una base de datos Postgres
real con el esquema de src/db/schema.sql ya aplicado — por eso se salta
automáticamente (`pytest.skip`) si no encuentra una en DATABASE_URL. Esto
permite que `pytest` corra completo en cualquier laptop sin Postgres
instalado, y que el pipeline completo SÍ se valide en CI (ver
.github/workflows/ci.yml, que levanta un servicio de Postgres justo para
este test) y en local si tienes Postgres arriba (ver README).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from src.common import db
from src.common.config import settings
from src.common.storage import LocalObjectStore
from src.processing.lambda_function import handler

TEST_DRIVER_ID = "DRV-TEST"
TEST_BUCKET = "test-bucket"


def _pg_available() -> bool:
    try:
        conn = psycopg2.connect(settings.database_url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="Requiere Postgres en DATABASE_URL con el esquema aplicado (ver src/db/schema.sql).",
)


@pytest.fixture(autouse=True)
def _clean_test_driver():
    """Aísla el test: borra cualquier rastro de DRV-TEST antes y después,
    para que corra igual de bien en una DB de CI recién creada que en la
    DB local de desarrollo (que puede tener datos de otras corridas)."""
    def _delete():
        conn = psycopg2.connect(settings.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM alerts WHERE driver_id = %s", (TEST_DRIVER_ID,))
                cur.execute("DELETE FROM risk_events WHERE driver_id = %s", (TEST_DRIVER_ID,))
                cur.execute("DELETE FROM driver_risk_scores WHERE driver_id = %s", (TEST_DRIVER_ID,))
                cur.execute("DELETE FROM trips WHERE driver_id = %s", (TEST_DRIVER_ID,))
                cur.execute("DELETE FROM drivers WHERE driver_id = %s", (TEST_DRIVER_ID,))
            conn.commit()
        finally:
            conn.close()

    _delete()
    yield
    _delete()


def _build_trip(trip_id: str, n_harsh_braking_events: int) -> dict:
    """Construye un viaje sintético con exactamente `n_harsh_braking_events`
    frenadas bruscas: cada evento es una caída de 20 km/h en un segundo
    (> HARSH_BRAKING_THRESHOLD_KMH_S), seguida de una recuperación gradual
    en pasos de 8 km/h/s (< AGGRESSIVE_ACCEL_THRESHOLD_KMH_S) para no
    disparar además eventos de aceleración agresiva."""
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    samples = []
    speed = 80.0

    def add_sample(t_idx: int, spd: float):
        t = start + timedelta(seconds=t_idx)
        samples.append({
            "timestamp": t.isoformat(), "speed_kmh": spd, "rpm": 1800,
            "engine_temp_c": 90.0, "lat": -33.45, "lon": -70.66,
        })

    add_sample(0, speed)
    t_idx = 1
    for _ in range(n_harsh_braking_events):
        speed -= 20.0
        add_sample(t_idx, speed)
        t_idx += 1
        while speed < 80.0 - 1e-6:
            speed = min(80.0, speed + 8.0)
            add_sample(t_idx, speed)
            t_idx += 1

    return {
        "trip_id": trip_id,
        "driver_id": TEST_DRIVER_ID,
        "driver_name": "Conductor de Prueba",
        "truck_id": "TRK-TEST",
        "source_type": "simulated",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(seconds=len(samples))).isoformat(),
        "sample_rate_hz": 1,
        "samples": samples,
    }


def test_get_truck_returns_native_numeric_types_not_decimal():
    """Regresión de un bug real encontrado en producción: psycopg2 devuelve
    las columnas NUMERIC como decimal.Decimal, que no se puede dividir con
    un float de Python (TypeError). risk_rules.py opera con floats
    nativos, así que get_truck() debe normalizar el tipo antes de devolver
    el perfil del vehículo. El bug pasó desapercibido en los tests
    unitarios de risk_rules.py porque ahí siempre se pasan floats a mano;
    solo se manifestaba leyendo un vehículo real desde Postgres."""
    conn = psycopg2.connect(settings.database_url)
    try:
        vehicle = db.get_truck(conn, "TRK-01")
    finally:
        conn.close()

    assert vehicle is not None
    assert isinstance(vehicle["max_rpm_normal"], int)
    assert isinstance(vehicle["max_speed_kmh"], float)
    assert isinstance(vehicle["rpm_per_kmh_min"], float)
    assert isinstance(vehicle["rpm_per_kmh_max"], float)
    # Esto es justo lo que provocaba el TypeError en producción.
    _ = 120.0 / vehicle["max_speed_kmh"]


def test_pipeline_persists_trip_and_detected_events_to_postgres():
    trip = _build_trip("TRP-TEST-0001", n_harsh_braking_events=3)
    key = f"trips/{TEST_DRIVER_ID}/2026-08-19/{trip['trip_id']}.json"

    store = LocalObjectStore(settings.s3_bucket)
    store.put_object(key, json.dumps(trip).encode())

    event = {"Records": [{"s3": {"bucket": {"name": settings.s3_bucket}, "object": {"key": key}}}]}
    result = handler(event, context=None)

    assert result["statusCode"] == 200
    assert result["processed"] == 1

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT driver_id, truck_id FROM trips WHERE trip_id = %s", (trip["trip_id"],))
            row = cur.fetchone()
            assert row == (TEST_DRIVER_ID, "TRK-TEST")

            cur.execute(
                "SELECT count(*) FROM risk_events WHERE trip_id = %s AND event_type = 'harsh_braking'",
                (trip["trip_id"],),
            )
            assert cur.fetchone()[0] == 3

            cur.execute(
                "SELECT risk_score FROM driver_risk_scores WHERE driver_id = %s",
                (TEST_DRIVER_ID,),
            )
            score_row = cur.fetchone()
            assert score_row is not None
            assert float(score_row[0]) > 0
    finally:
        conn.close()


def test_alert_fires_exactly_once_when_threshold_is_crossed_across_trips():
    """Regresión del bug de 'alert spam' encontrado durante el desarrollo:
    la alerta debe dispararse UNA sola vez al cruzar el umbral, no en cada
    viaje subsiguiente que el conductor siga por encima."""
    store = LocalObjectStore(settings.s3_bucket)

    # Cada viaje trae eventos suficientes para ir empujando el puntaje
    # acumulado del período hacia arriba del umbral configurado.
    for i in range(4):
        trip = _build_trip(f"TRP-TEST-ALERT-{i}", n_harsh_braking_events=6)
        key = f"trips/{TEST_DRIVER_ID}/2026-08-19/{trip['trip_id']}.json"
        store.put_object(key, json.dumps(trip).encode())
        event = {"Records": [{"s3": {"bucket": {"name": settings.s3_bucket}, "object": {"key": key}}}]}
        handler(event, context=None)

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM alerts WHERE driver_id = %s", (TEST_DRIVER_ID,))
            alert_count = cur.fetchone()[0]
    finally:
        conn.close()

    assert alert_count <= 1, (
        "Se disparó más de una alerta para el mismo cruce de umbral en el período "
        f"(se esperaba a lo más 1, se encontraron {alert_count})"
    )
