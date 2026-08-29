# ---------------------------------------------------------------------------
# S3: bucket donde llegan los archivos de viaje (crudo) desde el simulador
# o desde el capturador OBD2 real.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "raw_trips" {
  bucket = "${var.project_name}-raw-trips"
}

resource "aws_s3_bucket_versioning" "raw_trips" {
  bucket = aws_s3_bucket.raw_trips.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ---------------------------------------------------------------------------
# SNS: tópico al que se publica una alerta cuando un conductor supera el
# umbral de riesgo configurado.
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "risk_alerts" {
  name = "${var.project_name}-risk-alerts"
}

# ---------------------------------------------------------------------------
# DynamoDB: tabla de lectura rápida con el puntaje de riesgo "actual" por
# conductor (además del histórico detallado que vive en Postgres/RDS).
# Muestra el patrón de persistencia poliglota: RDS para análisis histórico,
# DynamoDB para lookups de baja latencia (p.ej. desde una app móvil).
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "driver_risk_current" {
  name         = "${var.project_name}-driver-risk-current"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "driver_id"

  attribute {
    name = "driver_id"
    type = "S"
  }
}

# ---------------------------------------------------------------------------
# IAM: rol que asume la Lambda de procesamiento.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda_exec" {
  name = "${var.project_name}-lambda-exec-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_permissions" {
  name = "${var.project_name}-lambda-permissions"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.raw_trips.arn,
          "${aws_s3_bucket.raw_trips.arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.driver_risk_current.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.risk_alerts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda: procesa cada viaje que llega a S3, detecta eventos de riesgo,
# actualiza DynamoDB y dispara una alerta SNS si corresponde.
#
# El código (src/processing/lambda_function.py) hace `from src.common import
# db` y `from src.processing.risk_rules import ...` — imports absolutos que
# asumen que "src" es un paquete Python real en la raíz del zip (tiene
# __init__.py, ver src/__init__.py). Por eso NO empaquetamos solo
# src/processing/ (eso rompería esos imports en tiempo de ejecución con
# ModuleNotFoundError), sino la raíz del repo completa, excluyendo todo lo
# que no hace falta en runtime (tests, infra, data, etc.). El handler queda
# como "src/processing/lambda_function.handler" para que coincida con esa
# estructura.
#
# Nota de empaquetado: boto3 ya viene incluido en el runtime de Lambda, pero
# psycopg2 NO — por eso va aparte, en su propio Lambda Layer (ver más abajo,
# aws_lambda_layer_version.psycopg2), en vez de venir incluido en este zip.
#
# El handler de AWS Lambda para Python usa notación de puntos (no de
# barras) para rutas anidadas: "src.processing.lambda_function.handler".
#
# Sobre las variables de entorno de más abajo:
#   - DEPLOYMENT_MODE=aws es la más importante de las cuatro: sin ella,
#     get_object_store()/get_kv_store()/get_alert_publisher()
#     (src/common/storage.py) caen a los adaptadores "local" por defecto y
#     tratarían de escribir archivos en el filesystem de la Lambda, que es
#     de solo lectura salvo /tmp.
#   - AWS_REGION NO se define acá: Lambda la inyecta sola como variable
#     reservada del sistema (y Terraform rechaza el apply si intentas
#     sobreescribirla a mano) — config.py ya lee esa misma variable.
#   - DATABASE_URL apunta a Neon, no a una RDS de Terraform — ver
#     variables.tf (var.database_url) y el README para el porqué.
# ---------------------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/build/risk_processor.zip"

  excludes = [
    ".git", ".github", ".pytest_cache", "data", "docs", "infra", "tests",
    "venv", ".venv", "docker-compose.yml", ".env", ".env.example",
    "pytest.ini", "README.md", "requirements.txt",
    "**/__pycache__/**", "**/*.pyc",
  ]
}


# ---------------------------------------------------------------------------
# Lambda Layer con psycopg2-binary.
#
# psycopg2 tiene una extensión en C (habla el protocolo de Postgres) — no
# basta con "pip install" y listo, el binario tiene que estar compilado
# para el sistema operativo/arquitectura donde CORRE la Lambda (Amazon
# Linux, x86_64), no para tu laptop. Por eso el wheel se descarga con
# `--platform manylinux2014_x86_64` en vez de dejar que pip elija el de tu
# sistema operativo — así el mismo comando funciona igual si lo corres
# desde Windows, Mac o Linux, porque nunca compila nada localmente, solo
# descarga el binario ya compilado para Lambda desde PyPI.
#
# Separar esto en un Layer (en vez de meter psycopg2 directo en el zip de
# la función) es la práctica recomendada por AWS: separa "mi código" de
# "dependencias de terceros", y permite reusar el mismo Layer en más de
# una Lambda sin duplicar el paquete.
#
# Regenerar el layer (ej. si cambia la versión de psycopg2):
#   pip install psycopg2-binary --platform manylinux2014_x86_64 \
#       --implementation cp --python-version 3.12 --abi cp312 \
#       --only-binary=:all: --target infra/layer/python
# ---------------------------------------------------------------------------
data "archive_file" "psycopg2_layer_zip" {
  type        = "zip"
  source_dir  = "${path.module}/layer"
  output_path = "${path.module}/build/psycopg2_layer.zip"
}

resource "aws_lambda_layer_version" "psycopg2" {
  layer_name          = "${var.project_name}-psycopg2"
  filename            = data.archive_file.psycopg2_layer_zip.output_path
  source_code_hash    = data.archive_file.psycopg2_layer_zip.output_base64sha256
  compatible_runtimes = ["python3.12"]
}

resource "aws_lambda_function" "risk_processor" {
  function_name    = "${var.project_name}-risk-processor"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "src.processing.lambda_function.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  timeout          = 30
  layers           = [aws_lambda_layer_version.psycopg2.arn]

  environment {
    variables = {
      DEPLOYMENT_MODE      = "aws"
      S3_BUCKET            = aws_s3_bucket.raw_trips.bucket
      DYNAMODB_TABLE       = aws_dynamodb_table.driver_risk_current.name
      SNS_TOPIC_ARN        = aws_sns_topic.risk_alerts.arn
      DATABASE_URL         = var.database_url
      RISK_ALERT_THRESHOLD = tostring(var.risk_score_alert_threshold)
    }
  }
}

# ---------------------------------------------------------------------------
# S3 -> Lambda: cada objeto nuevo en el bucket dispara el procesamiento
# automáticamente (arquitectura orientada a eventos).
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.risk_processor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.raw_trips.arn
}

resource "aws_s3_bucket_notification" "trip_upload_trigger" {
  bucket = aws_s3_bucket.raw_trips.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.risk_processor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".json"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}
