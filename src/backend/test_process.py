"""Pure-Python unit and end-to-end tests for preprocess.py, process.py, correlate.py, and ingest.py."""

import json
import math
import os
import sys
import time
import unittest
from datetime import datetime, timezone

# Ensure updated/backend is importable regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from correlate import correlate_triggers, haversine_km
from process import process_clean_batch


def _make_history(count, t0_ms, rms=0.1, is_stationary=True, is_candidate=False):
    return [
        {
            "t_ms": t0_ms - (count - i) * 1000,
            "rms": rms,
            "is_stationary": is_stationary,
            "is_candidate": is_candidate,
        }
        for i in range(count)
    ]


def _make_clean_batch(
    t0_ms=1795202100000,
    acc_x=None,
    acc_y=None,
    acc_z=None,
    gyro_val=0.05,
    gyro_x=None,
    gyro_y=None,
    gyro_z=None,
    warnings=None,
    location=None,
    sample_rate_hz=20,
    duration_s=2,
    device_id="ios-01",
    gravity_vector=(0.0, 0.0, 9.81),
    gravity_end=None,
    intra_batch_tilt_deg=0.0,
):
    n = sample_rate_hz * duration_s
    if acc_x is None:
        acc_x = [0.05] * n
    if acc_y is None:
        acc_y = [0.0] * n
    if acc_z is None:
        acc_z = [0.0] * n

    if gyro_x is not None or gyro_y is not None or gyro_z is not None:
        gyro = {
            "x": gyro_x if gyro_x is not None else [0.0] * n,
            "y": gyro_y if gyro_y is not None else [0.0] * n,
            "z": gyro_z if gyro_z is not None else [0.0] * n,
        }
    elif gyro_val is None:
        gyro = None
    else:
        gyro = {
            "x": [gyro_val] * n,
            "y": [0.0] * n,
            "z": [0.0] * n,
        }

    return {
        "device_id": device_id,
        "t0": t0_ms,
        "timestamp": "2026-11-20T19:15:00.000Z",
        "sample_rate_hz": sample_rate_hz,
        "location": location
        if location is not None
        else {"lat": 46.7712, "lon": 23.6236, "accuracy": 8.5},
        "acc": {"x": acc_x, "y": acc_y, "z": acc_z},
        "gyro": gyro,
        "gravity_vector": list(gravity_vector) if gravity_vector is not None else None,
        "gravity_end": list(gravity_end) if gravity_end is not None else (
            list(gravity_vector) if gravity_vector is not None else None
        ),
        "intra_batch_tilt_deg": intra_batch_tilt_deg,
        "warnings": warnings if warnings is not None else [],
    }


def _make_4hz_shake_signal(sample_rate_hz=20, duration_s=2, amplitude=3.5):
    """Return a zero-mean 4 Hz horizontal earthquake shake."""
    n = sample_rate_hz * duration_s
    return [
        amplitude * math.sin(2.0 * math.pi * 4.0 * i / sample_rate_hz)
        for i in range(n)
    ]


class FakeTable:
    """Minimal in-memory DynamoDB table mock supporting upsert on primary key."""

    def __init__(self, items=None, pk="device_id", sk="timestamp"):
        self.items = list(items or [])
        self.pk = pk
        self.sk = sk

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def get_item(self, Key):
        for it in self.items:
            if all(it.get(k) == v for k, v in Key.items()):
                return {"Item": dict(it)}
        return {}

    def query(self, **kwargs):
        return {"Items": list(self.items)}

    def put_item(self, Item):
        new_item = dict(Item)
        for idx, it in enumerate(self.items):
            if it.get(self.pk) == new_item.get(self.pk) and it.get(self.sk) == new_item.get(self.sk):
                self.items[idx] = new_item
                return {}
        self.items.append(new_item)
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        for item in self.items:
            if all(item.get(k) == v for k, v in Key.items()):
                item["triggered_devices"] = ExpressionAttributeValues[":devs"]
                item["max_pga"] = ExpressionAttributeValues[":pga"]
                item["active_devices"] = ExpressionAttributeValues[":active"]
                item["consensus_ratio"] = ExpressionAttributeValues[":ratio"]
                return {}
        return {}


