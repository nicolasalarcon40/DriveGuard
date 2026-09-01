"""
Capa de acceso a datos (Postgres / "RDS"). Usada tanto por la Lambda de
procesamiento como por el dashboard de Streamlit.

Se usa psycopg2 directo (no un ORM) a propósito: en un proyecto de datos es
más fácil de explicar y depurar en una entrevista técnica, y evita traer
una capa de abstracción innecesaria para algo tan sencillo como esto.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

from src.common.config import settings


@contextmanager
def get_connection():
    conn = psycopg2.connect(settings.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_truck(conn, truck_id: str) -> dict | None:
    """Perfil de umbrales del vehículo (ver tabla `trucks`). Si el
    truck_id no está registrado en el catálogo (datos de prueba, o un
    vehículo nuevo que aún no se cargó), devuelve None — risk_rules.
    detect_events() cae a sus valores por defecto en ese caso, así el
    pipeline nunca se cae por un dato de catálogo faltante."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT max_rpm_normal, max_speed_kmh, rpm_per_kmh_min, rpm_per_kmh_max
            FROM trucks WHERE truck_id = %s
            """,
            (truck_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def ensure_driver(conn, driver_id: str, driver_name: str, truck_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO drivers (driver_id, full_name, truck_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (driver_id) DO NOTHING
            """,
            (driver_id, driver_name, truck_id),
        )


def insert_trip(conn, trip: dict, source_file_key: str, stats: dict):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trips (
                trip_id, driver_id, truck_id, source_file, source_type,
                start_time, end_time, distance_km, avg_speed_kmh,
                max_rpm, max_engine_temp, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (trip_id) DO UPDATE SET processed_at = now()
            """,
            (
                trip["trip_id"], trip["driver_id"], trip["truck_id"],
                source_file_key, trip.get("source_type", "simulated"),
                trip["start_time"], trip.get("end_time"),
                stats.get("distance_km"), stats.get("avg_speed_kmh"),
                stats.get("max_rpm"), stats.get("max_engine_temp"),
            ),
        )


def insert_risk_events(conn, trip_id: str, driver_id: str, events: list[dict]):
    if not events:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO risk_events (trip_id, driver_id, event_type, severity, event_time, value, threshold, details)
            VALUES %s
            """,
            [
                (
                    trip_id, driver_id, e["event_type"], e["severity"], e["event_time"],
                    e["value"], e["threshold"], json.dumps(e),
                )
                for e in events
            ],
        )


def get_driver_period_counts(conn, driver_id: str, period_start: datetime, period_end: datetime) -> dict:
    """Conteos ya acumulados para el conductor en este período (antes de
    sumar el viaje que se está procesando ahora). Si no hay fila previa,
    devuelve todo en cero."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT total_events, harsh_braking_count, aggressive_accel_count,
                   excessive_rpm_count, excessive_temp_count,
                   excessive_speed_count, gear_mismatch_count
            FROM driver_risk_scores
            WHERE driver_id = %s AND period_start = %s AND period_end = %s
            """,
            (driver_id, period_start, period_end),
        )
        row = cur.fetchone()
    if row:
        return dict(row)
    return {
        "total_events": 0, "harsh_braking_count": 0, "aggressive_accel_count": 0,
        "excessive_rpm_count": 0, "excessive_temp_count": 0,
        "excessive_speed_count": 0, "gear_mismatch_count": 0,
    }


def set_driver_period_score(conn, driver_id: str, period_start: datetime, period_end: datetime,
                             counts: dict, risk_score: float):
    """Sobrescribe la fila del período con los conteos y el puntaje YA
    acumulados (el caller es responsable de haberlos sumado con
    get_driver_period_counts antes de llamar aquí — ver lambda_function.py)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO driver_risk_scores (
                driver_id, period_start, period_end, total_events,
                harsh_braking_count, aggressive_accel_count,
                excessive_rpm_count, excessive_temp_count,
                excessive_speed_count, gear_mismatch_count, risk_score, last_updated
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (driver_id, period_start, period_end) DO UPDATE SET
                total_events = EXCLUDED.total_events,
                harsh_braking_count = EXCLUDED.harsh_braking_count,
                aggressive_accel_count = EXCLUDED.aggressive_accel_count,
                excessive_rpm_count = EXCLUDED.excessive_rpm_count,
                excessive_temp_count = EXCLUDED.excessive_temp_count,
                excessive_speed_count = EXCLUDED.excessive_speed_count,
                gear_mismatch_count = EXCLUDED.gear_mismatch_count,
                risk_score = EXCLUDED.risk_score,
                last_updated = now()
            """,
            (
                driver_id, period_start, period_end, counts["total_events"],
                counts["harsh_braking_count"], counts["aggressive_accel_count"],
                counts["excessive_rpm_count"], counts["excessive_temp_count"],
                counts["excessive_speed_count"], counts["gear_mismatch_count"], risk_score,
            ),
        )


def insert_alert(conn, driver_id: str, risk_score: float, threshold: float, message: str, sns_message_id: str | None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alerts (driver_id, risk_score, threshold, message, sns_message_id)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (driver_id, risk_score, threshold, message, sns_message_id),
        )


def current_period_bounds() -> tuple[datetime, datetime]:
    """Ventana de riesgo 'del día' (UTC) — se podría cambiar a semanal fácilmente."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end
