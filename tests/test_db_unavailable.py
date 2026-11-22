"""GET-urile întorc 503 db_unavailable când DynamoDB nu răspunde."""
import json

import ingest
import pytest


class FailingTable:
    def scan(self, **kwargs):
        raise ingest.ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "table missing"}}, "Scan"
        )

    def query(self, **kwargs):
        return self.scan()


def get(path):
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": path}
    resp = ingest.lambda_handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def db_down(monkeypatch):
    table = FailingTable()
    monkeypatch.setattr(ingest, "_get_events_table", lambda: table)
    monkeypatch.setattr(ingest, "_get_alerts_table", lambda: table)
    monkeypatch.setattr(ingest, "_get_state_table", lambda: table)


@pytest.mark.parametrize("path", ["/events", "/alerts", "/devices"])
def test_get_returns_503_when_dynamodb_fails(db_down, path):
    assert get(path) == (503, {"error": "db_unavailable"})


def test_other_errors_stay_500(monkeypatch):
    def boom():
        raise ValueError("bug")

    monkeypatch.setitem(ingest.GET_ROUTES, "/events", boom)
    status, body = get("/events")
    assert status == 500
    assert body == {"error": "bug"}
