"""Thin AWS Lambda handler for seismic telemetry ingestion, events, and alerts."""

import base64
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from correlate import correlate_triggers
from preprocess import preprocess
from process import process_clean_batch

try:
    from boto3.dynamodb.conditions import Key
except ImportError:
    Key = None

try:
    from botocore.exceptions import ClientError
except ImportError:
    class ClientError(Exception):
        def __init__(self, error_response=None, operation_name=""):
            super().__init__(str(error_response))
            self.response = error_response or {}


_events_table = None
_alerts_table = None
_raw_table = None
_sns_client = None


def _get_events_table():
    global _events_table
    if _events_table is None:
        import boto3
        _events_table = boto3.resource("dynamodb").Table(
            os.environ.get("TABLE_NAME", "SensorEvents")
        )
    return _events_table


def _get_alerts_table():
    global _alerts_table
    if _alerts_table is None:
        import boto3
        _alerts_table = boto3.resource("dynamodb").Table(
            os.environ.get("ALERTS_TABLE_NAME", "DetectedEvents")
        )
    return _alerts_table


def _get_raw_table():
    global _raw_table
    raw_name = os.environ.get("RAW_TABLE_NAME", "")
    if not raw_name:
        return None
    if _raw_table is None:
        import boto3
        _raw_table = boto3.resource("dynamodb").Table(raw_name)
    return _raw_table


def _get_sns_client():
    global _sns_client
    if _sns_client is None:
        import boto3
        _sns_client = boto3.client("sns")
    return _sns_client


def _ms_to_iso(t_ms):
    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _to_decimal(obj):
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return Decimal("0")
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


def _json_default(obj):
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    return str(obj)


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _extract_raw_key(body):
    """Extract (device_id, t0_ms) from Contract v1 payload for RawBatches dedup."""
    if not isinstance(body, dict):
        return None
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return None
    acc = body.get("accelerometer")
    if isinstance(acc, dict) and _is_num(acc.get("t0")):
        return device_id.strip(), int(acc["t0"])
    return None


def _save_raw_batch(raw_table, device_id, t0, received_at_ms, raw_text, body):
    """Store raw batch in RawBatches with attribute_not_exists dedup.

    Returns True if saved, False if duplicate.
    """
    if raw_table is None:
        return True
    schema_ver = body.get("schema_version", 0)
    item = {
        "device_id": device_id,
        "t0": int(t0),
        "received_at": int(received_at_ms),
        "schema_version": int(schema_ver) if _is_num(schema_ver) else 0,
        "status": "received",
        "raw": raw_text,
    }
    try:
        raw_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(device_id)",
        )
        return True
    except Exception as e:
        resp = getattr(e, "response", None)
        if (
            isinstance(resp, dict)
            and resp.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
        ):
            return False
        raise


