"""
Captura de datos en tiempo real desde un adaptador OBD2 (ELM327) conectado
al vehículo real, usando la librería `python-obd`.

Genera EXACTAMENTE el mismo formato JSON que el simulador
(src/simulator/generate_trips.py) para que el resto del pipeline
(ingesta -> S3 -> Lambda -> DB -> dashboard) no distinga entre un viaje
simulado y uno real: solo cambia el campo "source_type".

Requiere:
    - Un adaptador ELM327 (USB, Bluetooth o WiFi) conectado al puerto OBD2
      del vehículo.
    - En Bluetooth: emparejar el adaptador con tu computador primero
      (en Linux suele quedar como /dev/rfcomm0, en Windows como un puerto
      COMx, en macOS como /dev/tty.OBDII-Port o similar).

Uso:
    python -m src.obd2_capture.capture_real --port /dev/rfcomm0 --driver DRV-004
    python -m src.obd2_capture.capture_real --port COM5 --driver DRV-004 --duration 20

Nota sobre GPS: los PIDs estándar de OBD2 no incluyen posición GPS. Si
tienes un receptor GPS USB/Bluetooth aparte, puedes conectarlo a
`gps_reader.py` (no incluido aquí) y pasar el lat/lon por ahí. Por
defecto, este script guarda lat/lon en None y el dashboard simplemente
omite el mapa para esos viajes.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw_trips"


def _connect(port: str | None, baudrate: int | None):
    """Import diferido: python-obd no debería ser una dependencia dura para
    quien solo quiere correr el simulador."""
    import obd

    obd.logger.setLevel(obd.logging.WARNING)
    connection = obd.OBD(portstr=port, baudrate=baudrate, fast=False)
    if not connection.is_connected():
        raise RuntimeError(
            f"No se pudo conectar al adaptador OBD2 en el puerto '{port}'. "
            "Verifica que esté emparejado/conectado y que el vehículo esté encendido."
        )
    return connection


def _read_pid(connection, command):
    import obd

    response = connection.query(command)
    if response.is_null():
        return None
    return response.value.magnitude if hasattr(response.value, "magnitude") else response.value


def capture_trip(port: str, driver_id: str, driver_name: str, truck_id: str,
                  duration_minutes: float | None, hz: float = 1.0) -> dict:
    import obd

    connection = _connect(port, baudrate=None)
    trip_id = f"TRP-{uuid.uuid4().hex[:10]}"
    start_time = datetime.now(timezone.utc)
    samples = []

    print(f"Capturando viaje {trip_id} para {driver_name} ({driver_id}). Ctrl+C para detener.")
    deadline = time.time() + duration_minutes * 60 if duration_minutes else None

    try:
        while deadline is None or time.time() < deadline:
            loop_start = time.time()

            rpm = _read_pid(connection, obd.commands.RPM)
            speed_kmh = _read_pid(connection, obd.commands.SPEED)
            coolant_temp = _read_pid(connection, obd.commands.COOLANT_TEMP)

            if rpm is None and speed_kmh is None:
                # el vehículo puede estar apagado o el adaptador se desconectó
                time.sleep(1)
                continue

            samples.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "speed_kmh": round(float(speed_kmh), 1) if speed_kmh is not None else None,
                "rpm": int(rpm) if rpm is not None else None,
                "engine_temp_c": round(float(coolant_temp), 1) if coolant_temp is not None else None,
                "lat": None,
                "lon": None,
            })

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, (1 / hz) - elapsed))
    except KeyboardInterrupt:
        print("\nCaptura detenida por el usuario.")
    finally:
        connection.close()

    end_time = datetime.now(timezone.utc)

    return {
        "trip_id": trip_id,
        "driver_id": driver_id,
        "driver_name": driver_name,
        "truck_id": truck_id,
        "source_type": "obd2_real",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "sample_rate_hz": hz,
        "samples": samples,
    }


def save_trip(trip: dict, out_dir: Path = DATA_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = trip["start_time"][:10]
    file_path = out_dir / f"{date_prefix}_{trip['driver_id']}_{trip['trip_id']}.json"
    file_path.write_text(json.dumps(trip, indent=2))
    return file_path


def main():
    parser = argparse.ArgumentParser(description="Captura un viaje real desde un adaptador OBD2")
    parser.add_argument("--port", required=True, help="Puerto serial del adaptador ELM327 (ej. /dev/rfcomm0, COM5)")
    parser.add_argument("--driver", required=True, help="ID del conductor (ej. DRV-004)")
    parser.add_argument("--driver-name", default="Conductor", help="Nombre del conductor")
    parser.add_argument("--truck", default="TRK-04", help="ID del camión/vehículo")
    parser.add_argument("--duration", type=float, default=None, help="Duración máxima en minutos (opcional)")
    parser.add_argument("--hz", type=float, default=1.0, help="Frecuencia de muestreo (lecturas/seg)")
    args = parser.parse_args()

    trip = capture_trip(
        port=args.port,
        driver_id=args.driver,
        driver_name=args.driver_name,
        truck_id=args.truck,
        duration_minutes=args.duration,
        hz=args.hz,
    )
    path = save_trip(trip)
    print(f"\nViaje guardado en {path} ({len(trip['samples'])} muestras)")
    print("Súbelo al pipeline con: python -m src.ingestion.upload_to_s3 --file " + str(path))


if __name__ == "__main__":
    main()
