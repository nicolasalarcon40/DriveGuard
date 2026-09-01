"""
Tests del módulo de reglas de riesgo (src/processing/risk_rules.py).

A propósito, este es el módulo con más cobertura de tests del proyecto: es
lógica de negocio pura (sin AWS, sin base de datos), así que no necesita
mocks ni fixtures pesadas — justo el tipo de código que SÍ se puede (y se
debe) probar exhaustivamente.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.processing.risk_rules import (
    AGGRESSIVE_ACCEL_THRESHOLD_KMH_S,
    EXCESSIVE_RPM_THRESHOLD,
    EXCESSIVE_TEMP_THRESHOLD_C,
    HARSH_BRAKING_THRESHOLD_KMH_S,
    compute_risk_score,
    compute_score_from_counts,
    describe_event,
    detect_events,
    summarize_events,
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sample(t, speed=70.0, rpm=1800, temp=90.0, lat=None, lon=None):
    timestamp = (_BASE_TIME + timedelta(seconds=t)).isoformat()
    return {
        "timestamp": timestamp, "speed_kmh": speed, "rpm": rpm,
        "engine_temp_c": temp, "lat": lat, "lon": lon,
    }


# --------------------------------------------------------------------------
# detect_events — eventos puntuales (sin pasar `vehicle`, usan DEFAULT_VEHICLE)
# --------------------------------------------------------------------------
def test_no_events_on_smooth_driving():
    samples = [_sample(i, speed=70 + (i % 3)) for i in range(30)]
    assert detect_events(samples) == []


def test_detects_harsh_braking():
    samples = [
        _sample(0, speed=80.0),
        _sample(1, speed=80.0 - HARSH_BRAKING_THRESHOLD_KMH_S - 1),
    ]
    events = detect_events(samples)
    assert len(events) == 1
    assert events[0]["event_type"] == "harsh_braking"
    assert events[0]["value"] > HARSH_BRAKING_THRESHOLD_KMH_S


def test_detects_aggressive_acceleration():
    samples = [
        _sample(0, speed=40.0),
        _sample(1, speed=40.0 + AGGRESSIVE_ACCEL_THRESHOLD_KMH_S + 1),
    ]
    events = detect_events(samples)
    assert len(events) == 1
    assert events[0]["event_type"] == "aggressive_acceleration"


def test_small_speed_change_is_not_an_event():
    samples = [_sample(0, speed=70.0), _sample(1, speed=75.0)]
    assert detect_events(samples) == []


def test_detects_excessive_rpm():
    samples = [_sample(0, rpm=EXCESSIVE_RPM_THRESHOLD + 100)]
    events = detect_events(samples)
    assert len(events) == 1
    assert events[0]["event_type"] == "excessive_rpm"


def test_detects_excessive_temp():
    samples = [_sample(0, temp=EXCESSIVE_TEMP_THRESHOLD_C + 5)]
    events = detect_events(samples)
    assert len(events) == 1
    assert events[0]["event_type"] == "excessive_temp"


def test_one_sample_can_trigger_multiple_event_types():
    samples = [_sample(0, rpm=EXCESSIVE_RPM_THRESHOLD + 100, temp=EXCESSIVE_TEMP_THRESHOLD_C + 20)]
    events = detect_events(samples)
    types = {e["event_type"] for e in events}
    assert types == {"excessive_rpm", "excessive_temp"}


def test_severity_scales_with_how_far_over_threshold():
    mild = [_sample(0, rpm=int(EXCESSIVE_RPM_THRESHOLD * 1.05))]
    severe = [_sample(0, rpm=int(EXCESSIVE_RPM_THRESHOLD * 2.0))]
    assert detect_events(mild)[0]["severity"] == "low"
    assert detect_events(severe)[0]["severity"] == "high"


def test_missing_speed_samples_do_not_crash_delta_detection():
    samples = [
        _sample(0, speed=None),
        _sample(1, speed=70.0),
        _sample(2, speed=None),
    ]
    # No debe lanzar excepción aunque falten lecturas (típico de hardware
    # OBD2 real con ruido en la señal).
    assert detect_events(samples) == []


# --------------------------------------------------------------------------
# detect_events — eventos por tramo: exceso de velocidad y descalce de marcha
# --------------------------------------------------------------------------
def test_no_excessive_speed_below_vehicle_limit():
    samples = [_sample(i, speed=95.0) for i in range(10)]
    events = detect_events(samples, vehicle={"max_speed_kmh": 100.0})
    assert not any(e["event_type"] == "excessive_speed" for e in events)


def test_detects_sustained_excessive_speed_with_duration():
    samples = [_sample(i, speed=120.0) for i in range(8)]  # 8s seguidos sobre 100 km/h
    events = detect_events(samples, vehicle={"max_speed_kmh": 100.0})
    speed_events = [e for e in events if e["event_type"] == "excessive_speed"]
    assert len(speed_events) == 1
    assert speed_events[0]["duration_s"] >= 7.0
    assert speed_events[0]["peak_speed_kmh"] == 120.0


def test_brief_speed_spike_is_ignored_as_noise():
    # Solo 1 segundo sobre el límite: por debajo de MIN_EPISODE_DURATION_S,
    # se descarta como ruido de sensor en vez de un evento real.
    samples = [_sample(0, speed=70.0), _sample(1, speed=150.0), _sample(2, speed=70.0)]
    events = detect_events(samples, vehicle={"max_speed_kmh": 100.0})
    assert not any(e["event_type"] == "excessive_speed" for e in events)


def test_detects_gear_mismatch_too_high_ratio():
    # RPM muy alto para la velocidad (marcha muy baja) durante varios segundos.
    samples = [_sample(i, speed=40.0, rpm=4000) for i in range(6)]
    vehicle = {"rpm_per_kmh_min": 20.0, "rpm_per_kmh_max": 55.0}
    events = detect_events(samples, vehicle=vehicle)
    mismatches = [e for e in events if e["event_type"] == "gear_mismatch"]
    assert len(mismatches) == 1
    assert "alta" in mismatches[0]["direction"]


def test_gear_mismatch_ignored_below_min_speed_for_ratio_check():
    # A velocidades muy bajas (ralentí/detenido) no se evalúa la razón.
    samples = [_sample(i, speed=5.0, rpm=4000) for i in range(6)]
    events = detect_events(samples, vehicle={"rpm_per_kmh_min": 20.0, "rpm_per_kmh_max": 55.0})
    assert not any(e["event_type"] == "gear_mismatch" for e in events)


def test_vehicle_thresholds_override_defaults():
    # Un umbral de RPM más permisivo (bencinero) no debe disparar exceso de
    # RPM donde uno más estricto (diésel) sí lo haría.
    samples = [_sample(0, rpm=4000)]
    diesel_events = detect_events(samples, vehicle={"max_rpm_normal": 2900})
    gasoline_events = detect_events(samples, vehicle={"max_rpm_normal": 6000})
    assert any(e["event_type"] == "excessive_rpm" for e in diesel_events)
    assert not any(e["event_type"] == "excessive_rpm" for e in gasoline_events)


# --------------------------------------------------------------------------
# describe_event
# --------------------------------------------------------------------------
def test_describe_event_for_all_types_returns_nonempty_string():
    samples = [_sample(i, speed=120.0, rpm=4000) for i in range(6)]
    events = detect_events(samples, vehicle={
        "max_speed_kmh": 100.0, "rpm_per_kmh_min": 20.0, "rpm_per_kmh_max": 55.0, "max_rpm_normal": 6500,
    })
    assert events  # exceso de velocidad y/o descalce de marcha detectados
    for e in events:
        description = describe_event(e)
        assert isinstance(description, str) and len(description) > 0


def test_describe_event_mentions_duration_for_speed_violation():
    samples = [_sample(i, speed=130.0) for i in range(10)]
    events = detect_events(samples, vehicle={"max_speed_kmh": 100.0})
    speed_event = next(e for e in events if e["event_type"] == "excessive_speed")
    description = describe_event(speed_event)
    assert "segundos" in description and "100" in description


# --------------------------------------------------------------------------
# summarize_events
# --------------------------------------------------------------------------
def test_summarize_events_counts_by_type():
    events = detect_events([
        _sample(0, speed=80.0),
        _sample(1, speed=80.0 - HARSH_BRAKING_THRESHOLD_KMH_S - 1, rpm=EXCESSIVE_RPM_THRESHOLD + 50),
    ])
    counts = summarize_events(events)
    assert counts["harsh_braking_count"] == 1
    assert counts["excessive_rpm_count"] == 1
    assert counts["aggressive_accel_count"] == 0
    assert counts["excessive_temp_count"] == 0
    assert counts["excessive_speed_count"] == 0
    assert counts["gear_mismatch_count"] == 0
    assert counts["total_events"] == 2


def test_summarize_events_empty():
    counts = summarize_events([])
    assert counts["total_events"] == 0
    assert all(v == 0 for k, v in counts.items() if k != "total_events")


# --------------------------------------------------------------------------
# compute_risk_score / compute_score_from_counts
# --------------------------------------------------------------------------
def test_no_events_means_zero_risk():
    assert compute_risk_score([]) == 0.0
    assert compute_score_from_counts(summarize_events([])) == 0.0


def test_more_events_means_higher_or_equal_score():
    few = detect_events([
        _sample(0, speed=80.0), _sample(1, speed=80.0 - HARSH_BRAKING_THRESHOLD_KMH_S - 1),
    ])
    many_samples = [_sample(0, speed=80.0)]
    for i in range(1, 20):
        # alterna frenadas bruscas cada 2 muestras
        speed = 80.0 - (HARSH_BRAKING_THRESHOLD_KMH_S + 1) if i % 2 == 0 else 80.0
        many_samples.append(_sample(i, speed=speed))
    many = detect_events(many_samples)

    assert compute_risk_score(many) >= compute_risk_score(few)


def test_risk_score_is_capped_at_100():
    # Genera muchísimos eventos severos para forzar el techo de la curva.
    samples = [_sample(0, speed=200.0)]
    for i in range(1, 200):
        samples.append(_sample(i, speed=200.0 if i % 2 == 0 else 10.0, rpm=6000, temp=140.0))
    events = detect_events(samples)
    assert compute_risk_score(events) <= 100.0


def test_excessive_temp_weighted_higher_than_aggressive_acceleration():
    """El sobrecalentamiento es, por diseño, el evento con más peso (riesgo
    mecánico + de incendio) — ver EVENT_WEIGHTS en risk_rules.py."""
    temp_event = [{
        "event_type": "excessive_temp", "severity": "low",
        "event_time": "t", "value": 111, "threshold": EXCESSIVE_TEMP_THRESHOLD_C,
    }]
    accel_event = [{
        "event_type": "aggressive_acceleration", "severity": "low",
        "event_time": "t", "value": 13, "threshold": AGGRESSIVE_ACCEL_THRESHOLD_KMH_S,
    }]
    assert compute_risk_score(temp_event) > compute_risk_score(accel_event)


def test_compute_score_from_counts_matches_shape_of_summarize_events():
    counts = summarize_events([])
    # No debe lanzar KeyError por claves faltantes/sobrantes.
    score = compute_score_from_counts(counts)
    assert isinstance(score, float)
