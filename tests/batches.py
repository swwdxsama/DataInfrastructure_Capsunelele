"""Batch-uri sintetice (contract v1) pentru teste."""
import copy
import math
import random

T0 = 1795202100000
SENT = T0 + 2050
RECV = T0 + 2310

EXAMPLE = {
    "schema_version": 1,
    "event_type": "raw_sensor_batch",
    "device_id": "ios-01",
    "platform": "ios",
    "sent_at": SENT,
    "timestamp": "2026-11-20T19:15:02.050Z",
    "value": 11.42,
    "unit": "m/s^2",
    "gyro_unit": "rad/s",
    "sample_rate_hz": 20,
    "location": {"lat": 46.7712, "lon": 23.6236, "accuracy": 8.5},
    "accelerometer": {
        "t0": T0, "t": [0, 50, 100],
        "x": [0.02, 2.45, 0.10], "y": [-0.01, -1.80, 0.05], "z": [9.81, 11.20, 9.79],
    },
    "gyroscope": {
        "t0": T0, "t": [0, 50, 100],
        "x": [0.01, 0.03, 0.01], "y": [0.00, 0.02, 0.00], "z": [0.01, 0.01, 0.01],
    },
}


def make_batch(n=40, jitter_ms=3, knock_at=None, seed=1):
    """Telefon pe masa (zgomot mic, gravitatie pe z), optional o lovitura la esantionul knock_at."""
    rnd = random.Random(seed)
    t = [0] + [i * 50 + rnd.randint(-jitter_ms, jitter_ms) for i in range(1, n)]
    acc = {
        "t0": T0, "t": t,
        "x": [0.02 + rnd.gauss(0, 0.005) for _ in t],
        "y": [-0.01 + rnd.gauss(0, 0.005) for _ in t],
        "z": [9.81 + rnd.gauss(0, 0.005) for _ in t],
    }
    if knock_at is not None:
        acc["z"][knock_at] += 6.0
        acc["x"][knock_at] += 2.0
    gyro = {"t0": T0, "t": list(t), **{k: [rnd.gauss(0, 0.002) for _ in t] for k in "xyz"}}
    p = copy.deepcopy(EXAMPLE)
    p["accelerometer"], p["gyroscope"] = acc, gyro
    p["value"] = max(math.sqrt(x * x + y * y + z * z) for x, y, z in zip(acc["x"], acc["y"], acc["z"]))
    return p
