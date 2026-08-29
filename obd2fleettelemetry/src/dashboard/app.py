"""
Dashboard de telemetría de flota — Streamlit.

Reemplaza a QuickSight (mencionado en la propuesta original del proyecto)
por algo que corre 100% local, sin necesitar una cuenta de AWS: lee
directamente de Postgres ("RDS") y se actualiza cada vez que se re-ejecuta
la ingesta.

Uso:
    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite ejecutar "streamlit run src/dashboard/app.py" desde la raíz del
# repo sin tener que instalar el proyecto como paquete.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine

from src.common.config import settings

st.set_page_config(
    page_title="Fleet Risk Telemetry — OBD2",
    page_icon="🚚",
    layout="wide",
)

REFRESH_SECONDS = 15


@st.cache_resource
def get_engine():
    return create_engine(settings.database_url)


@st.cache_data(ttl=REFRESH_SECONDS)
def load_driver_scores() -> pd.DataFrame:
    query = """
        SELECT s.driver_id, d.full_name, d.truck_id, s.total_events,
               s.harsh_braking_count, s.aggressive_accel_count,
               s.excessive_rpm_count, s.excessive_temp_count,
               s.risk_score, s.last_updated
        FROM driver_risk_scores s
        JOIN drivers d ON d.driver_id = s.driver_id
        WHERE s.period_end > now()
        ORDER BY s.risk_score DESC
    """
    return pd.read_sql(query, get_engine())


@st.cache_data(ttl=REFRESH_SECONDS)
def load_trips() -> pd.DataFrame:
    query = """
        SELECT t.trip_id, t.driver_id, d.full_name, t.truck_id, t.source_type,
               t.start_time, t.end_time, t.distance_km, t.avg_speed_kmh,
               t.max_rpm, t.max_engine_temp, t.processed_at
        FROM trips t
        JOIN drivers d ON d.driver_id = t.driver_id
        ORDER BY t.start_time DESC
    """
    return pd.read_sql(query, get_engine())


@st.cache_data(ttl=REFRESH_SECONDS)
def load_risk_events() -> pd.DataFrame:
    query = """
        SELECT e.event_id, e.trip_id, e.driver_id, d.full_name, e.event_type,
               e.severity, e.event_time, e.value, e.threshold
        FROM risk_events e
        JOIN drivers d ON d.driver_id = e.driver_id
        ORDER BY e.event_time DESC
        LIMIT 500
    """
    return pd.read_sql(query, get_engine())


@st.cache_data(ttl=REFRESH_SECONDS)
def load_alerts() -> pd.DataFrame:
    query = """
        SELECT a.alert_id, a.driver_id, d.full_name, a.risk_score, a.threshold,
               a.message, a.triggered_at
        FROM alerts a
        JOIN drivers d ON d.driver_id = a.driver_id
        ORDER BY a.triggered_at DESC
    """
    return pd.read_sql(query, get_engine())


def risk_color(score: float) -> str:
    if score >= 70:
        return "#ef5a5a"
    if score >= 40:
        return "#f5a623"
    return "#3ecf8e"


# --------------------------------------------------------------------------
st.title("🚚 Fleet Risk Telemetry — Detección de Conducción Riesgosa")
st.caption(
    "Pipeline OBD2 → Object Store (S3) → Procesamiento (Lambda) → Base de datos (RDS) → Alertas (SNS) → este dashboard. "
    f"Modo de despliegue actual: **{settings.deployment_mode}**."
)

try:
    scores_df = load_driver_scores()
    trips_df = load_trips()
    events_df = load_risk_events()
    alerts_df = load_alerts()
except Exception as exc:
    st.error(
        "No se pudo conectar a la base de datos. Verifica que Postgres esté "
        f"corriendo y que DATABASE_URL sea correcto.\n\nDetalle: {exc}"
    )
    st.stop()

# -- KPIs --------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Conductores activos hoy", len(scores_df))
col2.metric("Viajes procesados", len(trips_df))
col3.metric("Eventos de riesgo (recientes)", len(events_df))
col4.metric("Alertas disparadas", len(alerts_df))

st.divider()

# -- Ranking de riesgo por conductor -----------------------------------------
left, right = st.columns([2, 1])

with left:
    st.subheader("Ranking de riesgo por conductor (hoy)")
    if scores_df.empty:
        st.info("Aún no hay viajes procesados hoy. Corre el simulador y la ingesta primero.")
    else:
        fig = px.bar(
            scores_df.sort_values("risk_score"),
            x="risk_score", y="full_name", orientation="h",
            color="risk_score", color_continuous_scale=["#3ecf8e", "#f5a623", "#ef5a5a"],
            range_color=[0, 100],
            labels={"risk_score": "Puntaje de riesgo", "full_name": "Conductor"},
            text="risk_score",
        )
        fig.update_layout(showlegend=False, height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Desglose de eventos por tipo")
    if not events_df.empty:
        counts = events_df["event_type"].value_counts().reset_index()
        counts.columns = ["event_type", "count"]
        fig2 = px.pie(counts, names="event_type", values="count", hole=0.5)
        fig2.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Sin eventos aún.")

st.divider()

# -- Tabla de conductores con formato semáforo -------------------------------
st.subheader("Detalle por conductor")
if not scores_df.empty:
    display_df = scores_df[[
        "full_name", "truck_id", "risk_score", "total_events",
        "harsh_braking_count", "aggressive_accel_count",
        "excessive_rpm_count", "excessive_temp_count", "last_updated",
    ]].rename(columns={
        "full_name": "Conductor", "truck_id": "Camión", "risk_score": "Riesgo",
        "total_events": "Eventos totales", "harsh_braking_count": "Frenadas bruscas",
        "aggressive_accel_count": "Aceleraciones agresivas",
        "excessive_rpm_count": "RPM excesivo", "excessive_temp_count": "Temp. excesiva",
        "last_updated": "Última actualización",
    })
    styler = display_df.style
    color_fn = lambda v: f"background-color: {risk_color(float(v))}55" if pd.notnull(v) else ""
    # pandas >= 2.1 renombró Styler.applymap -> Styler.map (applymap quedó
    # deprecado y en pandas 3.x fue eliminado); soportamos ambas versiones.
    styler = styler.map(color_fn, subset=["Riesgo"]) if hasattr(styler, "map") else styler.applymap(color_fn, subset=["Riesgo"])
    st.dataframe(styler, use_container_width=True, hide_index=True)

st.divider()

# -- Alertas recientes --------------------------------------------------------
st.subheader("🚨 Alertas recientes")
if alerts_df.empty:
    st.info("No se han disparado alertas todavía.")
else:
    for _, row in alerts_df.head(10).iterrows():
        st.warning(f"**{row['triggered_at']:%Y-%m-%d %H:%M}** — {row['message']}")

st.divider()

# -- Explorador de viajes ------------------------------------------------------
st.subheader("Explorador de viajes")
if trips_df.empty:
    st.info("Sin viajes procesados todavía.")
else:
    driver_filter = st.selectbox(
        "Filtrar por conductor", ["Todos"] + sorted(trips_df["full_name"].unique().tolist())
    )
    filtered = trips_df if driver_filter == "Todos" else trips_df[trips_df["full_name"] == driver_filter]
    st.dataframe(
        filtered[[
            "trip_id", "full_name", "truck_id", "source_type", "start_time",
            "distance_km", "avg_speed_kmh", "max_rpm", "max_engine_temp",
        ]].rename(columns={
            "trip_id": "Viaje", "full_name": "Conductor", "truck_id": "Camión",
            "source_type": "Origen", "start_time": "Inicio", "distance_km": "Distancia (km)",
            "avg_speed_kmh": "Vel. promedio", "max_rpm": "RPM máx", "max_engine_temp": "Temp. máx",
        }),
        use_container_width=True, hide_index=True,
    )

st.caption(f"Se refresca automáticamente cada {REFRESH_SECONDS}s (cache de Streamlit). Recarga la página para forzar.")
