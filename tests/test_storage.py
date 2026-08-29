"""
Tests de los adaptadores "local" del patrón puertos-y-adaptadores
(src/common/storage.py) — la pieza que reemplaza a S3/DynamoDB/SNS reales
cuando DEPLOYMENT_MODE=local. Los adaptadores "aws" (boto3 real) no se
prueban acá: se ejercitan en un entorno con Docker/LocalStack o AWS real
(ver docker-compose.yml y infra/), no en este sandbox de desarrollo.
"""
from __future__ import annotations

import pytest

from src.common.storage import LocalAlertPublisher, LocalKeyValueStore, LocalObjectStore


# --------------------------------------------------------------------------
# LocalObjectStore ("S3")
# --------------------------------------------------------------------------
def test_object_store_roundtrip():
    store = LocalObjectStore("test-bucket")
    store.put_object("trips/DRV-001/2026-08-19/TRP-abc.json", b'{"hello": "world"}')

    data = store.get_object("trips/DRV-001/2026-08-19/TRP-abc.json")
    assert data == b'{"hello": "world"}'


def test_object_store_creates_nested_keys_like_a_real_bucket():
    store = LocalObjectStore("test-bucket")
    store.put_object("a/b/c/d.json", b"x")
    assert store.get_object("a/b/c/d.json") == b"x"


def test_object_store_get_missing_key_raises():
    store = LocalObjectStore("test-bucket")
    with pytest.raises(FileNotFoundError):
        store.get_object("does/not/exist.json")


def test_object_store_list_objects_filters_by_prefix():
    store = LocalObjectStore("test-bucket")
    store.put_object("trips/DRV-001/a.json", b"1")
    store.put_object("trips/DRV-001/b.json", b"2")
    store.put_object("trips/DRV-002/c.json", b"3")

    keys = store.list_objects("trips/DRV-001")
    assert sorted(keys) == ["trips/DRV-001/a.json", "trips/DRV-001/b.json"]


def test_object_store_list_objects_empty_prefix_returns_everything():
    store = LocalObjectStore("test-bucket")
    store.put_object("x.json", b"1")
    store.put_object("y.json", b"2")
    assert sorted(store.list_objects()) == ["x.json", "y.json"]


def test_two_buckets_are_isolated():
    a = LocalObjectStore("bucket-a")
    b = LocalObjectStore("bucket-b")
    a.put_object("k.json", b"from-a")
    with pytest.raises(FileNotFoundError):
        b.get_object("k.json")


# --------------------------------------------------------------------------
# LocalKeyValueStore ("DynamoDB")
# --------------------------------------------------------------------------
def test_kv_store_put_and_get():
    kv = LocalKeyValueStore("driver-risk-current")
    kv.put_item("DRV-001", {"risk_score": "42.0", "total_events": 3})

    item = kv.get_item("DRV-001")
    assert item == {"risk_score": "42.0", "total_events": 3}


def test_kv_store_get_missing_key_returns_none():
    kv = LocalKeyValueStore("driver-risk-current")
    assert kv.get_item("DRV-999") is None


def test_kv_store_put_overwrites_existing_item():
    kv = LocalKeyValueStore("driver-risk-current")
    kv.put_item("DRV-001", {"risk_score": "10.0"})
    kv.put_item("DRV-001", {"risk_score": "80.0"})
    assert kv.get_item("DRV-001")["risk_score"] == "80.0"


def test_kv_store_persists_across_instances():
    """Simula dos invocaciones separadas de la Lambda (cada una crea su
    propia instancia del adaptador) leyendo/escribiendo el mismo 'table'."""
    LocalKeyValueStore("driver-risk-current").put_item("DRV-001", {"risk_score": "55.0"})
    reopened = LocalKeyValueStore("driver-risk-current")
    assert reopened.get_item("DRV-001")["risk_score"] == "55.0"


# --------------------------------------------------------------------------
# LocalAlertPublisher ("SNS")
# --------------------------------------------------------------------------
def test_alert_publisher_returns_a_message_id():
    pub = LocalAlertPublisher()
    msg_id = pub.publish("Alerta de prueba", "mensaje de prueba")
    assert msg_id is not None
    assert msg_id.startswith("local-")


def test_alert_publisher_appends_to_log_file():
    pub = LocalAlertPublisher()
    pub.publish("Alerta 1", "mensaje 1")
    pub.publish("Alerta 2", "mensaje 2")

    log_contents = pub.path.read_text()
    assert "Alerta 1" in log_contents
    assert "Alerta 2" in log_contents
    assert log_contents.count("\n") == 2
