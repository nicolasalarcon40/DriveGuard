"""
Fábrica de clientes boto3. Centraliza el `endpoint_url` para que todo el
código de la aplicación funcione sin cambios contra moto_server, LocalStack
o AWS real — solo cambia una variable de entorno (ver config.py).
"""
import boto3

from src.common.config import settings


def _boto_kwargs() -> dict:
    kwargs = {"region_name": settings.aws_region, "endpoint_url": settings.aws_endpoint_url}
    # Credenciales explícitas SOLO para moto_server/LocalStack (que no validan
    # credenciales reales, pero igual exigen que boto3 mande algo). Contra AWS
    # real (DEPLOYMENT_MODE=aws) NUNCA hay que pasar credenciales a mano: hay
    # que dejar que boto3 use su cadena por defecto (rol de ejecución de la
    # Lambda cuando corre en AWS; `aws configure` / variables de entorno
    # cuando corre desde tu laptop). Pasar "test"/"test" acá pisaría
    # silenciosamente esas credenciales reales y toda llamada fallaría con
    # InvalidClientTokenId.
    if settings.deployment_mode != "aws":
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


def _client(service_name: str):
    return boto3.client(service_name, **_boto_kwargs())


def s3_client():
    return _client("s3")


def dynamodb_resource():
    return boto3.resource("dynamodb", **_boto_kwargs())


def sns_client():
    return _client("sns")


def ensure_dev_infrastructure():
    """
    Crea el bucket S3, la tabla DynamoDB y el tópico SNS si no existen.
    Solo se usa en modo dev (moto_server) para no tener que correr Terraform
    cada vez que se reinicia el emulador en memoria. En LocalStack/AWS real,
    esos recursos los crea Terraform (infra/) — ver README.
    """
    s3 = s3_client()
    existing_buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if settings.s3_bucket not in existing_buckets:
        if settings.aws_region == "us-east-1":
            s3.create_bucket(Bucket=settings.s3_bucket)
        else:
            s3.create_bucket(
                Bucket=settings.s3_bucket,
                CreateBucketConfiguration={"LocationConstraint": settings.aws_region},
            )

    ddb = dynamodb_resource()
    existing_tables = [t.name for t in ddb.tables.all()]
    if settings.dynamodb_table not in existing_tables:
        ddb.create_table(
            TableName=settings.dynamodb_table,
            KeySchema=[{"AttributeName": "driver_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "driver_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    sns = sns_client()
    topics = sns.list_topics().get("Topics", [])
    topic_name = settings.sns_topic_arn.split(":")[-1]
    if not any(t["TopicArn"].endswith(topic_name) for t in topics):
        sns.create_topic(Name=topic_name)


if __name__ == "__main__":
    ensure_dev_infrastructure()
    print("Infraestructura dev (S3 + DynamoDB + SNS) lista en", settings.aws_endpoint_url)
