"""Lambda handler: ingestie batch-uri de senzori.

Etapa curenta (task 1): salveaza batch-ul brut + deduplicare dupa (device_id, t0).
Daca PREPROCESS_ENABLED=1, ruleaza si preprocess (pasul 2); la respingere -> 400.
Pasii 3-6 (last_seen, process, correlate) vin dupa ce exista process.py / correlate.py.

Toata comunicarea cu DynamoDB se face aici; preprocess.py e pur.
"""
import base64
import json
import os
import time

from preprocess import preprocess

RAW_TABLE_NAME = os.environ.get("RAW_TABLE_NAME", "RawBatches")
PREPROCESS_ENABLED = os.environ.get("PREPROCESS_ENABLED", "0") == "1"

_raw_table = None  # creat o singura data per container (refolosit intre apeluri)


def _table():
    global _raw_table
    if _raw_table is None:
        import boto3
        _raw_table = boto3.resource("dynamodb").Table(RAW_TABLE_NAME)
    return _raw_table


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def extract_key(body):
    """(device_id, t0_ms) din payload v1; fallback pentru formatul vechi al App.tsx
    (metadata.accelerometer = [{t, x, y, z}] cu t absolut) ca sa putem colecta date reale."""
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return None
    acc = body.get("accelerometer")
    if isinstance(acc, dict) and _is_num(acc.get("t0")):
        return device_id, int(acc["t0"])
    meta = body.get("metadata")
    if isinstance(meta, dict):
        samples = meta.get("accelerometer")
        if isinstance(samples, list) and samples and isinstance(samples[0], dict) and _is_num(samples[0].get("t")):
            return device_id, int(samples[0]["t"])
    return None


def _save_raw(device_id, t0, received_at, raw_text, body):
    """True = salvat, False = exista deja (retry)."""
    from botocore.exceptions import ClientError
    item = {
        "device_id": device_id,
        "t0": t0,
        "received_at": received_at,
        "schema_version": body.get("schema_version", 0) if _is_num(body.get("schema_version")) else 0,
        "status": "received",
        "raw": raw_text,
    }
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(device_id)")
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _mark(device_id, t0, status, reason=None, detail=None):
    names = {"#s": "status"}
    values = {":s": status}
    expr = "SET #s = :s"
    if reason:
        expr += ", reason = :r, detail = :d"
        values.update({":r": reason, ":d": detail or ""})
    _table().update_item(
        Key={"device_id": device_id, "t0": t0},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _handle_post(event):
    received_at = int(time.time() * 1000)
    raw_text = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_text = base64.b64decode(raw_text).decode("utf-8")
    try:
        body = json.loads(raw_text)
    except ValueError:
        return _response(400, {"status": "rejected", "reason": "SCHEMA_INVALID", "detail": "invalid JSON"})
    if not isinstance(body, dict):
        return _response(400, {"status": "rejected", "reason": "SCHEMA_INVALID", "detail": "payload must be an object"})

    key = extract_key(body)
    if key is None:
        return _response(400, {"status": "rejected", "reason": "SCHEMA_INVALID",
                               "detail": "missing device_id or accelerometer.t0"})
    device_id, t0 = key

    # 1. Batch brut + deduplicare
    if not _save_raw(device_id, t0, received_at, raw_text, body):
        return _response(200, {"status": "duplicate", "device_id": device_id, "t0": t0})

    if not PREPROCESS_ENABLED:
        return _response(200, {"status": "stored", "device_id": device_id, "t0": t0})

    # 2. Preprocesare
    clean = preprocess(body, received_at)
    if clean["status"] != "ok":
        _mark(device_id, t0, "rejected", clean["reason"], clean["detail"])
        return _response(400, {"status": "rejected", "reason": clean["reason"], "detail": clean["detail"]})
    _mark(device_id, t0, "preprocessed")

    # 3-6: TODO (last_seen, istoric trasaturi, process, correlate)
    return _response(200, {"status": "ok", "device_id": device_id, "t0": t0, "warnings": clean["warnings"]})


def _handle_get(event):
    res = _table().scan(Limit=50, ProjectionExpression="device_id, t0, received_at, #s, reason",
                        ExpressionAttributeNames={"#s": "status"})
    return _response(200, {"batches": res.get("Items", [])})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    try:
        if method == "POST":
            return _handle_post(event)
        if method == "GET":
            return _handle_get(event)
        return _response(405, {"error": "Method not allowed"})
    except Exception as e:  # 5xx -> telefonul reincearca cu acelasi payload
        print("ERROR", repr(e))
        return _response(500, {"error": str(e)})
