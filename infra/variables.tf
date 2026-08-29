variable "aws_region" {
  description = "Región de AWS (o simulada por LocalStack)"
  type        = string
  default     = "us-east-1"
}

variable "localstack_endpoint" {
  description = "Endpoint del edge de LocalStack"
  type        = string
  default     = "http://localhost:4566"
}

variable "use_localstack" {
  description = <<-EOT
    true  = apunta a LocalStack corriendo en Docker (docker-compose.yml), con
            credenciales dummy y endpoints falsos — para desarrollar sin tocar
            una cuenta de AWS real.
    false = despliega contra AWS real, usando las credenciales que ya tengas
            configuradas (`aws configure`) y los endpoints reales de AWS.
            Este es el default, porque es el modo que se usa para la demo.
  EOT
  type    = bool
  default = false
}

variable "project_name" {
  description = <<-EOT
    Prefijo usado para nombrar todos los recursos, incluido el bucket S3 —
    y los nombres de bucket S3 son ÚNICOS A NIVEL GLOBAL en toda AWS, no solo
    en tu cuenta. Si usas el default tal cual y alguien más (en cualquier
    parte del mundo) ya lo usó, el apply falla con "BucketAlreadyExists".
    Cámbialo a algo con tu nombre, ej. "driveguard-nicolasalarcon".
  EOT
  type    = string
  default = "driveguard"
}

variable "risk_score_alert_threshold" {
  description = "Puntaje de riesgo (0-100) a partir del cual se dispara una alerta SNS"
  type        = number
  default     = 70
}

variable "database_url" {
  description = <<-EOT
    Connection string de Postgres que usará la Lambda (formato
    postgresql://usuario:password@host:5432/basededatos?sslmode=require).
    En este proyecto NO es una RDS de AWS (a propósito, ver README): es un
    Postgres gestionado gratuito (Neon) para no tener que configurar VPC.
    Sin default a propósito, para que nunca quede un secreto commiteado por
    accidente — pásalo por terraform.tfvars (ver terraform.tfvars.example,
    y que terraform.tfvars esté en .gitignore) o con -var en la línea de comandos.
  EOT
  type      = string
  sensitive = true
}
