# 🚚 Fleet Risk Telemetry — Detección de Conducción Riesgosa con OBD2

Pipeline de datos end-to-end que captura telemetría de un vehículo en tiempo real (RPM, velocidad, temperatura del motor y GPS) desde un dispositivo **OBD2 real**, detecta patrones de conducción riesgosa (frenadas bruscas, aceleraciones agresivas, sobre-revolución, sobrecalentamiento), calcula un puntaje de riesgo por conductor y dispara alertas automáticas cuando se supera un umbral — todo visible en un dashboard en tiempo real.

Arquitectura pensada para AWS (S3 + Lambda + RDS/DynamoDB + SNS), con una capa de adaptadores que permite correr **exactamente la misma lógica de negocio 100% en local**, sin necesitar una cuenta de AWS ni Docker, para desarrollar y probar el pipeline completo desde cero.

[![CI](https://github.com/TU_USUARIO/TU_REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/TU_USUARIO/TU_REPO/actions/workflows/ci.yml)

> Reemplaza `TU_USUARIO/TU_REPO` en el badge de arriba por la ruta real una vez que subas este repo a GitHub — el pipeline de CI (tests + validación de Terraform) corre automáticamente en cada push.

---

## El problema

Una empresa de transporte de carga quiere reducir accidentes, costos de mantención y consumo de combustible identificando a los conductores con hábitos de manejo riesgosos **antes** de que ocurra un incidente. Este proyecto simula el sistema de telemetría que resolvería eso: cada camión reporta su estado mecánico en tiempo real, el sistema detecta automáticamente comportamiento riesgoso, calcula un puntaje de riesgo por conductor por período, y notifica a supervisión apenas un conductor cruza el umbral configurado — sin que nadie tenga que revisar reportes manualmente.

## Demo

![Dashboard de telemetría de flota](docs/dashboard-screenshot.png)

*Datos generados por el simulador incluido en el repo (`src/simulator/generate_trips.py`), procesados por el mismo pipeline que procesaría datos reales del OBD2.*

---

## Arquitectura

```mermaid
flowchart LR
    subgraph Captura
        OBD2["🔌 OBD2 real\n(python-obd / ELM327)"]
        SIM["🎲 Simulador\n(datos sintéticos)"]
    end

    OBD2 -->|JSON: rpm, speed,\ntemp, gps| RAW[("data/raw_trips/\n*.json")]
    SIM --> RAW

    RAW -->|upload_to_s3.py| S3["📦 Object Store\n(S3 / carpeta local)"]
    S3 -->|evento ObjectCreated| LAMBDA["⚙️ Lambda\nrisk_rules.py\nlambda_function.py"]

    LAMBDA -->|viajes + eventos\ndetallados| RDS[("🗄️ RDS / Postgres\ntrips, risk_events,\ndriver_risk_scores")]
    LAMBDA -->|puntaje actual\n(lectura rápida)| DDB[("⚡ DynamoDB\n(JSON local)")]
    LAMBDA -->|si supera umbral| SNS["📣 SNS\n(log local)"]

    RDS --> DASH["📊 Dashboard\n(Streamlit)"]
    DDB -.->|futuro: app móvil| DASH
    SNS -.->|futuro: email/Slack| NOTIFY["🔔 Notificación\na supervisión"]
```

### El detalle importante: dos modos de despliegue, un solo código

Este proyecto se construyó desde un entorno de desarrollo **sin acceso a la consola de AWS ni a Docker Hub** — una restricción real que obligó a tomar una decisión de arquitectura que terminó siendo la parte más interesante del proyecto: en vez de escribir el pipeline contra boto3 directamente (lo que lo haría imposible de probar sin AWS), toda la lógica de negocio depende de **interfaces abstractas**, no de servicios concretos:

```python
# src/common/storage.py
class ObjectStore(ABC): ...      # el "S3" que ve el resto del código
class KeyValueStore(ABC): ...    # el "DynamoDB" que ve el resto del código
class AlertPublisher(ABC): ...   # el "SNS" que ve el resto del código
```

Cada interfaz tiene dos implementaciones, elegidas en runtime con una sola variable de entorno (`DEPLOYMENT_MODE`):

| | `DEPLOYMENT_MODE=local` (default) | `DEPLOYMENT_MODE=aws` |
|---|---|---|
| **S3** | carpeta en disco (`data/local_aws/s3/`) | bucket S3 real (o LocalStack) |
| **DynamoDB** | archivo JSON (`data/local_aws/dynamodb/`) | tabla DynamoDB real (o LocalStack) |
| **SNS** | log de texto (`data/local_aws/sns/`) | tópico SNS real (o LocalStack) |
| **Lambda** | se invoca como función Python normal | function real, disparada por evento S3 |
| **Postgres** | Postgres local (o cualquier Postgres alcanzable) | Postgres gestionado alcanzable desde AWS (Neon en la demo; RDS en un entorno productivo) |
| **Requiere** | solo Python + Postgres | cuenta de AWS + Terraform (o Docker, para LocalStack) |

La Lambda (`src/processing/lambda_function.py`) **nunca sabe cuál de los dos modos está corriendo** — solo conoce las interfaces. Esto no es un mock ni un workaround para hacer demos: es el mismo patrón puertos-y-adaptadores (arquitectura hexagonal) que se usa en sistemas productivos para lograr paridad dev/prod real y poder testear lógica de negocio sin dependencias externas. `infra/` (Terraform) despliega el modo `aws` contra una cuenta de AWS real por defecto — ver [Modo AWS real (Windows)](#modo-aws-real-windows) más abajo — y también puede apuntar a LocalStack (`docker-compose.yml`) si prefieres seguir sin tocar una cuenta real — en ese caso, `terraform apply -var="use_localstack=true"`.

---

## Decisiones de ingeniería (y por qué)

- **Alertas edge-triggered, no level-triggered.** La alerta SNS se dispara solo la *primera* vez que el puntaje acumulado del período cruza el umbral (`previous_score < threshold <= new_score`), no en cada viaje subsiguiente mientras el conductor siga por encima. La primera versión disparaba una alerta duplicada por cada viaje adicional por encima del umbral — se corrigió comparando el puntaje antes/después de sumar cada viaje. Ver `tests/test_lambda_integration.py::test_alert_fires_exactly_once_when_threshold_is_crossed_across_trips`, que es justamente un test de regresión de ese bug.
- **Puntaje de riesgo con compresión logarítmica**, no una suma lineal de eventos: `100 * (1 - e^(-raw_score/40))`. Así un conductor con 50 eventos no obtiene un puntaje 3x peor que uno con 15 — los rendimientos son decrecientes, como en cualquier score real de riesgo (crédito, fraude, etc.), y el puntaje queda naturalmente acotado 0-100.
- **Cada tipo de evento pesa distinto** (`EVENT_WEIGHTS` en `risk_rules.py`): sobrecalentamiento pesa más que una aceleración agresiva porque el riesgo mecánico/de incendio es objetivamente mayor. Los umbrales y pesos son parámetros explícitos, documentados y fáciles de ajustar según el tipo de vehículo.
- **psycopg2 directo, sin ORM.** Para un pipeline de este tamaño, un ORM agrega una capa de abstracción que hay que explicar y depurar sin aportar mucho — SQL explícito es más fácil de razonar y de justificar en una entrevista técnica.
- **Persistencia poliglota real, no de adorno.** Postgres/RDS guarda el histórico completo (para análisis, auditoría, dashboards); DynamoDB guarda solo el puntaje *actual* por conductor, pensado para lecturas de baja latencia (ej. una futura app móvil para el supervisor) — cada base de datos hace lo que mejor sabe hacer.
- **El simulador y el capturador OBD2 real producen el mismo esquema JSON.** `src/obd2_capture/capture_real.py` (hardware real, vía `python-obd`) y `src/simulator/generate_trips.py` (datos sintéticos) son intercambiables para el resto del pipeline — ingesta, Lambda y dashboard no distinguen el origen de los datos (queda registrado en `source_type` solo para trazabilidad).
- **psycopg2 va en su propio Lambda Layer, no dentro del paquete de la función.** `psycopg2` tiene una extensión en C que necesita estar compilada para Amazon Linux (no para tu laptop) — se descarga como wheel `manylinux2014_x86_64` precompilado, sin compilar nada localmente, y se separa como Layer (ver `infra/main.tf`) siguiendo la práctica que recomienda AWS de no mezclar código propio con dependencias de terceros en el mismo paquete.
- **Neon en vez de RDS para la demo, RDS documentado como la elección de producción.** Evita configurar VPC/subnets/security groups para que la Lambda alcance la base de datos — una Lambda sin VPC ya tiene salida a internet por defecto. Es una decisión de alcance consciente para el plazo disponible, no un desconocimiento de cómo se haría "bien": el propio `infra/main.tf` deja documentado por qué.

---

## Stack tecnológico

| Categoría | Herramienta |
|---|---|
| Lenguaje | Python 3.12 |
| Captura de hardware | [`python-obd`](https://github.com/brendan-w/python-obd) (protocolo ELM327 sobre Bluetooth/USB) |
| Object storage | AWS S3 real (o adaptador local en disco) |
| Cómputo serverless | AWS Lambda real, con Lambda Layer para `psycopg2` |
| Base de datos relacional | Postgres (psycopg2, sin ORM) — Neon en la demo, RDS en producción |
| Base de datos NoSQL | AWS DynamoDB real (o adaptador local JSON) |
| Notificaciones | AWS SNS real (o adaptador local a log) |
| Infraestructura como código | Terraform (`infra/`), contra AWS real por defecto (o LocalStack) |
| Emulación de AWS local (opcional) | LocalStack (`docker-compose.yml`) |
| Dashboard | Streamlit + Plotly |
| Tests | pytest (unitarios + integración end-to-end) |
| CI/CD | GitHub Actions (tests con Postgres real de servicio + `terraform validate`) |

---

## Estructura del repo

```
obd2-fleet-telemetry/
├── src/
│   ├── obd2_capture/     # captura desde hardware OBD2 real
│   ├── simulator/        # generador de viajes sintéticos
│   ├── ingestion/        # sube viajes al object store y dispara el procesamiento
│   ├── processing/       # Lambda: detección de riesgo (risk_rules.py es 100% pura/testeable)
│   ├── db/               # schema.sql de Postgres
│   ├── dashboard/        # Streamlit + Dockerfile
│   └── common/           # config, adaptadores S3/DynamoDB/SNS (storage.py), acceso a datos
├── infra/                # Terraform (AWS real o LocalStack)
├── tests/                # pytest: unitarios + integración end-to-end
├── docker-compose.yml    # LocalStack + Postgres + dashboard, para modo AWS/local con Docker
└── .github/workflows/    # CI: tests + terraform validate
```

---

## Cómo correrlo localmente (sin AWS, sin Docker)

Requisitos: Python 3.12, Postgres corriendo en algún lado (local, Docker, o el `postgres` nativo de tu SO).

```bash
git clone <este-repo>
cd obd2-fleet-telemetry

python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # DEPLOYMENT_MODE=local por defecto — no toques nada más para empezar

# Crea la base de datos y aplica el esquema (ajusta a tu instalación de Postgres)
createdb fleet_telemetry
psql -d fleet_telemetry -f src/db/schema.sql

# 1. Genera viajes simulados para los 4 conductores de ejemplo
python -m src.simulator.generate_trips --all --trips 10

# 2. Súbelos al "object store" (carpeta local) — esto dispara automáticamente
#    la misma Lambda que se dispararía por un evento real de S3
python -m src.ingestion.upload_to_s3 --all-pending

# 3. Levanta el dashboard
streamlit run src/dashboard/app.py
```

Abre `http://localhost:8501` y deberías ver el ranking de riesgo por conductor poblado.

### Usando tu OBD2 real en vez del simulador

```bash
python -m src.obd2_capture.capture_real --port /dev/rfcomm0 --driver DRV-001 --driver-name "Tu Nombre" --truck TRK-01 --duration 20
python -m src.ingestion.upload_to_s3 --all-pending
```

El OBD2 no entrega GPS — si necesitas distancia recorrida con datos reales, empareja un receptor GPS aparte o usa la estimación por velocidad×tiempo que ya incluye `lambda_function.py` cuando no hay coordenadas.

---

## Modo AWS real (Windows)

Esta es la versión de la demo pensada para mostrar en una entrevista: se despliega de verdad contra una cuenta de AWS (capa gratuita), no una simulación. `infra/` funciona contra AWS real por defecto — no hay que tocar el Terraform, solo darle credenciales reales y una base de datos alcanzable desde internet.

**Por qué Postgres no es una RDS acá:** RDS es la elección "de libro" para este proyecto (y así queda documentado como la opción de producción), pero requiere configurar VPC, subnets y security groups para que la Lambda pueda alcanzarla — y la capa gratuita de RDS tiene reglas más enredadas que el resto de los servicios (revisa el storage tipo *gp2* al crearla, o AWS te cobra igual aunque diga "free tier eligible"). Para una demo con 2 semanas de plazo, usamos **[Neon](https://neon.tech)** (Postgres gestionado, gratis, sin tarjeta) en su lugar: cero configuración de red, porque una Lambda sin VPC asignada ya sale a internet por defecto. Esto es una decisión de ingeniería real y defendible — puedes explicarla así en la entrevista.

### 1. Instala lo necesario en Windows

- **Python 3.12**: [python.org/downloads](https://www.python.org/downloads/) (marca "Add python.exe to PATH" en el instalador).
- **AWS CLI v2**: [descarga el instalador MSI](https://awscli.amazonaws.com/AWSCLIV2.msi) y ábrelo.
- **Terraform**: descarga el .zip de [terraform.io/downloads](https://developer.hashicorp.com/terraform/install), descomprime `terraform.exe` en una carpeta (ej. `C:\terraform\`) y agrega esa carpeta al PATH (Panel de Control → Variables de entorno).
- **Git** (si aún no lo tienes): [git-scm.com](https://git-scm.com/download/win).

Verifica que todo quedó instalado (PowerShell):
```powershell
python --version
aws --version
terraform -version
```

### 2. Crea la cuenta de AWS y configura tus credenciales

1. Crea la cuenta en [aws.amazon.com](https://aws.amazon.com) (pide una tarjeta para verificar identidad, es normal — no te cobra mientras te quedes en la capa gratuita).
2. Entra a la consola → busca **IAM** → crea un usuario para ti (no uses la cuenta root para esto) → dale la policy `AdministratorAccess` (para una demo de 2 semanas está bien; en un proyecto real sería un rol acotado) → genera unas **Access Keys**.
3. En PowerShell:
   ```powershell
   aws configure
   # Te va a pedir: Access Key ID, Secret Access Key, región (usa us-east-1), formato (json)
   ```
4. Confirma que quedó bien conectado:
   ```powershell
   aws sts get-caller-identity
   ```
   Si te devuelve tu Account ID sin error, ya estás listo.

### 3. Crea la base de datos gratis en Neon

1. Entra a [neon.tech](https://neon.tech) y crea una cuenta (gratis, sin tarjeta).
2. Crea un proyecto nuevo, base de datos `fleet_telemetry`.
3. Copia el **connection string** que te dan (formato `postgresql://usuario:password@ep-xxxx.aws.neon.tech/fleet_telemetry?sslmode=require`).
4. Aplícale el esquema desde tu PC (necesitas `psql`, que viene con el instalador de PostgreSQL para Windows si no lo tienes: [postgresql.org/download/windows](https://www.postgresql.org/download/windows/) — solo necesitas las "Command Line Tools", no el servidor):
   ```powershell
   psql "postgresql://usuario:password@ep-xxxx.aws.neon.tech/fleet_telemetry?sslmode=require" -f src/db/schema.sql
   ```

### 4. Despliega la infraestructura con Terraform

```powershell
cd infra
copy terraform.tfvars.example terraform.tfvars
notepad terraform.tfvars   # pega tu connection string de Neon y un project_name único (ver el propio archivo)

# El Lambda Layer de psycopg2 ya viene pre-armado en este repo (infra/layer/).
# Si alguna vez necesitas regenerarlo, el comando está documentado en infra/main.tf.

terraform init
terraform apply     # revisa el plan, escribe "yes"
```

Esto crea, en tu cuenta real de AWS: el bucket S3, la tabla DynamoDB, el tópico SNS, el rol IAM, el Lambda Layer con psycopg2, la función Lambda y su trigger desde S3. `terraform apply` te muestra la lista completa de recursos que va a crear antes de pedirte confirmación — léela, es exactamente lo que vas a ver después en la consola de AWS.

### 5. Prueba el pipeline completo contra AWS real

```powershell
cd ..
copy .env .env.local.bak   # respaldo de tu .env local, por si acaso
# Edita .env: DEPLOYMENT_MODE=aws, y DATABASE_URL apuntando a tu Neon

python -m src.simulator.generate_trips --all --trips 5
python -m src.ingestion.upload_to_s3 --all-pending
```

Esto sube los archivos a tu bucket S3 real — dispara automáticamente tu Lambda real — que escribe en tu Neon real y en tu DynamoDB real, y publica en tu SNS real si algún conductor cruza el umbral. Para la entrevista, ten abiertas de antemano estas pantallas de la consola de AWS: **S3** (mostrando los archivos subidos), **Lambda → Monitor → CloudWatch Logs** (mostrando cada invocación real, con el `print()` que ya tiene `lambda_function.py`), y **DynamoDB** (mostrando la tabla con el puntaje actual por conductor). El dashboard de Streamlit sigue funcionando igual, apuntando ahora a tu Neon en vez de tu Postgres local.

### 6. Cuando termines de mostrar la demo

Aunque S3, Lambda, DynamoDB y SNS son gratis indefinidamente a este nivel de uso, es buena práctica no dejar recursos de nube dando vueltas sin necesidad:

```powershell
cd infra
terraform destroy
```

Puedes volver a crear todo con `terraform apply` las veces que quieras antes de la entrevista para practicar — el estado queda guardado en `infra/terraform.tfstate` (no se sube a git).

---

## Testing y CI

```bash
pytest -v
```

- `tests/test_risk_rules.py` — lógica de detección y scoring, sin dependencias externas.
- `tests/test_storage.py` — adaptadores locales de S3/DynamoDB/SNS.
- `tests/test_simulator.py` — el generador de datos respeta el contrato/esquema que espera el resto del pipeline.
- `tests/test_lambda_integration.py` — pipeline end-to-end real (object store → Lambda → Postgres), incluyendo el test de regresión de alertas duplicadas. Se salta automáticamente si no hay Postgres disponible en `DATABASE_URL`.

GitHub Actions (`.github/workflows/ci.yml`) corre todo lo anterior en cada push, levantando un Postgres real como servicio, más un job separado de `terraform validate` / `terraform fmt` sobre `infra/`.

---

## Limitaciones conocidas y alcance

- Postgres no se despliega como RDS vía Terraform — se usa un Postgres gestionado externo (Neon) para evitar configurar VPC/subnets/security groups en el tiempo disponible. Es una decisión de alcance explícita, no un olvido (ver la sección de modo AWS real más arriba); en un entorno productivo real, la elección natural sería RDS en una VPC privada.
- El GPS del OBD2 no existe como PID estándar; la distancia se calcula por GPS cuando hay datos simulados, o por velocidad × tiempo cuando la captura es real y no viene de un receptor GPS aparte.
- El período de agregación de riesgo es "por día" (`current_period_bounds()` en `src/common/db.py`) — cambiar a semanal es una línea de código.

## Con más tiempo / roadmap

- Reemplazar el polling del dashboard por WebSockets o Server-Sent Events para verdadero tiempo real.
- Notificaciones de SNS a email/Slack real (ya está el punto de extensión: `AlertPublisher`).
- Un modelo de ML simple (ej. isolation forest) sobre las series de tiempo para detectar anomalías que las reglas fijas no capturan.
- Panel comparativo histórico (semana vs. semana) por conductor.

---

## Autor

Nicolás Alarcón Ferrus — proyecto de portafolio para postulaciones a Data Engineer.