class FakeSnsClient:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "test-msg-1"}


class TestProcessCleanBatch(unittest.TestCase):
    def test_warmup_blocks_triggers_when_history_insufficient(self):
        t0 = 1795202100000
        history = _make_history(count=10, t0_ms=t0, rms=0.1, is_stationary=True)
        shake = _make_4hz_shake_signal(duration_s=2)
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_val=0.05, duration_s=2)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["features"]), 2)
        self.assertFalse(any(f["is_trigger"] for f in result["features"]))
        self.assertEqual(len(result["triggers"]), 0)

    def test_picking_up_armed_phone_to_talk_is_rejected_and_disarmed(self):
        """Phone was flat & ARMED, then picked up (tilted 50 deg + gyro spike)."""
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.1, is_stationary=True)
        shake = _make_4hz_shake_signal(duration_s=2, amplitude=4.0)
        # Tilted ~50 degrees away from flat [0, 0, 9.81]
        tilted_gravity = [0.0, 7.51, 6.31]
        batch = _make_clean_batch(
            t0_ms=t0,
            acc_x=shake,
            gyro_val=0.8,  # above TRIGGER_MAX_GYRO_RMS=0.50
            duration_s=2,
            gravity_vector=tilted_gravity,
            gravity_end=tilted_gravity,
            intra_batch_tilt_deg=45.0,
        )
        device_state = {
            "state": "ARMED",
            "static_sec": 20,
            "baseline_gravity": [0.0, 0.0, 9.81],
            "lta_rms": 0.08,
        }

        result = process_clean_batch(batch, history, device_state=device_state)

        self.assertEqual(len(result["triggers"]), 0)
        self.assertTrue(result["disarm_device"])
        self.assertEqual(result["disarm_reason"], "TILT_CHANGE")

    def test_quick_gyro_flick_rejected_by_gyro_peak(self):
        """Short 0.1s hand flick diluted in RMS must still be caught by gyro_peak."""
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.1, is_stationary=True)
        shake = _make_4hz_shake_signal(duration_s=2, amplitude=3.5)
        # Only 2 samples have a 1.6 rad/s flick -> RMS ~0.35 (below 0.50), peak 1.6 (above 1.10)
        gx = [0.05] * 40
        gx[5] = 1.6
        gx[25] = 1.6
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_x=gx, duration_s=2)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["triggers"]), 0)
        self.assertTrue(result["disarm_device"])
        self.assertEqual(result["disarm_reason"], "HIGH_GYRO")

    def test_violent_desk_typing_vertical_impulses_rejected(self):
        """Aggressive typing on desk next to flat phone: vertical Z spikes, high crest factor."""
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.08, is_stationary=True)
        n = 40
        acc_x = [0.08] * n
        acc_y = [0.08] * n
        acc_z = [0.05] * n
        # Sharp vertical keystroke bangs (brief spikes on Z)
        for idx in (4, 15, 24, 35):
            acc_z[idx] = 3.8
        batch = _make_clean_batch(
            t0_ms=t0,
            acc_x=acc_x,
            acc_y=acc_y,
            acc_z=acc_z,
            gyro_val=0.04,
            duration_s=2,
            gravity_vector=[0.0, 0.0, 9.81],
        )

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["triggers"]), 0)
        self.assertFalse(any(f["is_trigger"] for f in result["features"]))

    def test_brief_single_second_bump_rejected_by_sustained_duration(self):
        """A 1-second bump followed by calm does NOT trigger (requires 2 sustained seconds)."""
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.1, is_stationary=True)
        shake_1s = _make_4hz_shake_signal(duration_s=1, amplitude=3.5)
        calm_1s = [0.05] * 20
        batch = _make_clean_batch(
            t0_ms=t0, acc_x=shake_1s + calm_1s, gyro_val=0.04, duration_s=2
        )

        result = process_clean_batch(batch, history)

        self.assertTrue(result["features"][0]["is_candidate"])
        self.assertFalse(result["features"][0]["is_trigger"])
        self.assertFalse(result["features"][1]["is_candidate"])
        self.assertEqual(len(result["triggers"]), 0)

    def test_high_frequency_desk_buzz_rejected_at_50hz(self):
        """16 Hz mechanical buzz/typing resonance at 50 Hz sampling is outside 1-8 Hz band."""
        t0 = 1795202100000
        fs = 50
        history = _make_history(count=20, t0_ms=t0, rms=0.1, is_stationary=True)
        n = fs * 2
        acc_x = [4.0 * math.sin(2.0 * math.pi * 16.0 * i / fs) for i in range(n)]
        batch = _make_clean_batch(
            t0_ms=t0, acc_x=acc_x, gyro_val=0.03, sample_rate_hz=fs, duration_s=2
        )

        result = process_clean_batch(batch, history)

        self.assertGreater(result["features"][0]["dominant_freq_hz"], 8)
        self.assertEqual(len(result["triggers"]), 0)

    def test_stationary_phone_with_sustained_4hz_shake_triggers_accurately(self):
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.2, is_stationary=True)
        shake = _make_4hz_shake_signal(sample_rate_hz=20, duration_s=2, amplitude=3.5)
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_val=0.05, duration_s=2)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["features"]), 2)
        self.assertFalse(result["disarm_device"])
        self.assertEqual(len(result["triggers"]), 1)  # Fires on 2nd consecutive second

        feat = result["features"][1]
        trig = result["triggers"][0]

        self.assertTrue(feat["is_trigger"])
        self.assertEqual(feat["dominant_freq_hz"], 4)
        self.assertEqual(trig["t_onset_ms"], t0)  # Onset was window 0 (t0)
        self.assertGreater(feat["sta_lta"], 10.0)
        self.assertGreaterEqual(feat["horiz_ratio"], 0.9)

    def test_gps_low_accuracy_warning_does_not_block_trigger(self):
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.2, is_stationary=True)
        shake = _make_4hz_shake_signal(duration_s=2)
        batch = _make_clean_batch(
            t0_ms=t0,
            acc_x=shake,
            gyro_val=0.05,
            duration_s=2,
            warnings=["GPS_LOW_ACCURACY"],
            location={"lat": 46.7712, "lon": 23.6236, "accuracy": 250.0},
        )

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["triggers"]), 1)
        self.assertIn("GPS_LOW_ACCURACY", result["features"][1]["warnings"])


