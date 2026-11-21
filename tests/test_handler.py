"""Teste handler.py cu un DynamoDB fals (fara boto3 / AWS)."""
import copy
import json
import sys
import types

import pytest

from batches import EXAMPLE, T0, make_batch


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        k = (Item["device_id"], Item["t0"])
        if ConditionExpression and k in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        self.items[k] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        it = self.items[(Key["device_id"], Key["t0"])]
        it["status"] = ExpressionAttributeValues[":s"]
        if ":r" in ExpressionAttributeValues:
            it["reason"] = ExpressionAttributeValues[":r"]


_exc = types.ModuleType("botocore.exceptions")
_exc.ClientError = FakeClientError
sys.modules.setdefault("botocore", types.ModuleType("botocore"))
sys.modules["botocore.exceptions"] = _exc

import handler  # noqa: E402


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    raw = FakeTable()
    monkeypatch.setattr(handler, "_raw_table", raw)
    monkeypatch.setattr(handler, "PREPROCESS_ENABLED", False)
    return raw


def post(body):
    raw = body if isinstance(body, str) else json.dumps(body)
    return handler.handler({"requestContext": {"http": {"method": "POST"}}, "body": raw}, None)


def status_of(resp):
    return resp["statusCode"], json.loads(resp["body"]).get("status")


def test_store_then_duplicate_returns_200(fake_db):
    p = make_batch()
    assert status_of(post(p)) == (200, "stored")
    assert status_of(post(p)) == (200, "duplicate")
    assert len(fake_db.items) == 1
    assert json.loads(fake_db.items[("ios-01", T0)]["raw"]) == p


def test_legacy_app_payload_is_stored(fake_db):
    legacy = {
        "device_id": "android-42", "event_type": "raw_sensor_batch", "value": 9.8,
        "metadata": {"platform": "android", "location": None,
                     "accelerometer": [{"t": T0 + 7, "x": 0, "y": 0, "z": 9.81}], "gyroscope": []},
    }
    assert post(legacy)["statusCode"] == 200
    assert ("android-42", T0 + 7) in fake_db.items


@pytest.mark.parametrize("body", ["{nope", "[1, 2]", {"device_id": "x"}])
def test_bad_payload_is_400(body):
    assert post(body)["statusCode"] == 400


def test_preprocess_rejection_is_400_and_saved(fake_db, monkeypatch):
    monkeypatch.setattr(handler, "PREPROCESS_ENABLED", True)
    p = copy.deepcopy(EXAMPLE)
    p["sent_at"] = int(handler.time.time() * 1000)
    p.pop("timestamp")
    resp = post(p)  # 3 esantioane -> BATCH_TOO_SHORT
    assert resp["statusCode"] == 400
    assert fake_db.items[("ios-01", T0)]["reason"] == "BATCH_TOO_SHORT"


def test_db_error_is_500_so_phone_retries(fake_db, monkeypatch):
    def boom(**_):
        raise FakeClientError("ProvisionedThroughputExceededException")
    monkeypatch.setattr(fake_db, "put_item", boom)
    assert post(make_batch())["statusCode"] == 500
