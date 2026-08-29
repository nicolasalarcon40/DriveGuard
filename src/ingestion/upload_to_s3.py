"""
Sube archivos de viaje (JSON, generados por el simulador o por el
capturador OBD2 real) al object store configurado, particionados por
conductor y fecha:

    trips/<driver_id>/<YYYY-MM-DD>/<trip_id>.json

En modo AWS (DEPLOYMENT_MODE=aws, con LocalStack+Terraform o AWS real), la
sola llegada del objeto a S3 dispara automáticamente la Lambda de
detección de riesgo (ver infra/main.tf, `aws_s3_bucket_notification`). En
modo local (el que corre en este sandbox), no hay un trigger S3->Lambda
real, así que este script simula ese evento invocando el MISMO handler
directamente justo después de "subir" el archivo — la lógica de negocio
(src/processing/lambda_function.py) es idéntica en ambos casos, solo
cambia quién la invoca. Ver src/common/storage.py para el detalle de los
adaptadores.

Uso:
    python -m src.ingestion.upload_to_s3 --all-pending
    python -m src.ingestion.upload_to_s3 --file data/raw_trips/2026-08-19_DRV-003_TRP-xxx.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.common.storage import get_object_store

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "raw_trips"
UPLOADED_MARKER_SUFFIX = ".uploaded"


def s3_key_for(trip: dict) -> str:
    date_prefix = trip["start_time"][:10]
    return f"trips/{trip['driver_id']}/{date_prefix}/{trip['trip_id']}.json"


def upload_file(file_path: Path, invoke_processor: bool = True) -> str:
    trip = json.loads(file_path.read_text())
    key = s3_key_for(trip)

    store = get_object_store()
    store.put_object(key, file_path.read_bytes())
    print(f"  subido -> trips bucket / {key}")

    marker = file_path.with_suffix(file_path.suffix + UPLOADED_MARKER_SUFFIX)
    marker.write_text(key)

    if invoke_processor:
        _simulate_s3_event(key)

    return key


def _simulate_s3_event(key: str):
    """Construye un evento con la MISMA forma que un evento real de S3 y
    llama al handler de la Lambda directamente (ver docstring del módulo)."""
    from src.common.config import settings
    from src.processing.lambda_function import handler as lambda_handler

    event = {
        "Records": [
            {"s3": {"bucket": {"name": settings.s3_bucket}, "object": {"key": key}}}
        ]
    }
    lambda_handler(event, context=None)


def pending_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    all_json = sorted(DATA_DIR.glob("*.json"))
    return [p for p in all_json if not p.with_suffix(p.suffix + UPLOADED_MARKER_SUFFIX).exists()]


def main():
    parser = argparse.ArgumentParser(description="Sube viajes al object store y dispara el procesamiento")
    parser.add_argument("--file", help="Ruta a un archivo de viaje específico")
    parser.add_argument("--all-pending", action="store_true", help="Sube todos los viajes aún no subidos en data/raw_trips")
    parser.add_argument("--no-process", action="store_true", help="Solo subir, sin invocar el procesamiento (modo prod-like)")
    args = parser.parse_args()

    files = [Path(args.file)] if args.file else pending_files()
    if not files:
        print("No hay viajes pendientes por subir.")
        return

    for f in files:
        upload_file(f, invoke_processor=not args.no_process)

    print(f"\n{len(files)} viaje(s) subido(s) y procesado(s).")


if __name__ == "__main__":
    main()
