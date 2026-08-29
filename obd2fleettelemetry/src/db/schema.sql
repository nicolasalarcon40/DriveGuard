-- Esquema de la base de datos de telemetría de flota.
-- Se ejecuta automáticamente al levantar el contenedor de Postgres (docker-entrypoint-initdb.d).

CREATE TABLE IF NOT EXISTS drivers (
    driver_id       TEXT PRIMARY KEY,
    full_name       TEXT NOT NULL,
    truck_id        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id         TEXT PRIMARY KEY,
    driver_id       TEXT NOT NULL REFERENCES drivers(driver_id),
    truck_id        TEXT NOT NULL,
    source_file     TEXT NOT NULL,          -- key de S3 del archivo de origen (simulado o real)
    source_type     TEXT NOT NULL DEFAULT 'simulated', -- 'simulated' | 'obd2_real'
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ,
    distance_km     NUMERIC(10,2),
    avg_speed_kmh   NUMERIC(6,2),
    max_rpm         INTEGER,
    max_engine_temp NUMERIC(6,2),
    processed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trips_driver ON trips(driver_id);
CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id        BIGSERIAL PRIMARY KEY,
    trip_id         TEXT NOT NULL REFERENCES trips(trip_id),
    driver_id       TEXT NOT NULL REFERENCES drivers(driver_id),
    event_type      TEXT NOT NULL,   -- harsh_braking | aggressive_acceleration | excessive_rpm | excessive_temp
    severity        TEXT NOT NULL,   -- low | medium | high
    event_time      TIMESTAMPTZ NOT NULL,
    value            NUMERIC(10,3),  -- valor que disparó el evento (ej. deceleración en m/s2)
    threshold        NUMERIC(10,3),  -- umbral configurado para ese tipo de evento
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_risk_events_driver ON risk_events(driver_id);
CREATE INDEX IF NOT EXISTS idx_risk_events_trip ON risk_events(trip_id);
CREATE INDEX IF NOT EXISTS idx_risk_events_type ON risk_events(event_type);

CREATE TABLE IF NOT EXISTS driver_risk_scores (
    driver_id       TEXT NOT NULL REFERENCES drivers(driver_id),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    total_events    INTEGER NOT NULL DEFAULT 0,
    harsh_braking_count         INTEGER NOT NULL DEFAULT 0,
    aggressive_accel_count      INTEGER NOT NULL DEFAULT 0,
    excessive_rpm_count         INTEGER NOT NULL DEFAULT 0,
    excessive_temp_count        INTEGER NOT NULL DEFAULT 0,
    risk_score      NUMERIC(6,2) NOT NULL DEFAULT 0,  -- 0-100
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (driver_id, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id        BIGSERIAL PRIMARY KEY,
    driver_id       TEXT NOT NULL REFERENCES drivers(driver_id),
    risk_score      NUMERIC(6,2) NOT NULL,
    threshold       NUMERIC(6,2) NOT NULL,
    message         TEXT NOT NULL,
    sns_message_id  TEXT,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Datos semilla: un par de conductores/camiones de ejemplo para poder
-- correr el simulador y el dashboard de inmediato sin configurar nada más.
INSERT INTO drivers (driver_id, full_name, truck_id) VALUES
    ('DRV-001', 'Juan Perez',     'TRK-01'),
    ('DRV-002', 'Maria Gonzalez', 'TRK-02'),
    ('DRV-003', 'Carlos Rojas',   'TRK-03'),
    ('DRV-004', 'Nicolas A.',     'TRK-04')
ON CONFLICT (driver_id) DO NOTHING;
