"""Fixtures compartidas. Ver también pytest.ini para la config de paths."""
from __future__ import annotations

import os

# Los tests SIEMPRE deben correr en modo "local" (adaptadores en disco/log,
# sin llamadas de red a AWS real), sin importar qué diga tu .env — igual
# que en CI (.github/workflows/ci.yml), que fija DEPLOYMENT_MODE=local
# directamente como variable de entorno, nunca vía .env. Esto se hace ANTES
# de cualquier import de src.common.config (que carga .env con
# load_dotenv()): dotenv no sobreescribe una variable que ya existe en el
# entorno, así que fijarla acá primero gana siempre. Sin esto, si tu .env
# tiene DEPLOYMENT_MODE=aws (ej. mientras trabajas la demo contra AWS
# real), los tests de integración terminarían llamando a S3/DynamoDB/SNS
# reales en vez de a los adaptadores locales — y fallarían con errores como
# "NoSuchKey", en vez de simplemente probar la lógica de negocio.
os.environ["DEPLOYMENT_MODE"] = "local"

import pytest


@pytest.fixture(autouse=True)
def _isolated_local_data_root(tmp_path, monkeypatch):
    """Redirige LOCAL_DATA_ROOT (usado por los adaptadores 'local' de
    src/common/storage.py) a un directorio temporal por test, para que los
    tests nunca lean ni escriban en data/ del repo real."""
    import src.common.storage as storage_module

    monkeypatch.setattr(storage_module, "LOCAL_DATA_ROOT", tmp_path / "local_aws")
    yield
