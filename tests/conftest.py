"""Fixtures compartidas. Ver también pytest.ini para la config de paths."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_local_data_root(tmp_path, monkeypatch):
    """Redirige LOCAL_DATA_ROOT (usado por los adaptadores 'local' de
    src/common/storage.py) a un directorio temporal por test, para que los
    tests nunca lean ni escriban en data/ del repo real."""
    import src.common.storage as storage_module

    monkeypatch.setattr(storage_module, "LOCAL_DATA_ROOT", tmp_path / "local_aws")
    yield
