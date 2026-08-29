"""
Tests del generador de viajes simulados (src/simulator/generate_trips.py).

No probamos valores exactos (es aleatorio a propósito, para que la demo se
vea distinta cada vez), sino que la SALIDA respeta el contrato que el resto
del pipeline espera: mismo esquema que produciría un capturador OBD2 real
(ver src/obd2_capture/capture_real.py), rangos físicamente plausibles, y
series de tiempo ordenadas.
"""
from __future__ import annotations

from src.simulator.generate_trips import DRIVER_PROFILES, generate_trip


def test_generate_trip_has_expected_top_level_schema():
    trip = generate_trip("DRV-001", duration_minutes=1)
    for field in (
        "trip_id", "driver_id", "driver_name", "truck_id", "source_type",
        "start_time", "end_time", "sample_rate_hz", "samples",
    ):
        assert field in trip
    assert trip["driver_id"] == "DRV-001"
    assert trip["source_type"] == "simulated"


def test_generate_trip_sample_count_matches_duration():
    trip = generate_trip("DRV-002", duration_minutes=2)
    assert len(trip["samples"]) == 2 * 60 * trip["sample_rate_hz"]


def test_generate_trip_samples_have_expected_fields_and_plausible_ranges():
    trip = generate_trip("DRV-003", duration_minutes=1)
    for s in trip["samples"]:
        assert set(s.keys()) == {"timestamp", "speed_kmh", "rpm", "engine_temp_c", "lat", "lon"}
        assert 0 <= s["speed_kmh"] <= 150
        assert 0 <= s["rpm"] <= 8000
        assert -50 <= s["engine_temp_c"] <= 200


def test_generate_trip_timestamps_are_strictly_increasing():
    trip = generate_trip("DRV-004", duration_minutes=1)
    timestamps = [s["timestamp"] for s in trip["samples"]]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)


def test_generate_trip_ids_are_unique_across_calls():
    trip_ids = {generate_trip("DRV-001", duration_minutes=1)["trip_id"] for _ in range(10)}
    assert len(trip_ids) == 10


def test_all_driver_profiles_can_generate_a_trip():
    for driver_id in DRIVER_PROFILES:
        trip = generate_trip(driver_id, duration_minutes=1)
        assert trip["driver_id"] == driver_id
        assert trip["driver_name"] == DRIVER_PROFILES[driver_id]["name"]