def _mark_raw_batch(raw_table, device_id, t0, status, reason=None, detail=None):
    if raw_table is None:
        return
    names = {"#s": "status"}
    values = {":s": status}
    expr = "SET #s = :s"
    if reason:
        expr += ", reason = :r, detail = :d"
        values[":r"] = reason
        values[":d"] = detail or ""
    try:
        raw_table.update_item(
            Key={"device_id": device_id, "t0": int(t0)},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        pass


def adapt_clean_batch(clean):
    """Bridge Simion's preprocess.py clean_batch_v1 output to process.py's expected input."""
    sig = clean["signal"]
    loc = clean.get("location")
    t_start_ms = int(sig["t_start_ms"])

    adapted_location = None
    if isinstance(loc, dict):
        adapted_location = {
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "accuracy": loc.get("accuracy_m", loc.get("accuracy")),
        }

    return {
        "device_id": clean["device_id"],
        "t0": t_start_ms,
        "timestamp": _ms_to_iso(t_start_ms),
        "sample_rate_hz": int(sig["fs_hz"]),
        "location": adapted_location,
        "acc": sig["acc"],
        "gyro": sig.get("gyro"),
        "warnings": list(clean.get("warnings") or []),
    }


RECENT_EVENTS_INDEX = "RecentEventsIndex"
RECENT_EVENTS_PK = "EVENT"

EVENTS_PROJECTION_FIELDS = (
    "device_id",
    "timestamp",
    "t_ms",
    "rms",
    "pga",
    "variance",
    "dominant_freq_hz",
    "gyro_rms",
    "is_stationary",
    "sta_lta",
    "is_trigger",
    "lat",
    "lon",
    "warnings",
    "value",
)
EVENTS_ATTR_NAMES = {
    f"#f{i}": name for i, name in enumerate(EVENTS_PROJECTION_FIELDS)
}
EVENTS_PROJECTION = ", ".join(EVENTS_ATTR_NAMES.keys())


def _scan_table(table, max_items=None, max_pages=None, projection=None, attr_names=None):
    items = []
    kwargs = {}
    if projection:
        kwargs["ProjectionExpression"] = projection
    if attr_names:
        kwargs["ExpressionAttributeNames"] = attr_names

    pages = 0
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        pages += 1

        if max_items is not None and len(items) >= max_items:
            break
        if max_pages is not None and pages >= max_pages:
            break

        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items[:max_items] if max_items is not None else items


def _fetch_device_history(events_table, device_id, t0_ms, lookback_sec):
    start_iso = _ms_to_iso(t0_ms - lookback_sec * 1000)
    end_iso = _ms_to_iso(t0_ms - 1)
    if Key is not None:
        resp = events_table.query(
            KeyConditionExpression=Key("device_id").eq(device_id)
            & Key("timestamp").between(start_iso, end_iso),
            ScanIndexForward=True,
        )
    else:
        resp = events_table.query(
            device_id=device_id,
            start_iso=start_iso,
            end_iso=end_iso,
        )
    items = resp.get("Items", [])
    history = []
    for item in items:
        if "rms" not in item:
            continue
        t_ms = item.get("t_ms")
        if t_ms is None:
            try:
                dt = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
                t_ms = int(dt.timestamp() * 1000)
            except ValueError:
                continue
        history.append(
            {
                "t_ms": int(t_ms),
                "rms": float(item["rms"]),
                "is_stationary": bool(item.get("is_stationary", False)),
            }
        )
    return history


def _handle_post_telemetry(event):
    received_at_ms = int(time.time() * 1000)
    raw_text = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_text = base64.b64decode(raw_text).decode("utf-8")

    try:
        body = json.loads(raw_text or "{}")
    except ValueError:
        return _response(
            400,
            {"status": "rejected", "reason": "SCHEMA_INVALID", "error": "Invalid JSON body"},
        )

    if not isinstance(body, dict):
        return _response(
            400,
            {"status": "rejected", "reason": "SCHEMA_INVALID", "error": "Payload must be a JSON object"},
        )

    # Optional RawBatches storage & deduplication on (device_id, t0)
    raw_table = _get_raw_table()
    raw_key = _extract_raw_key(body)
    if raw_table is not None and raw_key is not None:
        raw_device_id, raw_t0 = raw_key
        if not _save_raw_batch(raw_table, raw_device_id, raw_t0, received_at_ms, raw_text, body):
            return _response(
                200,
                {
                    "status": "duplicate_ignored",
                    "device_id": raw_device_id,
                    "t0": raw_t0,
                },
            )

    # Validate & clean via Simion's preprocess.py
    clean = preprocess(body, received_at_ms)
    if clean.get("status") != "ok":
        reason = clean.get("reason", "SCHEMA_INVALID")
        detail = clean.get("detail", "Invalid telemetry payload")
        if raw_table is not None and raw_key is not None:
            _mark_raw_batch(raw_table, raw_key[0], raw_key[1], "rejected", reason, detail)
        return _response(
            400,
            {
                "status": "rejected",
                "reason": reason,
                "detail": detail,
                "error": detail,
            },
        )

    if raw_table is not None and raw_key is not None:
        _mark_raw_batch(raw_table, raw_key[0], raw_key[1], "preprocessed")

    clean_batch = adapt_clean_batch(clean)
    device_id = clean_batch["device_id"]
    t0 = int(clean_batch["t0"])
    t0_iso = _ms_to_iso(t0)

    events_table = _get_events_table()

    # Fallback deduplication on SensorEvents (device_id, timestamp) when RawBatches is not active
    if raw_table is None:
        existing = events_table.get_item(
            Key={"device_id": device_id, "timestamp": t0_iso}
        )
        if "Item" in existing:
            return _response(
                200,
                {
                    "status": "duplicate_ignored",
                    "device_id": device_id,
                    "t0": t0,
                },
            )

    lta_sec = int(os.environ.get("LTA_SEC", "30"))
    history = _fetch_device_history(events_table, device_id, t0, lta_sec + 5)

    result = process_clean_batch(clean_batch, history)
    features = result["features"]
    triggers = result["triggers"]

    for feat in features:
        item = dict(feat)
        item["gsi_pk"] = RECENT_EVENTS_PK
        events_table.put_item(Item=_to_decimal(item))

    correlation = None
    if triggers:
        sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
        sns_client = _get_sns_client() if sns_topic_arn else None
        correlation = correlate_triggers(
            triggers,
            events_table,
            _get_alerts_table(),
            sns_client=sns_client,
            sns_topic_arn=sns_topic_arn,
        )

    max_pga = max((f["pga"] for f in features), default=0.0)
    return _response(
        200,
        {
            "status": "processed",
            "device_id": device_id,
            "windows_processed": len(features),
            "max_pga": round(max_pga, 6),
            "triggers_count": len(triggers),
            "warnings": clean_batch["warnings"],
            "correlation": correlation,
        },
    )


def _handle_get_events():
    table = _get_events_table()
    if Key is not None and hasattr(table, "query"):
        try:
            resp = table.query(
                IndexName=RECENT_EVENTS_INDEX,
                KeyConditionExpression=Key("gsi_pk").eq(RECENT_EVENTS_PK),
                ScanIndexForward=False,
                Limit=50,
                ProjectionExpression=EVENTS_PROJECTION,
                ExpressionAttributeNames=EVENTS_ATTR_NAMES,
            )
            items = resp.get("Items", [])
            if items:
                return _response(200, {"events": items})
        except Exception:
            pass

    # Fallback while GSI is building or if no GSI-tagged items exist yet
    items = _scan_table(
        table,
        max_items=500,
        max_pages=3,
        projection=EVENTS_PROJECTION,
        attr_names=EVENTS_ATTR_NAMES,
    )
    items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return _response(200, {"events": items[:50]})


def _handle_get_alerts():
    items = _scan_table(_get_alerts_table())
    items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return _response(200, {"alerts": items[:20]})


def lambda_handler(event, context):
    http_ctx = event.get("requestContext", {}).get("http", {})
    method = http_ctx.get("method", "GET").upper()
    path = event.get("rawPath") or http_ctx.get("path") or "/"

    try:
        if method == "POST" and path.rstrip("/") == "/telemetry":
            return _handle_post_telemetry(event)
        if method == "GET" and path.rstrip("/") == "/events":
            return _handle_get_events()
        if method == "GET" and path.rstrip("/") == "/alerts":
            return _handle_get_alerts()
        return _response(404, {"error": f"Route not found: {method} {path}"})
    except Exception as e:
        return _response(500, {"error": str(e)})


# Backwards-compatible alias
handler = lambda_handler