class TestCorrelateTriggers(unittest.TestCase):
    def test_haversine_distance_accuracy(self):
        d_same = haversine_km(46.7712, 23.6236, 46.7712, 23.6236)
        self.assertAlmostEqual(d_same, 0.0, places=5)

        d_far = haversine_km(46.7712, 23.6236, 46.9512, 23.6236)
        self.assertGreater(d_far, 19.0)
        self.assertLess(d_far, 21.0)

    def test_unsynchronized_events_10s_apart_do_not_form_quorum(self):
        """Two phones triggering 10s apart (outside ONSET_SYNC_SEC=5) must NOT alert."""
        t_ms = 1795202100000
        base_lat, base_lon = 46.7712, 23.6236

        events = [
            {
                "device_id": "phone-1",
                "t_ms": t_ms,
                "t_onset_ms": t_ms,
                "lat": base_lat,
                "lon": base_lon,
                "is_trigger": True,
                "device_eligible": True,
                "pga": 4.0,
            },
            {
                "device_id": "phone-2",
                "t_ms": t_ms - 10000,  # 10s earlier -> outside 5s sync window!
                "t_onset_ms": t_ms - 10000,
                "lat": base_lat + 0.001,
                "lon": base_lon + 0.001,
                "is_trigger": True,
                "device_eligible": True,
                "pga": 4.2,
            },
        ]
        new_trig = [events[0]]
        events_table = FakeTable(events)
        alerts_table = FakeTable(pk="zone_id", sk="timestamp")
        sns = FakeSnsClient()

        res = correlate_triggers(
            new_trig,
            events_table,
            alerts_table,
            sns_client=sns,
            sns_topic_arn="arn:aws:sns:eu-central-1:123456789012:quake-alerts-topic",
        )
        self.assertFalse(res["quake_detected"])
        self.assertEqual(res["active_devices"], 2)
        self.assertEqual(res["triggered_devices"], ["phone-1"])
        self.assertEqual(len(sns.published), 0)

    def test_quorum_and_spatial_filtering_synchronized(self):
        t_ms = 1795202100000
        base_lat, base_lon = 46.7712, 23.6236

        events_1_of_4 = [
            {
                "device_id": "phone-1",
                "t_ms": t_ms,
                "t_onset_ms": t_ms,
                "lat": base_lat,
                "lon": base_lon,
                "is_trigger": True,
                "device_eligible": True,
                "pga": 4.2,
            },
            {
                "device_id": "phone-2",
                "t_ms": t_ms - 1000,
                "t_onset_ms": t_ms - 1000,
                "lat": base_lat + 0.005,
                "lon": base_lon + 0.005,
                "is_trigger": False,
                "device_eligible": True,
                "pga": 0.2,
            },
            {
                "device_id": "phone-3",
                "t_ms": t_ms - 2000,
                "lat": base_lat - 0.005,
                "lon": base_lon,
                "is_trigger": False,
                "device_eligible": True,
                "pga": 0.1,
            },
            {
                "device_id": "phone-4",
                "t_ms": t_ms - 1500,
                "lat": base_lat,
                "lon": base_lon - 0.005,
                "is_trigger": False,
                "device_eligible": True,
                "pga": 0.15,
            },
            # Hidden state item & phone 20 km north must both be ignored
            {
                "device_id": "phone-1",
                "timestamp": "__STATE__",
                "item_type": "DEVICE_STATE",
                "state": "ARMED",
            },
            {
                "device_id": "phone-far",
                "t_ms": t_ms,
                "t_onset_ms": t_ms,
                "lat": base_lat + 0.18,
                "lon": base_lon,
                "is_trigger": True,
                "device_eligible": True,
                "pga": 6.0,
            },
        ]

        new_trig = [
            {
                "device_id": "phone-1",
                "t_ms": t_ms,
                "t_onset_ms": t_ms,
                "timestamp": "2026-11-20T19:15:00.000Z",
                "lat": base_lat,
                "lon": base_lon,
                "pga": 4.2,
                "rms": 2.1,
                "sta_lta": 10.5,
                "dominant_freq_hz": 4,
            }
        ]

        events_table = FakeTable(events_1_of_4)
        alerts_table = FakeTable(pk="zone_id", sk="timestamp")
        sns = FakeSnsClient()

        res_1 = correlate_triggers(
            new_trig,
            events_table,
            alerts_table,
            sns_client=sns,
            sns_topic_arn="arn:aws:sns:eu-central-1:123456789012:quake-alerts-topic",
        )
        self.assertFalse(res_1["quake_detected"])
        self.assertEqual(res_1["active_devices"], 4)
        self.assertEqual(res_1["triggered_devices"], ["phone-1"])

        # Phone-2 also triggers within 1s onset -> 2/4 (50%) -> CONFIRMED
        events_table.items[1]["is_trigger"] = True
        events_table.items[1]["pga"] = 5.1

        res_2 = correlate_triggers(
            new_trig,
            events_table,
            alerts_table,
            sns_client=sns,
            sns_topic_arn="arn:aws:sns:eu-central-1:123456789012:quake-alerts-topic",
        )
        self.assertTrue(res_2["quake_detected"])
        self.assertEqual(res_2["active_devices"], 4)
        self.assertEqual(res_2["triggered_devices"], ["phone-1", "phone-2"])
        self.assertNotIn("phone-far", res_2["triggered_devices"])
        self.assertAlmostEqual(res_2["consensus_ratio"], 0.5, places=4)
        self.assertTrue(res_2["event_created"])
        self.assertEqual(len(alerts_table.items), 1)
        self.assertEqual(len(sns.published), 1)


