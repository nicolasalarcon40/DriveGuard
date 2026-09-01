"""
Capa de adaptadores (patrón puertos-y-adaptadores / hexagonal) para los
tres servicios de AWS que usa el pipeline: object storage (S3), key-value
de lectura rápida (DynamoDB) y notificaciones (SNS).

Por qué existe esto en vez de llamar boto3 directo desde la lógica de
negocio:

  1. Portabilidad real: la Lambda (src/processing/lambda_function.py) y el
     script de ingesta nunca importan boto3 directamente. Solo conocen
     estas interfaces (ObjectStore, KeyValueStore, AlertPublisher). Cambiar
     de "correr en mi laptop" a "correr en AWS" es cambiar una variable de
     entorno (DEPLOYMENT_MODE), no reescribir código.

  2. En este entorno de desarrollo (sandbox en la nube sin salida a
     internet para llamadas AWS reales, ni Docker Hub para levantar
     LocalStack) se usa el adaptador "local": S3 se simula como una
     carpeta en disco con la misma estructura de keys que un bucket real,
     DynamoDB se simula con un archivo JSON de key-value, y SNS se simula
     escribiendo a un log de alertas. La lógica de riesgo (risk_rules.py)
     es 100% idéntica en ambos modos — lo único que cambia es dónde
     "aterrizan" los datos.

  3. En infra/ (Terraform) y docker-compose.yml está la versión real con
     S3 + DynamoDB + SNS reales/LocalStack, pensada para correr en tu
     laptop (con Docker) o en CI (GitHub Actions), donde sí hay acceso de
     red completo. Ahí se usa el adaptador "aws" (boto3 real).

Selecciona el modo con la variable de entorno DEPLOYMENT_MODE=local|aws
(ver .env.example).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from src.common.config import settings

LOCAL_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "local_aws"


# ---------------------------------------------------------------------------
# Object storage (S3 en AWS real / carpeta en disco en modo local)
# ---------------------------------------------------------------------------
class ObjectStore(ABC):
    @abstractmethod
    def put_object(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get_object(self, key: str) -> bytes: ...

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[str]: ...


class LocalObjectStore(ObjectStore):
    def __init__(self, bucket: str):
        self.root = LOCAL_DATA_ROOT / "s3" / bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def put_object(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_object(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list_objects(self, prefix: str = "") -> list[str]:
        # .as_posix() (no str()) a propósito: en Windows, Path usa "\" como
        # separador, pero las keys de S3 (y el prefijo que recibe esta
        # función) siempre usan "/" — sin esto, list_objects() nunca
        # encuentra nada en Windows aunque los archivos sí existan.
        return [
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
            if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix)
        ]


class S3ObjectStore(ObjectStore):
    """Adaptador real, usado cuando DEPLOYMENT_MODE=aws (AWS real o
    LocalStack vía Docker en tu laptop/CI). No se ejercita en este sandbox."""

    def __init__(self, bucket: str):
        from src.common.aws_clients import s3_client
        self.bucket = bucket
        self._client = s3_client()

    def put_object(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get_object(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list_objects(self, prefix: str = "") -> list[str]:
        resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return [o["Key"] for o in resp.get("Contents", [])]


# ---------------------------------------------------------------------------
# Key-value de lectura rápida (DynamoDB en AWS real / JSON en disco local)
# ---------------------------------------------------------------------------
class KeyValueStore(ABC):
    @abstractmethod
    def put_item(self, key: str, item: dict) -> None: ...

    @abstractmethod
    def get_item(self, key: str) -> dict | None: ...


class LocalKeyValueStore(KeyValueStore):
    def __init__(self, table: str):
        self.path = LOCAL_DATA_ROOT / "dynamodb" / f"{table}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}")

    def _read_all(self) -> dict:
        return json.loads(self.path.read_text())

    def put_item(self, key: str, item: dict) -> None:
        data = self._read_all()
        data[key] = item
        self.path.write_text(json.dumps(data, indent=2))

    def get_item(self, key: str) -> dict | None:
        return self._read_all().get(key)


class DynamoDBKeyValueStore(KeyValueStore):
    """Adaptador real (DEPLOYMENT_MODE=aws). No se ejercita en este sandbox."""

    def __init__(self, table: str):
        from src.common.aws_clients import dynamodb_resource
        self._table = dynamodb_resource().Table(table)

    def put_item(self, key: str, item: dict) -> None:
        self._table.put_item(Item={"driver_id": key, **item})

    def get_item(self, key: str) -> dict | None:
        resp = self._table.get_item(Key={"driver_id": key})
        return resp.get("Item")


# ---------------------------------------------------------------------------
# Alertas (SNS en AWS real / log de texto en modo local)
# ---------------------------------------------------------------------------
class AlertPublisher(ABC):
    @abstractmethod
    def publish(self, subject: str, message: str) -> str | None:
        """Devuelve un id de mensaje si aplica (o None)."""
        ...


class LocalAlertPublisher(AlertPublisher):
    def __init__(self):
        self.path = LOCAL_DATA_ROOT / "sns" / "risk-alerts.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def publish(self, subject: str, message: str) -> str | None:
        import uuid
        from datetime import datetime, timezone

        msg_id = f"local-{uuid.uuid4().hex[:12]}"
        with self.path.open("a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] ({msg_id}) {subject}: {message}\n")
        print(f"[ALERTA] {subject}: {message}")
        return msg_id


class SNSAlertPublisher(AlertPublisher):
    """Adaptador real (DEPLOYMENT_MODE=aws). No se ejercita en este sandbox."""

    def __init__(self, topic_arn: str):
        from src.common.aws_clients import sns_client
        self.topic_arn = topic_arn
        self._client = sns_client()

    def publish(self, subject: str, message: str) -> str | None:
        resp = self._client.publish(TopicArn=self.topic_arn, Message=message, Subject=subject)
        return resp.get("MessageId")


# ---------------------------------------------------------------------------
# Factories: eligen el adaptador según DEPLOYMENT_MODE
# ---------------------------------------------------------------------------
def get_object_store() -> ObjectStore:
    if settings.deployment_mode == "aws":
        return S3ObjectStore(settings.s3_bucket)
    return LocalObjectStore(settings.s3_bucket)


def get_kv_store() -> KeyValueStore:
    if settings.deployment_mode == "aws":
        return DynamoDBKeyValueStore(settings.dynamodb_table)
    return LocalKeyValueStore(settings.dynamodb_table)


def get_alert_publisher() -> AlertPublisher:
    if settings.deployment_mode == "aws":
        return SNSAlertPublisher(settings.sns_topic_arn)
    return LocalAlertPublisher()
