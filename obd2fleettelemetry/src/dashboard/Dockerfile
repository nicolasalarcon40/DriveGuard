# Imagen del dashboard de Streamlit.
#
# El contexto de build es la raíz del repo (ver docker-compose.yml:
# `build.context: .`, `build.dockerfile: src/dashboard/Dockerfile`) porque
# app.py importa `src.common.config` — necesita el paquete `src/` completo,
# no solo esta carpeta.
FROM python:3.12-slim

WORKDIR /app

# Solo copiamos requirements primero para aprovechar la cache de capas de
# Docker: si el código cambia pero no las dependencias, este paso no se
# vuelve a ejecutar.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8501

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

CMD ["streamlit", "run", "src/dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
