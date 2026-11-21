"""Multi-device spatial/temporal earthquake correlation & SNS alerting.

Evaluates active eligible (ARMED) devices over the last QUORUM_WINDOW_SEC (default 60s)
and requires tight onset synchronization (ONSET_SYNC_SEC, default 5s) among triggering
devices so unrelated everyday disturbances cannot form a false quorum.
"""

import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal

try:
    from boto3.dynamodb.conditions import Key
except ImportError:
    Key = None

EARTH_RADIUS_KM = 6371.0
STATE_SORT_KEY = "__STATE__"


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometers between two WGS84 coordinates."""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_KM * c


def _load_config(config=None):
    overrides = config or {}
    return {
        "ZONE_RADIUS_KM": float(
            overrides.get(
                "ZONE_RADIUS_KM",
                os.environ.get("ZONE_RADIUS_KM", "5.0"),
            )
        ),
        "QUORUM_WINDOW_SEC": int(
            overrides.get(
                "QUORUM_WINDOW_SEC",
                os.environ.get("QUORUM_WINDOW_SEC", "60"),
            )
        ),
        "ONSET_SYNC_SEC": int(
            overrides.get(
                "ONSET_SYNC_SEC",
                os.environ.get("ONSET_SYNC_SEC", "5"),
            )
        ),
        "QUORUM_MIN_DEVICES": int(
            overrides.get(
                "QUORUM_MIN_DEVICES",
                os.environ.get("QUORUM_MIN_DEVICES", "2"),
            )
        ),
        "QUORUM_MIN_RATIO": float(
            overrides.get(
                "QUORUM_MIN_RATIO",
                os.environ.get("QUORUM_MIN_RATIO", "0.5"),
            )
        ),
        "ALERT_COOLDOWN_SEC": int(
            overrides.get(
                "ALERT_COOLDOWN_SEC",
                os.environ.get("ALERT_COOLDOWN_SEC", "30"),
            )
        ),
    }


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


def _parse_t_ms(item):
    if "t_ms" in item and item["t_ms"] is not None:
        return int(item["t_ms"])
    ts = item.get("timestamp")
    if isinstance(ts, str) and ts and ts != STATE_SORT_KEY:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _parse_onset_ms(item):
    if "t_onset_ms" in item and item["t_onset_ms"] is not None:
        return int(item["t_onset_ms"])
    return _parse_t_ms(item)


def _ms_to_iso(t_ms):
    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


RECENT_EVENTS_INDEX = "RecentEventsIndex"
RECENT_EVENTS_PK = "EVENT"


def _scan_all_items(table):
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


def _fetch_recent_events(events_table, ref_t_ms, window_ms):
    """Query RecentEventsIndex for the quorum time window; fall back to scan if unavailable."""
    if Key is not None and hasattr(events_table, "query"):
        start_iso = _ms_to_iso(ref_t_ms - window_ms)
        end_iso = _ms_to_iso(ref_t_ms + window_ms)
        try:
            items = []
            kwargs = {
                "IndexName": RECENT_EVENTS_INDEX,
                "KeyConditionExpression": Key("gsi_pk").eq(RECENT_EVENTS_PK)
                & Key("timestamp").between(start_iso, end_iso),
            }
            while True:
                resp = events_table.query(**kwargs)
                items.extend(resp.get("Items", []))
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                kwargs["ExclusiveStartKey"] = last_key
            return items
        except Exception:
            pass
    return _scan_all_items(events_table)


def _find_recent_zone_alert(alerts_table, zone_id, ref_t_ms, cooldown_sec):
    items = []
    if hasattr(alerts_table, "query") and Key is not None:
        try:
            resp = alerts_table.query(
                KeyConditionExpression=Key("zone_id").eq(zone_id),
                ScanIndexForward=False,
                Limit=5,
            )
            items = resp.get("Items", [])
        except Exception:
            items = _scan_all_items(alerts_table)
    else:
        items = _scan_all_items(alerts_table)

    cooldown_ms = cooldown_sec * 1000
    best_match = None
    best_t_ms = -1

    for item in items:
        if item.get("zone_id") != zone_id:
            continue
        item_t_ms = _parse_t_ms(item)
        if item_t_ms is None:
            continue
        if abs(ref_t_ms - item_t_ms) <= cooldown_ms and item_t_ms > best_t_ms:
            best_t_ms = item_t_ms
            best_match = item

    return best_match


def correlate_triggers(
    new_triggers,
    events_table,
    alerts_table,
    sns_client=None,
    sns_topic_arn="",
    config=None,
):
    """Evaluate multi-device spatial/temporal quorum and emit/update DetectedEvents."""
    cfg = _load_config(config)

    if not new_triggers:
        return {
            "quake_detected": False,
            "zone_id": "",
            "triggered_devices": [],
            "active_devices": 0,
            "consensus_ratio": 0.0,
            "event_created": False,
        }

    # Use highest-PGA trigger as spatial/temporal anchor
    ref_trigger = max(new_triggers, key=lambda t: float(t.get("pga", 0.0)))
    ref_lat = float(ref_trigger["lat"])
    ref_lon = float(ref_trigger["lon"])
    ref_t_ms = int(ref_trigger["t_ms"])
    ref_onset_ms = _parse_onset_ms(ref_trigger) or ref_t_ms
    zone_id = f"{round(ref_lat, 2)}_{round(ref_lon, 2)}"

    window_ms = cfg["QUORUM_WINDOW_SEC"] * 1000
    onset_sync_ms = cfg["ONSET_SYNC_SEC"] * 1000
    radius_km = cfg["ZONE_RADIUS_KM"]

    all_events = _fetch_recent_events(events_table, ref_t_ms, window_ms)

    active_devices = set()
    triggered_devices = set()
    max_pga = float(ref_trigger.get("pga", 0.0))

    # Count eligible active devices over QUORUM_WINDOW_SEC (default 60s),
    # and only count triggers whose onset is synchronized within ONSET_SYNC_SEC (default 5s).
    for item in all_events:
        if item.get("timestamp") == STATE_SORT_KEY or item.get("item_type") == "DEVICE_STATE":
            continue
        if item.get("device_eligible") is False:
            continue
        dev_id = item.get("device_id")
        if not dev_id:
            continue
        item_t_ms = _parse_t_ms(item)
        if item_t_ms is None or abs(item_t_ms - ref_t_ms) > window_ms:
            continue
        item_lat = item.get("lat")
        item_lon = item.get("lon")
        if item_lat is None or item_lon is None:
            continue

        dist = haversine_km(ref_lat, ref_lon, float(item_lat), float(item_lon))
        if dist <= radius_km:
            active_devices.add(str(dev_id))
            if bool(item.get("is_trigger", False)):
                item_onset_ms = _parse_onset_ms(item)
                if (
                    item_onset_ms is not None
                    and abs(item_onset_ms - ref_onset_ms) <= onset_sync_ms
                ):
                    triggered_devices.add(str(dev_id))
                    pga_val = float(item.get("pga", 0.0))
                    if pga_val > max_pga:
                        max_pga = pga_val

    # Ensure current synchronized triggers are always counted
    for trig in new_triggers:
        t_lat = trig.get("lat")
        t_lon = trig.get("lon")
        if t_lat is None or t_lon is None:
            continue
        if haversine_km(ref_lat, ref_lon, float(t_lat), float(t_lon)) <= radius_km:
            dev_id = str(trig["device_id"])
            active_devices.add(dev_id)
            trig_onset_ms = _parse_onset_ms(trig)
            if (
                trig_onset_ms is None
                or abs(trig_onset_ms - ref_onset_ms) <= onset_sync_ms
            ):
                triggered_devices.add(dev_id)
                pga_val = float(trig.get("pga", 0.0))
                if pga_val > max_pga:
                    max_pga = pga_val

    active_count = len(active_devices)
    consensus_ratio = len(triggered_devices) / max(active_count, 1)

    quake_detected = (
        len(triggered_devices) >= cfg["QUORUM_MIN_DEVICES"]
        and consensus_ratio >= cfg["QUORUM_MIN_RATIO"]
    )
    event_created = False

    if quake_detected:
        existing_alert = _find_recent_zone_alert(
            alerts_table, zone_id, ref_t_ms, cfg["ALERT_COOLDOWN_SEC"]
        )

        if existing_alert is not None:
            existing_devs = set(existing_alert.get("triggered_devices", []))
            merged_devs = sorted(existing_devs.union(triggered_devices))
            merged_max_pga = max(
                float(existing_alert.get("max_pga", 0.0)), max_pga
            )
            alerts_table.update_item(
                Key={
                    "zone_id": existing_alert["zone_id"],
                    "timestamp": existing_alert["timestamp"],
                },
                UpdateExpression=(
                    "SET triggered_devices = :devs, "
                    "max_pga = :pga, "
                    "active_devices = :active, "
                    "consensus_ratio = :ratio"
                ),
                ExpressionAttributeValues=_to_decimal(
                    {
                        ":devs": merged_devs,
                        ":pga": merged_max_pga,
                        ":active": active_count,
                        ":ratio": round(consensus_ratio, 4),
                    }
                ),
            )
            event_created = False
        else:
            event_ts = _ms_to_iso(ref_t_ms)
            alert_item = {
                "zone_id": zone_id,
                "timestamp": event_ts,
                "t_ms": ref_t_ms,
                "t_onset_ms": ref_onset_ms,
                "status": "CONFIRMED",
                "lat": round(ref_lat, 6),
                "lon": round(ref_lon, 6),
                "triggered_devices": sorted(triggered_devices),
                "active_devices": active_count,
                "consensus_ratio": round(consensus_ratio, 4),
                "max_pga": round(max_pga, 6),
            }
            alerts_table.put_item(Item=_to_decimal(alert_item))
            event_created = True

            if sns_client is not None and sns_topic_arn:
                sns_client.publish(
                    TopicArn=sns_topic_arn,
                    Subject=f"Earthquake Detected in Zone {zone_id}",
                    Message=json.dumps(alert_item),
                )

    return {
        "quake_detected": quake_detected,
        "zone_id": zone_id,
        "triggered_devices": sorted(triggered_devices),
        "active_devices": active_count,
        "consensus_ratio": round(consensus_ratio, 4),
        "event_created": event_created,
    }
