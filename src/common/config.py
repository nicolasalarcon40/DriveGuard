"""
Configuración centralizada, leída desde variables de entorno (.env).

Este es el único lugar del proyecto que sabe si estamos corriendo contra
moto_server (dev local sin Docker), LocalStack (Docker, más fiel a AWS real)
o AWS real. El resto del código (simulador, ingesta, Lambda, dashboard)
nunca hardcodea un endpoint: siempre importa esta clase.
"""
from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # "local"  -> adaptadores en disco/log, sin dependencias externas (default,
    #             lo que corre en este sandbox de desarrollo).
    # "aws"    -> boto3 real, contra AWS real o LocalStack (tu laptop con
    #             Docker, o CI). Ver src/common/storage.py.
    deployment_mode: str = os.getenv("DEPLOYMENT_MODE", "local")

    aws_endpoint_url: str | None = os.getenv("AWS_ENDPOINT_URL") or None
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "test")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "test")

    s3_bucket: str = os.getenv("S3_BUCKET", "obd2-fleet-raw-trips")
    dynamodb_table: str = os.getenv("DYNAMODB_TABLE", "obd2-fleet-driver-risk-current")
    sns_topic_arn: str = os.getenv(
        "SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:obd2-fleet-risk-alerts"
    )

    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://obd2:obd2pass@localhost:5432/fleet_telemetry"
    )

    risk_alert_threshold: float = float(os.getenv("RISK_ALERT_THRESHOLD", "70"))


settings = Settings()
