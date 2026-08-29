# Provider de AWS: por defecto (var.use_localstack = false) despliega contra
# AWS real, tomando las credenciales de tu `aws configure` / variables de
# entorno estándar — no hay nada de LocalStack en el camino por defecto.
#
# El bloque `endpoints` y las credenciales dummy ("test"/"test") SOLO se
# activan si pones -var="use_localstack=true" (para desarrollar contra
# LocalStack vía docker-compose.yml sin tocar una cuenta de AWS real). Es
# el mismo archivo .tf para ambos casos — la única diferencia es esa
# variable — para que la infraestructura sea "production-shaped" desde el
# día uno y no dos configuraciones divergentes que hay que mantener por
# separado.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  access_key                  = var.use_localstack ? "test" : null
  secret_key                  = var.use_localstack ? "test" : null
  s3_use_path_style           = var.use_localstack
  skip_credentials_validation = var.use_localstack
  skip_metadata_api_check     = var.use_localstack
  skip_requesting_account_id  = var.use_localstack

  dynamic "endpoints" {
    for_each = var.use_localstack ? [1] : []
    content {
      s3         = var.localstack_endpoint
      lambda     = var.localstack_endpoint
      dynamodb   = var.localstack_endpoint
      sns        = var.localstack_endpoint
      iam        = var.localstack_endpoint
      logs       = var.localstack_endpoint
      cloudwatch = var.localstack_endpoint
    }
  }
}
