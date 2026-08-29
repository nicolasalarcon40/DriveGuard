"""
Reglas de detección de conducción riesgosa.

Módulo puro (sin AWS, sin base de datos) para que sea trivial de probar con
pytest — ver tests/test_risk_rules.py. Tanto la Lambda real como cualquier
notebook de análisis pueden importar estas mismas funciones.

Los umbrales están pensados para un camión diésel de flota; ajústalos según
el vehículo real que estés capturando con el OBD2 (por ejemplo, revisa cuál
es el RPM de corte/redline real de tu motor).
"""
from __future__ import annotations

from datetime import datetime
from typing import TypedDict

# km/h de cambio de velocidad en 1 segundo ~ equivalente a m/s^2 * 3.6
HARSH_BRAKING_THRESHOLD_KMH_S = 15.0        # frenada brusca
AGGRESSIVE_ACCEL_THRESHOLD_KMH_S = 12.0     # aceleración agresiva
EXCESSIVE_RPM_THRESHOLD = 2900              # RPM
EXCESSIVE_TEMP_THRESHOLD_C = 110.0          # temperatura del motor

# Peso de cada tipo de evento al calcular el puntaje de riesgo (0-100)
EVENT_WEIGHTS = {
    "harsh_braking": 7,
    "aggressive_acceleration": 5,
    "excessive_rpm": 4,
    "excessive_temp": 9,   # el sobrecalentamiento es lo más grave: riesgo mecánico + de incendio
}


class RiskEvent(TypedDict):
    event_type: str
    severity: str
    event_time: str
    value: float
    threshold: float


def _severity_for(event_type: str, value: float, threshold: float) -> str:
    ratio = value / threshold if threshold else 1
    if ratio >= 1.6:
        return "high"
    if ratio >= 1.2:
        return "medium"
    return "low"


def detect_events(samples: list[dict]) -> list[RiskEvent]:
    """Recorre las muestras de un viaje (ordenadas por tiempo) y devuelve
    la lista de eventos de riesgo detectados."""
    events: list[RiskEvent] = []
    prev = None

    for sample in samples:
        speed = sample.get("speed_kmh")
        rpm = sample.get("rpm")
        temp = sample.get("engine_temp_c")
        t = sample["timestamp"]

        if prev is not None and prev.get("speed_kmh") is not None and speed is not None:
            delta = speed - prev["speed_kmh"]

            if -delta >= HARSH_BRAKING_THRESHOLD_KMH_S:
                events.append({
                    "event_type": "harsh_braking",
                    "severity": _severity_for("harsh_braking", -delta, HARSH_BRAKING_THRESHOLD_KMH_S),
                    "event_time": t,
                    "value": round(-delta, 2),
                    "threshold": HARSH_BRAKING_THRESHOLD_KMH_S,
                })
            elif delta >= AGGRESSIVE_ACCEL_THRESHOLD_KMH_S:
                events.append({
                    "event_type": "aggressive_acceleration",
                    "severity": _severity_for("aggressive_acceleration", delta, AGGRESSIVE_ACCEL_THRESHOLD_KMH_S),
                    "event_time": t,
                    "value": round(delta, 2),
                    "threshold": AGGRESSIVE_ACCEL_THRESHOLD_KMH_S,
                })

        if rpm is not None and rpm >= EXCESSIVE_RPM_THRESHOLD:
            events.append({
                "event_type": "excessive_rpm",
                "severity": _severity_for("excessive_rpm", rpm, EXCESSIVE_RPM_THRESHOLD),
                "event_time": t,
                "value": rpm,
                "threshold": EXCESSIVE_RPM_THRESHOLD,
            })

        if temp is not None and temp >= EXCESSIVE_TEMP_THRESHOLD_C:
            events.append({
                "event_type": "excessive_temp",
                "severity": _severity_for("excessive_temp", temp, EXCESSIVE_TEMP_THRESHOLD_C),
                "event_time": t,
                "value": temp,
                "threshold": EXCESSIVE_TEMP_THRESHOLD_C,
            })

        prev = sample

    return events


def compute_score_from_counts(counts: dict) -> float:
    """Igual que compute_risk_score, pero a partir de conteos agregados
    (por ejemplo, todos los eventos de un conductor en el día) en vez de
    la lista detallada de eventos de un solo viaje. Se usa para decidir si
    corresponde disparar una alerta a nivel de período, tal como pide el
    proyecto: 'si un conductor supera cierto umbral de eventos riesgosos
    en un período'. No aplica el multiplicador de severidad (no se
    conserva a nivel agregado), así que es una aproximación algo más
    conservadora que el puntaje por viaje."""
    import math

    key_map = {
        "harsh_braking_count": "harsh_braking",
        "aggressive_accel_count": "aggressive_acceleration",
        "excessive_rpm_count": "excessive_rpm",
        "excessive_temp_count": "excessive_temp",
    }
    raw_score = sum(
        counts.get(count_key, 0) * EVENT_WEIGHTS[event_type]
        for count_key, event_type in key_map.items()
    )
    score = 100 * (1 - math.exp(-raw_score / 40))
    return round(min(100.0, score), 2)


def compute_risk_score(events: list[RiskEvent]) -> float:
    """Convierte la lista de eventos en un puntaje único 0-100.
    Cada evento suma su peso (más si es de severidad alta), con
    rendimientos decrecientes para que un viaje con 50 eventos no dé
    un puntaje absurdamente mayor que uno con 15."""
    severity_multiplier = {"low": 1.0, "medium": 1.4, "high": 2.0}

    raw_score = sum(
        EVENT_WEIGHTS.get(e["event_type"], 1) * severity_multiplier[e["severity"]]
        for e in events
    )
    # compresión logarítmica suave para acotar a 0-100
    import math
    score = 100 * (1 - math.exp(-raw_score / 40))
    return round(min(100.0, score), 2)


def summarize_events(events: list[RiskEvent]) -> dict:
    counts = {
        "harsh_braking_count": 0,
        "aggressive_accel_count": 0,
        "excessive_rpm_count": 0,
        "excessive_temp_count": 0,
    }
    key_map = {
        "harsh_braking": "harsh_braking_count",
        "aggressive_acceleration": "aggressive_accel_count",
        "excessive_rpm": "excessive_rpm_count",
        "excessive_temp": "excessive_temp_count",
    }
    for e in events:
        counts[key_map[e["event_type"]]] += 1
    counts["total_events"] = len(events)
    return counts
