"""
Reglas de detección de conducción riesgosa.

Módulo puro (sin AWS, sin base de datos) para que sea trivial de probar con
pytest — ver tests/test_risk_rules.py. Tanto la Lambda real como cualquier
notebook de análisis pueden importar estas mismas funciones.

Seis tipos de evento, en dos familias:

  - "Puntuales" (ocurren en una muestra específica): frenada brusca,
    aceleración agresiva, exceso de RPM, sobrecalentamiento.
  - "Por tramo" (ocurren durante varios segundos seguidos, no en un
    instante): exceso de velocidad, y descalce de marcha (RPM
    desproporcionado a la velocidad — ej. ir en una marcha muy baja para
    la velocidad actual). Para estos se mide la DURACIÓN del tramo, no
    solo si ocurrió — ver _extract_episodes().

Los umbrales de exceso de RPM, velocidad máxima y razón RPM/velocidad
esperada dependen del VEHÍCULO (un camión diésel pesado se comporta muy
distinto a una camioneta bencinera liviana) — ver el parámetro `vehicle`
de detect_events() y la tabla `trucks` en src/db/schema.sql. Si no se
conoce el vehículo (o no está registrado en el catálogo), se usan los
valores de DEFAULT_VEHICLE — el pipeline nunca debe caerse por un dato de
catálogo faltante.
"""
from __future__ import annotations

from datetime import datetime
from typing import TypedDict

# km/h de cambio de velocidad en 1 segundo ~ equivalente a m/s^2 * 3.6.
# No dependen del tipo de vehículo: un frenazo brusco es un frenazo brusco
# sea diésel o bencinero — a diferencia de RPM/velocidad, que sí varían.
HARSH_BRAKING_THRESHOLD_KMH_S = 15.0
AGGRESSIVE_ACCEL_THRESHOLD_KMH_S = 12.0

# Umbral de RPM de respaldo, usado solo si el vehículo no tiene su propio
# max_rpm_normal (ver DEFAULT_VEHICLE más abajo).
EXCESSIVE_RPM_THRESHOLD = 2900
EXCESSIVE_TEMP_THRESHOLD_C = 110.0

# Perfil de vehículo por defecto — se usa cuando no se pasa `vehicle` a
# detect_events(), o cuando el truck_id no está en la tabla `trucks`.
DEFAULT_VEHICLE = {
    "max_rpm_normal": EXCESSIVE_RPM_THRESHOLD,
    "max_speed_kmh": 100.0,
    "rpm_per_kmh_min": 20.0,
    "rpm_per_kmh_max": 55.0,
}

# Bajo esta velocidad no se evalúa la razón RPM/velocidad (al ralentí o
# casi detenido, cualquier RPM es "normal" y la razón se dispara sin
# significar nada — ruido, no señal de descalce de marcha real).
MIN_SPEED_FOR_RATIO_CHECK_KMH = 15.0

# Los eventos "por tramo" que duran menos de esto se descartan — evita que
# un solo instante de ruido del sensor (o un adelantamiento de 1 segundo)
# cuente como un evento de riesgo real.
MIN_EPISODE_DURATION_S = 3.0

