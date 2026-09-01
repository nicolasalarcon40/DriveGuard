"""
Generador de viajes de flota simulados.

Produce el MISMO formato de archivo (JSON) que el capturador OBD2 real
(src/obd2_capture/capture_real.py) — así el resto del pipeline (ingesta,
Lambda de detección de riesgo, dashboard) no necesita saber si el viaje
vino de un camión de verdad o de este simulador.

Uso:
    python -m src.simulator.generate_trips --driver DRV-001 --trips 5
    python -m src.simulator.generate_trips --all --trips 8
"""
from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SAMPLE_HZ = 1  # una lectura por segundo, igual que un ELM327 típico en modo continuo

# Perfil de riesgo por conductor: controla qué tan seguido se inyectan
# eventos peligrosos, para que el dashboard muestre variación real entre
# conductores "buenos" y "riesgosos" (bueno para la demo).
DRIVER_PROFILES = {
    "DRV-001": {"name": "Juan Perez", "truck": "TRK-01", "risk": 0.05},   # conductor cuidadoso
    "DRV-002": {"name": "Maria Gonzalez", "truck": "TRK-02", "risk": 0.10},
    "DRV-003": {"name": "Carlos Rojas", "truck": "TRK-03", "risk": 0.28},  # conductor riesgoso
    "DRV-004": {"name": "Nicolas A.", "truck": "TRK-04", "risk": 0.15},
}

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw_trips"


def _new_sample(t, speed, rpm, temp, lat, lon):
    return {
        "timestamp": t.isoformat(),
        "speed_kmh": round(speed, 1),
        "rpm": int(rpm),
        "engine_temp_c": round(temp, 1),
        "lat": round(lat, 5),
        "lon": round(lon, 5),
    }


