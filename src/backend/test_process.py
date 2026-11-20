"""Pure-Python unit tests for process.py and correlate.py."""

import math
import os
import sys
import unittest

# Ensure src/backend is importable regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from correlate import correlate_triggers, haversine_km
from process import process_clean_batch


def _make_history(count, t0_ms, rms=0.1, is_stationary=True):
    return [
        {
            "t_ms": t0_ms - (count - i) * 1000,
            "rms": rms,
            "is_stationary": is_stationary,
        }
        for i in range(count)
    ]


def _make_clean_batch(
    t0_ms=1795202100000,
    acc_x=None,
    acc_y=None,
    acc_z=None,
    gyro_val=0.05,
    warnings=None,
    location=None,
    sample_rate_hz=20,
    device_id="ios-01",
):
    n = sample_rate_hz
    if acc_x is None:
        acc_x = [0.05] * n
    if acc_y is None:
        acc_y = [0.0] * n
    if acc_z is None:
        acc_z = [0.0] * n

    gyro = (
        None
        if gyro_val is None
        else {
            "x": [gyro_val] * n,
            "y": [0.0] * n,
            "z": [0.0] * n,
        }
    )

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
        "warnings": warnings if warnings is not None else [],
    }


def _make_4hz_shake_signal(sample_rate_hz=20, amplitude=3.0, dc_bias=3.5):
    """Return a 1-second 4 Hz synthetic signal whose magnitude oscillates at 4 Hz."""
    return [
        dc_bias + amplitude * math.sin(2.0 * math.pi * 4.0 * i / sample_rate_hz)
        for i in range(sample_rate_hz)
    ]


class FakeTable:
    """Minimal in-memory DynamoDB table mock for unit tests."""

    def __init__(self, items=None):
        self.items = list(items or [])

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def put_item(self, Item):
        self.items.append(dict(Item))
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
        shake = _make_4hz_shake_signal()
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_val=0.05)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["features"]), 1)
        self.assertFalse(result["features"][0]["is_trigger"])
        self.assertEqual(len(result["triggers"]), 0)

    def test_high_gyro_rms_does_not_trigger(self):
        t0 = 1795202100000
        # Device was being shaken in hand (not stationary in preceding windows)
        history = _make_history(count=20, t0_ms=t0, rms=0.1, is_stationary=False)
        shake = _make_4hz_shake_signal()
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_val=2.5)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["features"]), 1)
        self.assertGreaterEqual(result["features"][0]["gyro_rms"], 2.0)
        self.assertFalse(result["features"][0]["is_stationary"])
        self.assertFalse(result["features"][0]["is_trigger"])
        self.assertEqual(len(result["triggers"]), 0)

    def test_stationary_phone_with_4hz_shake_triggers_accurately(self):
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.2, is_stationary=True)
        shake = _make_4hz_shake_signal(sample_rate_hz=20, amplitude=3.0, dc_bias=3.5)
        batch = _make_clean_batch(t0_ms=t0, acc_x=shake, gyro_val=0.05)

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["features"]), 1)
        self.assertEqual(len(result["triggers"]), 1)

        feat = result["features"][0]
        trig = result["triggers"][0]

        self.assertTrue(feat["is_trigger"])
        self.assertAlmostEqual(feat["pga"], max(shake), places=4)
        self.assertGreater(feat["sta_lta"], 15.0)
        self.assertEqual(feat["dominant_freq_hz"], 4)
        self.assertEqual(trig["dominant_freq_hz"], 4)
        self.assertAlmostEqual(trig["pga"], feat["pga"], places=4)
        self.assertAlmostEqual(trig["sta_lta"], feat["sta_lta"], places=4)

    def test_gps_low_accuracy_warning_does_not_block_trigger(self):
        t0 = 1795202100000
        history = _make_history(count=20, t0_ms=t0, rms=0.2, is_stationary=True)
        shake = _make_4hz_shake_signal()
        batch = _make_clean_batch(
            t0_ms=t0,
            acc_x=shake,
            gyro_val=0.05,
            warnings=["GPS_LOW_ACCURACY"],
            location={"lat": 46.7712, "lon": 23.6236, "accuracy": 250.0},
        )

        result = process_clean_batch(batch, history)

        self.assertEqual(len(result["triggers"]), 1)
        self.assertTrue(result["features"][0]["is_trigger"])
        self.assertIn("GPS_LOW_ACCURACY", result["features"][0]["warnings"])


class TestCorrelateTriggers(unittest.TestCase):
    def test_haversine_distance_accuracy(self):
        # Cluj-Napoca center to ~20 km north
        d_same = haversine_km(46.7712, 23.6236, 46.7712, 23.6236)
        self.assertAlmostEqual(d_same, 0.0, places=5)

        d_far = haversine_km(46.7712, 23.6236, 46.9512, 23.6236)
        self.assertGreater(d_far, 19.0)
        self.assertLess(d_far, 21.0)

    def test_quorum_and_spatial_filtering(self):
        t_ms = 1795202100000
        base_lat, base_lon = 46.7712, 23.6236

        # 4 phones within 5 km, only 1 triggered + 1 triggered phone 20 km away
        events_1_of_4 = [
            {
                "device_id": "phone-1",
                "t_ms": t_ms,
                "lat": base_lat,
                "lon": base_lon,
                "is_trigger": True,
                "pga": 4.2,
            },
            {
                "device_id": "phone-2",
                "t_ms": t_ms - 1000,
                "lat": base_lat + 0.005,
                "lon": base_lon + 0.005,
                "is_trigger": False,
                "pga": 0.2,
            },
            {
                "device_id": "phone-3",
                "t_ms": t_ms - 2000,
                "lat": base_lat - 0.005,
                "lon": base_lon,
                "is_trigger": False,
                "pga": 0.1,
            },
            {
                "device_id": "phone-4",
                "t_ms": t_ms - 1500,
                "lat": base_lat,
                "lon": base_lon - 0.005,
                "is_trigger": False,
                "pga": 0.15,
            },
            # Phone 20 km north (should NOT count toward zone)
            {
                "device_id": "phone-far",
                "t_ms": t_ms,
                "lat": base_lat + 0.18,
                "lon": base_lon,
                "is_trigger": True,
                "pga": 6.0,
            },
        ]

        new_trig = [
            {
                "device_id": "phone-1",
                "t_ms": t_ms,
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
        alerts_table = FakeTable()
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
        self.assertAlmostEqual(res_1["consensus_ratio"], 0.25, places=4)
        self.assertFalse(res_1["event_created"])
        self.assertEqual(len(alerts_table.items), 0)
        self.assertEqual(len(sns.published), 0)

        # Now phone-2 also triggers -> 2/4 (50%) within 5 km -> quake_detected: True
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


if __name__ == "__main__":
    unittest.main()
