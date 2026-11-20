"""Thin AWS Lambda handler for seismic telemetry ingestion, events, and alerts."""

import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from correlate import correlate_triggers
from process import process_clean_batch

# Automatically use teammate's preprocess.py once merged; fall back to built-in Contract v1 adapter.
try:
    from preprocess import preprocess_batch
except ImportError:
    def preprocess_batch(body):
        """Built-in Contract v1 fallback adapter."""
        if not isinstance(body, dict):
            return None, "Payload must be a JSON object"

        if body.get("schema_version") != 1:
            return None, "Unsupported schema_version; expected 1"

        device_id = body.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            return None, "Missing or invalid device_id"

        sample_rate_hz = body.get("sample_rate_hz", 20)
        if not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
            return None, "Invalid sample_rate_hz"

        acc = body.get("accelerometer")
        if not isinstance(acc, dict):
            return None, "Missing accelerometer object"

        t0 = acc.get("t0")
        if isinstance(t0, bool) or not isinstance(t0, (int, float)):
            return None, "Missing or invalid accelerometer.t0"
        t0 = int(t0)

        acc_t = acc.get("t")
        acc_x = acc.get("x")
        acc_y = acc.get("y")
        acc_z = acc.get("z")
        if not all(isinstance(arr, list) for arr in (acc_t, acc_x, acc_y, acc_z)):
            return None, "accelerometer.t/x/y/z must be arrays"

        n_acc = len(acc_t)
        if not (len(acc_x) == n_acc and len(acc_y) == n_acc and len(acc_z) == n_acc):
            return None, "accelerometer arrays must have equal length"

        whole_seconds = n_acc // sample_rate_hz
        if whole_seconds < 1 or whole_seconds > 5:
            return None, "Batch duration must be between 1 and 5 whole seconds"

        trimmed_n = whole_seconds * sample_rate_hz
        xs = [float(v) for v in acc_x[:trimmed_n]]
        ys = [float(v) for v in acc_y[:trimmed_n]]
        zs = [float(v) for v in acc_z[:trimmed_n]]

        mean_x = sum(xs) / trimmed_n
        mean_y = sum(ys) / trimmed_n
        mean_z = sum(zs) / trimmed_n
        clean_acc = {
            "x": [v - mean_x for v in xs],
            "y": [v - mean_y for v in ys],
            "z": [v - mean_z for v in zs],
        }

        warnings = []
        gyro = body.get("gyroscope")
        clean_gyro = None
        if isinstance(gyro, dict):
            gt = gyro.get("t")
            gx = gyro.get("x")
            gy = gyro.get("y")
            gz = gyro.get("z")
            if all(isinstance(arr, list) for arr in (gt, gx, gy, gz)):
                n_g = len(gt)
                if not (len(gx) == n_g and len(gy) == n_g and len(gz) == n_g):
                    return None, "gyroscope arrays must have equal length"
                if n_g >= trimmed_n:
                    clean_gyro = {
                        "x": [float(v) for v in gx[:trimmed_n]],
                        "y": [float(v) for v in gy[:trimmed_n]],
                        "z": [float(v) for v in gz[:trimmed_n]],
                    }
                elif n_g > 0:
                    pad = trimmed_n - n_g
                    clean_gyro = {
                        "x": [float(v) for v in gx] + [float(gx[-1])] * pad,
                        "y": [float(v) for v in gy] + [float(gy[-1])] * pad,
                        "z": [float(v) for v in gz] + [float(gz[-1])] * pad,
                    }
                else:
                    warnings.append("NO_GYRO")
            else:
                warnings.append("NO_GYRO")
        else:
            warnings.append("NO_GYRO")

        location = body.get("location")
        if isinstance(location, dict):
            acc_m = location.get("accuracy")
            if isinstance(acc_m, (int, float)) and not isinstance(acc_m, bool) and acc_m > 100:
                warnings.append("GPS_LOW_ACCURACY")

        ts = body.get("timestamp")
        if not isinstance(ts, str) or not ts:
            ts = _ms_to_iso(t0)

        clean_batch = {
            "device_id": device_id.strip(),
            "t0": t0,
            "timestamp": ts,
            "sample_rate_hz": sample_rate_hz,
            "location": location if isinstance(location, dict) else None,
            "acc": clean_acc,
            "gyro": clean_gyro,
            "warnings": warnings,
        }
        return clean_batch, None


dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(os.environ.get("TABLE_NAME", "SensorEvents"))
alerts_table = dynamodb.Table(os.environ.get("ALERTS_TABLE_NAME", "DetectedEvents"))
sns_client = boto3.client("sns")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")


def _ms_to_iso(t_ms):
    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _scan_all(table):
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def _fetch_device_history(device_id, t0_ms, lookback_sec):
    start_iso = _ms_to_iso(t0_ms - lookback_sec * 1000)
    end_iso = _ms_to_iso(t0_ms - 1)
    resp = events_table.query(
        KeyConditionExpression=Key("device_id").eq(device_id)
        & Key("timestamp").between(start_iso, end_iso),
        ScanIndexForward=True,
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
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    clean_batch, reason = preprocess_batch(body)
    if clean_batch is None:
        return _response(400, {"error": reason or "Invalid telemetry payload"})

    device_id = clean_batch["device_id"]
    t0 = int(clean_batch["t0"])
    t0_iso = _ms_to_iso(t0)

    # Deduplicate on (device_id, t0)
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
    history = _fetch_device_history(device_id, t0, lta_sec + 5)

    result = process_clean_batch(clean_batch, history)
    features = result["features"]
    triggers = result["triggers"]

    for feat in features:
        events_table.put_item(Item=_to_decimal(feat))

    correlation = None
    if triggers:
        correlation = correlate_triggers(
            triggers,
            events_table,
            alerts_table,
            sns_client=sns_client,
            sns_topic_arn=SNS_TOPIC_ARN,
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
            "correlation": correlation,
        },
    )


def _handle_get_events():
    items = _scan_all(events_table)
    items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return _response(200, {"events": items[:50]})


def _handle_get_alerts():
    items = _scan_all(alerts_table)
    items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return _response(200, {"alerts": items[:20]})


def handler(event, context):
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