class TestEndToEndEverydayVsQuake(unittest.TestCase):
    def _build_payload(
        self,
        device_id,
        t0_ms,
        sent_at_ms,
        acc_x,
        acc_y,
        acc_z,
        gyro_x,
        gyro_y,
        gyro_z,
        lat=46.7712,
        lon=23.6236,
    ):
        n = len(acc_x)
        offsets = [i * 50 for i in range(n)]
        return {
            "schema_version": 1,
            "event_type": "raw_sensor_batch",
            "device_id": device_id,
            "platform": "ios",
            "sent_at": sent_at_ms,
            "timestamp": datetime.fromtimestamp(sent_at_ms / 1000.0, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "unit": "m/s^2",
            "gyro_unit": "rad/s",
            "sample_rate_hz": 20,
            "location": {"lat": lat, "lon": lon, "accuracy": 8.5},
            "accelerometer": {"t0": t0_ms, "t": offsets, "x": acc_x, "y": acc_y, "z": acc_z},
            "gyroscope": {"t0": t0_ms, "t": offsets, "x": gyro_x, "y": gyro_y, "z": gyro_z},
        }

    def _post(self, ingest_mod, payload):
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/telemetry"}},
            "rawPath": "/telemetry",
            "body": json.dumps(payload),
        }
        return ingest_mod.lambda_handler(event, None)

    def test_full_pipeline_everyday_life_ignored_and_real_2phone_quake_confirmed(self):
        import ingest

        class MockConditionalCheckError(Exception):
            def __init__(self):
                super().__init__("ConditionalCheckFailedException")
                self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        class MockRawTable:
            def __init__(self):
                self.items = {}

            def put_item(self, Item, ConditionExpression=None):
                k = (Item["device_id"], Item["t0"])
                if ConditionExpression and k in self.items:
                    raise MockConditionalCheckError()
                self.items[k] = dict(Item)

            def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
                it = self.items[(Key["device_id"], Key["t0"])]
                it["status"] = ExpressionAttributeValues[":s"]

        mock_events = FakeTable()
        mock_alerts = FakeTable(pk="zone_id", sk="timestamp")
        mock_raw = MockRawTable()
        mock_sns = FakeSnsClient()

        orig_events = ingest._events_table
        orig_alerts = ingest._alerts_table
        orig_raw = ingest._raw_table
        orig_state = ingest._state_table
        orig_sns = ingest._sns_client
        orig_env = {k: os.environ.get(k) for k in ("RAW_TABLE_NAME", "SNS_TOPIC_ARN", "ARMING_SEC")}

        try:
            ingest._events_table = mock_events
            ingest._alerts_table = mock_alerts
            ingest._raw_table = mock_raw
            ingest._state_table = None
            ingest._sns_client = mock_sns
            os.environ["RAW_TABLE_NAME"] = "RawBatches"
            os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:eu-central-1:123456789012:quake-alerts-topic"
            os.environ["ARMING_SEC"] = "6"  # 3 calm 2s batches to arm quickly in test

            now_ms = int(time.time() * 1000)
            n = 40  # 2 seconds @ 20 Hz

            # 1) Arm phone-A, phone-B, and phone-pickup with 3 calm 2s batches (6s total)
            for step_idx in range(3):
                t0 = now_ms - (20 - step_idx * 2) * 1000
                for dev in ("phone-A", "phone-B", "phone-pickup"):
                    p = self._build_payload(
                        dev,
                        t0,
                        now_ms,
                        [0.02] * n,
                        [0.01] * n,
                        [9.81] * n,
                        [0.01] * n,
                        [0.01] * n,
                        [0.01] * n,
                    )
                    resp = self._post(ingest, p)
                    self.assertEqual(resp["statusCode"], 200)
                    body = json.loads(resp["body"])
                    if step_idx < 2:
                        self.assertEqual(body["status"], "arming")
                        self.assertFalse(body["eligible"])
                    else:
                        self.assertEqual(body["status"], "processed")
                        self.assertEqual(body["device_state"], "ARMED")

            # 2) phone-youtube is held in hand (gentle rotation + tilt) -> stays UNARMED & out of SensorEvents
            yt_t0 = now_ms - 10000
            yt_payload = self._build_payload(
                "phone-youtube",
                yt_t0,
                now_ms,
                [0.4 * math.sin(i * 0.3) for i in range(n)],
                [6.0] * n,
                [7.7] * n,
                [0.4] * n,
                [0.2] * n,
                [0.1] * n,
            )
            yt_body = json.loads(self._post(ingest, yt_payload)["body"])
            self.assertEqual(yt_body["status"], "ignored_unarmed")
            self.assertFalse(yt_body["eligible"])

            # 3) phone-pickup (was ARMED!) is picked up to answer a call -> DISARMED immediately, 0 triggers!
            pick_t0 = now_ms - 8000
            pick_payload = self._build_payload(
                "phone-pickup",
                pick_t0,
                now_ms,
                [3.5 * math.sin(2 * math.pi * 3 * i / 20) for i in range(n)],
                [7.2] * n,  # tilted sharply from flat Z to Y
                [6.6] * n,
                [1.4] * n,  # strong rotation while lifting
                [0.5] * n,
                [0.2] * n,
            )
            pick_body = json.loads(self._post(ingest, pick_payload)["body"])
            self.assertEqual(pick_body["status"], "disarmed")
            self.assertEqual(pick_body["device_state"], "UNARMED")
            self.assertEqual(pick_body["triggers_count"], 0)
            self.assertEqual(len(mock_sns.published), 0)

            # 4) Violent typing on desk next to ARMED phone-A (brief vertical Z impulses) -> 0 triggers!
            type_t0 = now_ms - 6000
            tz = [9.81] * n
            for idx in (3, 14, 23, 34):
                tz[idx] = 13.6  # +3.8 m/s^2 vertical bang
            type_payload = self._build_payload(
                "phone-A",
                type_t0,
                now_ms,
                [0.05] * n,
                [0.05] * n,
                tz,
                [0.02] * n,
                [0.02] * n,
                [0.02] * n,
            )
            type_body = json.loads(self._post(ingest, type_payload)["body"])
            self.assertEqual(type_body["triggers_count"], 0)
            self.assertEqual(len(mock_sns.published), 0)

            # 5) Real synchronized 4 Hz horizontal earthquake hits ARMED phone-A and phone-B!
            quake_t0 = now_ms - 2000
            shake_x = [3.8 * math.sin(2.0 * math.pi * 4.0 * i / 20.0) for i in range(n)]
            quake_a = self._build_payload(
                "phone-A",
                quake_t0,
                now_ms,
                shake_x,
                [0.1] * n,
                [9.81] * n,
                [0.03] * n,
                [0.02] * n,
                [0.02] * n,
            )
            body_a = json.loads(self._post(ingest, quake_a)["body"])
            self.assertEqual(body_a["status"], "processed")
            self.assertEqual(body_a["triggers_count"], 1)
            self.assertFalse(body_a["correlation"]["quake_detected"])  # Only 1 phone so far

            # Duplicate of phone-A quake batch must be ignored via RawBatches dedup
            dup_body = json.loads(self._post(ingest, quake_a)["body"])
            self.assertEqual(dup_body["status"], "duplicate_ignored")

            quake_b = self._build_payload(
                "phone-B",
                quake_t0 + 500,  # 0.5s later -> synchronized!
                now_ms,
                shake_x,
                [0.1] * n,
                [9.81] * n,
                [0.03] * n,
                [0.02] * n,
                [0.02] * n,
            )
            body_b = json.loads(self._post(ingest, quake_b)["body"])
            self.assertEqual(body_b["status"], "processed")
            self.assertEqual(body_b["triggers_count"], 1)
            self.assertTrue(body_b["correlation"]["quake_detected"])
            self.assertEqual(body_b["correlation"]["triggered_devices"], ["phone-A", "phone-B"])
            self.assertEqual(len(mock_alerts.items), 1)
            self.assertEqual(len(mock_sns.published), 1)

            # Verify GET /events never leaks internal __STATE__ records or phone-youtube
            get_resp = ingest.lambda_handler(
                {"requestContext": {"http": {"method": "GET", "path": "/events"}}, "rawPath": "/events"},
                None,
            )
            events_out = json.loads(get_resp["body"])["events"]
            self.assertTrue(all(e.get("timestamp") != "__STATE__" for e in events_out))
            self.assertTrue(all(e.get("device_id") != "phone-youtube" for e in events_out))
        finally:
            ingest._events_table = orig_events
            ingest._alerts_table = orig_alerts
            ingest._raw_table = orig_raw
            ingest._state_table = orig_state
            ingest._sns_client = orig_sns
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