def generate_trip(driver_id: str, duration_minutes: int | None = None) -> dict:
    """Genera un viaje simulado con física simplificada pero plausible:
    la velocidad varía suavemente, el RPM sigue aproximadamente la
    velocidad (con ruido), y la temperatura del motor sube gradualmente
    hasta estabilizarse. Cada tanto, según el `risk` del conductor, se
    inyecta un evento de riesgo (frenada brusca, aceleración agresiva,
    sobre-revolución o sobrecalentamiento).
    """
    profile = DRIVER_PROFILES[driver_id]
    duration_minutes = duration_minutes or random.randint(18, 45)
    start_time = datetime.now(timezone.utc) - timedelta(minutes=duration_minutes)
    n_samples = duration_minutes * 60 * SAMPLE_HZ

    trip_id = f"TRP-{uuid.uuid4().hex[:10]}"
    lat, lon = -33.45 + random.uniform(-0.3, 0.3), -70.66 + random.uniform(-0.3, 0.3)
    heading = random.uniform(0, 2 * math.pi)

    speed = 0.0
    engine_temp = 20.0  # arranca en frío
    samples = []

    # "excessive_speed" y "gear_mismatch" son eventos POR TRAMO (duran
    # varios segundos seguidos), a diferencia de los otros cuatro, que son
    # puntuales (un instante). Por eso se simulan como un tramo continuo
    # con este pequeño estado, en vez de un solo valor extremo en 1 muestra
    # — ver risk_rules.MIN_EPISODE_DURATION_S, que de otro modo descartaría
    # el evento por "ruido de un instante".
    episode_type = None
    episode_remaining = 0

    for i in range(n_samples):
        t = start_time + timedelta(seconds=i / SAMPLE_HZ)

        # -- Física base: variación suave de velocidad (carretera con tráfico) --
        target_speed = 70 + 25 * math.sin(i / 220) + random.uniform(-4, 4)
        target_speed = max(0, target_speed)
        speed += (target_speed - speed) * 0.05

        event_injected = None

        if episode_remaining > 0:
            event_injected = episode_type
            episode_remaining -= 1
        else:
            risk_roll = random.random()
            if risk_roll < profile["risk"] / 1200:  # eventos son raros por segundo
                event_type = random.choice([
                    "harsh_braking", "aggressive_acceleration",
                    "excessive_rpm", "excessive_temp",
                    "excessive_speed", "gear_mismatch",
                ])
                if event_type == "harsh_braking" and speed > 30:
                    event_injected = event_type
                elif event_type in ("aggressive_acceleration", "excessive_rpm", "excessive_temp"):
                    event_injected = event_type
                elif event_type in ("excessive_speed", "gear_mismatch"):
                    # tramo de 5 a 18 segundos (esta muestra + 4-17 más)
                    episode_type = event_type
                    episode_remaining = random.randint(4, 17)
                    event_injected = event_type

        if event_injected == "harsh_braking":
            speed = max(0, speed - random.uniform(28, 45))  # caída brusca de velocidad
        elif event_injected == "aggressive_acceleration":
            speed = min(120, speed + random.uniform(25, 40))
        elif event_injected == "excessive_speed":
            speed = random.uniform(130, 150)  # muy por sobre el límite de cualquier vehículo del catálogo

        # -- RPM: función de la velocidad + marcha simulada, con ruido --
        base_rpm = 600 + speed * 22 + random.uniform(-80, 80)
        if event_injected == "excessive_rpm":
            base_rpm = random.uniform(3100, 3800)
        elif event_injected == "gear_mismatch":
            base_rpm = speed * random.uniform(85, 105)  # muy desproporcionado a la velocidad actual

        # -- Temperatura del motor: sube y se estabiliza ~92-98C --
        target_temp = 95 + random.uniform(-2, 2)
        engine_temp += (target_temp - engine_temp) * 0.01
        if event_injected == "excessive_temp":
            engine_temp = random.uniform(112, 125)

        # -- Posición GPS: avanza según heading, con pequeño ruido --
        heading += random.uniform(-0.05, 0.05)
        lat += math.cos(heading) * 0.00003 * (speed / 60)
        lon += math.sin(heading) * 0.00003 * (speed / 60)

        samples.append(_new_sample(t, speed, base_rpm, engine_temp, lat, lon))

    end_time = start_time + timedelta(seconds=n_samples / SAMPLE_HZ)

    return {
        "trip_id": trip_id,
        "driver_id": driver_id,
        "driver_name": profile["name"],
        "truck_id": profile["truck"],
        "source_type": "simulated",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "sample_rate_hz": SAMPLE_HZ,
        "samples": samples,
    }


def save_trip(trip: dict, out_dir: Path = DATA_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = trip["start_time"][:10]
    file_path = out_dir / f"{date_prefix}_{trip['driver_id']}_{trip['trip_id']}.json"
    file_path.write_text(json.dumps(trip, indent=2))
    return file_path


def main():
    parser = argparse.ArgumentParser(description="Genera viajes OBD2 simulados")
    parser.add_argument("--driver", help="ID de un conductor específico (ej. DRV-001)")
    parser.add_argument("--all", action="store_true", help="Generar para todos los conductores")
    parser.add_argument("--trips", type=int, default=3, help="Viajes por conductor")
    args = parser.parse_args()

    driver_ids = list(DRIVER_PROFILES.keys()) if args.all else [args.driver]
    if not driver_ids or driver_ids == [None]:
        parser.error("Especifica --driver DRV-00X o --all")

    generated = []
    for driver_id in driver_ids:
        for _ in range(args.trips):
            trip = generate_trip(driver_id)
            path = save_trip(trip)
            generated.append(path)
            n_events = sum(1 for s in trip["samples"] if s["rpm"] > 2900 or s["engine_temp_c"] > 110)
            print(f"  {path.name}  ({len(trip['samples'])} muestras)")

    print(f"\n{len(generated)} viajes generados en {DATA_DIR}")


if __name__ == "__main__":
    main()
