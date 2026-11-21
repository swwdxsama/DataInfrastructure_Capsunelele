"""Teste pentru GET /devices din ingest.py, cu un tabel DynamoDB fals."""
import json
from decimal import Decimal

import ingest
import pytest


class FakeScanTable:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return {"Items": list(self.items)}


def get_devices():
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/devices"}
    resp = ingest.lambda_handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def state_table(monkeypatch):
    table = FakeScanTable(
        [
            {
                "device_id": "p-02",
                "state": "UNARMED",
                "static_sec": Decimal("6"),
                "disarm_reason": "TILT_CHANGE",
                "last_t_ms": Decimal("1700000000000"),
                "updated_at_ms": Decimal("1700000000500"),
            },
            {
                "device_id": "p-01",
                "state": "ARMED",
                "static_sec": Decimal("42"),
                "disarm_reason": "",
                "last_t_ms": Decimal("1700000001000"),
                "updated_at_ms": Decimal("1700000001200"),
            },
        ]
    )
    monkeypatch.setattr(ingest, "_get_state_table", lambda: table)
    monkeypatch.setenv("ARMING_SEC", "15")
    return table


def test_lists_every_device_with_its_state(state_table):
    status, body = get_devices()
    assert status == 200
    assert [d["device_id"] for d in body["devices"]] == ["p-01", "p-02"]

    armed, unarmed = body["devices"]
    assert armed["state"] == "ARMED"
    assert armed["disarm_reason"] is None
    assert unarmed == {
        "device_id": "p-02",
        "state": "UNARMED",
        "static_sec": 6,
        "arming_sec": 15,
        "disarm_reason": "TILT_CHANGE",
        "last_t_ms": 1700000000000,
        "updated_at_ms": 1700000000500,
        "updated_at": "2023-11-14T22:13:20.500Z",
    }


def test_projection_aliases_reserved_words(state_table):
    get_devices()
    names = state_table.calls[0]["ExpressionAttributeNames"]
    assert "state" in names.values()
    assert "state" not in state_table.calls[0]["ProjectionExpression"]


def test_falls_back_to_hidden_state_rows(monkeypatch):
    events = FakeScanTable(
        [
            {"device_id": "p-01", "state": "ARMED", "updated_at_ms": Decimal("1")},
            {"device_id": "p-01"},  # a projected telemetry row, not a state record
        ]
    )
    monkeypatch.setattr(ingest, "_get_state_table", lambda: None)
    monkeypatch.setattr(ingest, "_get_events_table", lambda: events)
    status, body = get_devices()
    assert status == 200
    assert [d["device_id"] for d in body["devices"]] == ["p-01"]