# Peso de cada tipo de evento al calcular el puntaje de riesgo (0-100)
EVENT_WEIGHTS = {
    "harsh_braking": 7,
    "aggressive_acceleration": 5,
    "excessive_rpm": 4,
    "excessive_temp": 9,   # el sobrecalentamiento es lo más grave: riesgo mecánico + de incendio
    "excessive_speed": 8,  # exceso de velocidad sostenido: alto riesgo de colisión
    "gear_mismatch": 3,    # desgaste mecánico, pero menor riesgo agudo que los anteriores
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


def _resolve_vehicle(vehicle: dict | None) -> dict:
    return {**DEFAULT_VEHICLE, **(vehicle or {})}


def _extract_episodes(samples: list[dict], condition_fn) -> list[tuple[list[dict], float]]:
    """Agrupa muestras consecutivas que cumplen `condition_fn(sample)` en
    tramos continuos, calcula la duración de cada uno (a partir de sus
    timestamps ISO) y descarta los que duran menos de
    MIN_EPISODE_DURATION_S. Usado por los eventos "por tramo" — a
    diferencia de los puntuales, que se detectan muestra por muestra."""
    raw_episodes: list[list[dict]] = []
    current: list[dict] = []
    for sample in samples:
        if condition_fn(sample):
            current.append(sample)
        else:
            if current:
                raw_episodes.append(current)
            current = []
    if current:
        raw_episodes.append(current)

    episodes = []
    for ep in raw_episodes:
        start = datetime.fromisoformat(ep[0]["timestamp"])
        end = datetime.fromisoformat(ep[-1]["timestamp"])
        duration = max((end - start).total_seconds(), 1.0)
        if duration >= MIN_EPISODE_DURATION_S:
            episodes.append((ep, duration))
    return episodes


def _detect_speed_violations(samples: list[dict], vehicle: dict) -> list[RiskEvent]:
    max_speed = vehicle["max_speed_kmh"]

    def over_limit(sample: dict) -> bool:
        speed = sample.get("speed_kmh")
        return speed is not None and speed > max_speed

    events: list[RiskEvent] = []
    for episode, duration in _extract_episodes(samples, over_limit):
        peak_speed = max(s["speed_kmh"] for s in episode)
        events.append({
            "event_type": "excessive_speed",
            "severity": _severity_for("excessive_speed", peak_speed, max_speed),
            "event_time": episode[0]["timestamp"],
            "value": round(duration, 1),
            "threshold": max_speed,
            "duration_s": round(duration, 1),
            "peak_speed_kmh": round(peak_speed, 1),
        })
    return events


def _detect_gear_mismatch(samples: list[dict], vehicle: dict) -> list[RiskEvent]:
    rpm_min, rpm_max = vehicle["rpm_per_kmh_min"], vehicle["rpm_per_kmh_max"]

    def mismatched(sample: dict) -> bool:
        speed = sample.get("speed_kmh")
        rpm = sample.get("rpm")
        if speed is None or rpm is None or speed < MIN_SPEED_FOR_RATIO_CHECK_KMH:
            return False
        ratio = rpm / speed
        return ratio < rpm_min or ratio > rpm_max

    events: list[RiskEvent] = []
    for episode, duration in _extract_episodes(samples, mismatched):
        ratios = [s["rpm"] / s["speed_kmh"] for s in episode]
        over = [r for r in ratios if r > rpm_max]
        if over:
            peak_ratio = max(over)
            threshold = rpm_max
            direction = "muy alta para la velocidad (posible marcha muy baja)"
            severity_ratio = peak_ratio / rpm_max
        else:
            peak_ratio = min(ratios)
            threshold = rpm_min
            direction = "muy baja para la velocidad (posible marcha muy alta / motor forzado)"
            severity_ratio = rpm_min / peak_ratio
        events.append({
            "event_type": "gear_mismatch",
            "severity": _severity_for("gear_mismatch", severity_ratio, 1.0),
            "event_time": episode[0]["timestamp"],
            "value": round(peak_ratio, 1),
            "threshold": round(threshold, 1),
            "duration_s": round(duration, 1),
            "direction": direction,
        })
    return events


def detect_events(samples: list[dict], vehicle: dict | None = None) -> list[RiskEvent]:
    """Recorre las muestras de un viaje (ordenadas por tiempo) y devuelve
    la lista de eventos de riesgo detectados, combinando los puntuales con
    los de tramo (exceso de velocidad, descalce de marcha). `vehicle` son
    los umbrales propios del vehículo (ver DEFAULT_VEHICLE) — si no se
    pasa, se usan los genéricos."""
    v = _resolve_vehicle(vehicle)
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

        if rpm is not None and rpm >= v["max_rpm_normal"]:
            events.append({
                "event_type": "excessive_rpm",
                "severity": _severity_for("excessive_rpm", rpm, v["max_rpm_normal"]),
                "event_time": t,
                "value": rpm,
                "threshold": v["max_rpm_normal"],
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

    events.extend(_detect_speed_violations(samples, v))
    events.extend(_detect_gear_mismatch(samples, v))
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
        "excessive_speed_count": "excessive_speed",
        "gear_mismatch_count": "gear_mismatch",
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
        "excessive_speed_count": 0,
        "gear_mismatch_count": 0,
    }
    key_map = {
        "harsh_braking": "harsh_braking_count",
        "aggressive_acceleration": "aggressive_accel_count",
        "excessive_rpm": "excessive_rpm_count",
        "excessive_temp": "excessive_temp_count",
        "excessive_speed": "excessive_speed_count",
        "gear_mismatch": "gear_mismatch_count",
    }
    for e in events:
        counts[key_map[e["event_type"]]] += 1
    counts["total_events"] = len(events)
    return counts


def describe_event(event: dict) -> str:
    """Traduce un evento (tal como lo devuelve detect_events) a una frase
    legible en español — pensado para mostrarse directamente en el
    dashboard o en una alerta, en vez de solo un puntaje numérico."""
    et = event["event_type"]

    if et == "harsh_braking":
        return f"Frenada brusca: caída de {event['value']:.0f} km/h en 1 segundo."
    if et == "aggressive_acceleration":
        return f"Aceleración agresiva: subida de {event['value']:.0f} km/h en 1 segundo."
    if et == "excessive_rpm":
        return f"Exceso de RPM: {event['value']:.0f} RPM (máximo normal para este vehículo: {event['threshold']:.0f})."
    if et == "excessive_temp":
        return f"Sobrecalentamiento del motor: {event['value']:.0f}°C (umbral: {event['threshold']:.0f}°C)."
    if et == "excessive_speed":
        return (
            f"{event['duration_s']:.0f} segundos sobre el límite de {event['threshold']:.0f} km/h "
            f"(velocidad máxima alcanzada: {event['peak_speed_kmh']:.0f} km/h)."
        )
    if et == "gear_mismatch":
        return (
            f"{event['duration_s']:.0f} segundos con revoluciones {event['direction']} "
            f"(razón RPM/velocidad: {event['value']:.1f}, límite normal: {event['threshold']:.1f})."
        )
    return f"Evento de riesgo: {et}."
