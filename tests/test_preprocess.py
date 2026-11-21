import copy
import json
import math

import pytest

from batches import EXAMPLE, RECV, SENT, T0, make_batch
from preprocess import preprocess


def assert_clean_shape(r):
    s = r["signal"]
    assert r["format"] == "clean_batch_v1"
    assert s["n_samples"] == s["duration_s"] * s["fs_hz"]
    for sensor in ("acc", "gyro"):
        for k in "xyz":
            vals = s[sensor][k]
            assert len(vals) == s["n_samples"]
            assert all(math.isfinite(v) for v in vals)


# ---------------------------------------------------------------- acceptanta

def test_table_batch_ok_without_warnings():
    r = preprocess(make_batch(), RECV)
    assert r["status"] == "ok", r
    assert r["warnings"] == []
    assert r["quality"]["gravity_norm"] == pytest.approx(9.81, abs=0.05)
    assert (r["signal"]["n_samples"], r["signal"]["duration_s"]) == (40, 2)
    assert_clean_shape(r)
    assert max(abs(v) for v in r["signal"]["acc"]["z"]) < 0.05


def test_knock_peak_visible_in_acc():
    r = preprocess(make_batch(knock_at=17), RECV)
    assert r["status"] == "ok", r
    z = r["signal"]["acc"]["z"]
    peak = max(range(len(z)), key=lambda i: abs(z[i]))
    assert peak in (16, 17, 18)
    assert abs(z[peak]) > 3.0
    assert_clean_shape(r)


def test_example_payload_is_too_short():
    r = preprocess(EXAMPLE, RECV)
    assert (r["status"], r["reason"]) == ("rejected", "BATCH_TOO_SHORT")
    assert (r["device_id"], r["source_t0_ms"]) == ("ios-01", T0)


def test_list_length_mismatch_is_schema_invalid():
    p = make_batch()
    p["accelerometer"]["x"].pop()
    assert preprocess(p, RECV)["reason"] == "SCHEMA_INVALID"


def test_schema_version_2_is_unsupported():
    p = make_batch()
    p["schema_version"] = 2
    assert preprocess(p, RECV)["reason"] == "UNSUPPORTED_VERSION"


# ---------------------------------------------------------------- reguli

def test_stale_batch():
    assert preprocess(make_batch(), SENT + 61_000)["reason"] == "BATCH_STALE"


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("schema_version"),
    lambda p: p.update(location=None),
    lambda p: p.update(device_id=""),
    lambda p: p.update(unit="furlong/s^2"),
])
def test_schema_invalid(mutate):
    p = make_batch()
    mutate(p)
    assert preprocess(p, RECV)["reason"] == "SCHEMA_INVALID"


@pytest.mark.parametrize("sensor,value", [("accelerometer", 85.0), ("gyroscope", 40.0)])
def test_out_of_range(sensor, value):
    p = make_batch()
    p[sensor]["x"][5] = value
    assert preprocess(p, RECV)["reason"] == "OUT_OF_RANGE"


def test_missing_gyro_gives_null_and_warning():
    p = make_batch()
    del p["gyroscope"]
    r = preprocess(p, RECV)
    assert r["status"] == "ok"
    assert r["signal"]["gyro"] is None
    assert r["quality"]["has_gyro"] is False
    assert "NO_GYRO" in r["warnings"]


def test_gps_low_accuracy_is_only_a_warning():
    p = make_batch()
    p["location"]["accuracy"] = 250
    r = preprocess(p, RECV)
    assert r["status"] == "ok"
    assert "GPS_LOW_ACCURACY" in r["warnings"]
    assert r["quality"]["gps_ok"] is False


def test_g_units_are_converted():
    p = make_batch()
    p["unit"] = "g"
    for k in "xyz":
        p["accelerometer"][k] = [v / 9.81 for v in p["accelerometer"][k]]
    r = preprocess(p, RECV)
    assert r["warnings"] == []
    assert r["quality"]["gravity_norm"] == pytest.approx(9.81, abs=0.05)


def test_gravity_mismatch_warning():
    p = make_batch()
    p["accelerometer"]["z"] = [v + 3 for v in p["accelerometer"]["z"]]
    assert "GRAVITY_MISMATCH" in preprocess(p, RECV)["warnings"]


def test_timestamp_mismatch_warning():
    p = make_batch()
    p["timestamp"] = "2026-11-20T19:20:00.000Z"
    r = preprocess(p, RECV)
    assert r["status"] == "ok"
    assert "TIMESTAMP_MISMATCH" in r["warnings"]


def test_nan_unsorted_and_duplicate_samples_are_cleaned():
    p = make_batch(n=44)
    a = p["accelerometer"]
    a["x"][10] = float("nan")
    a["y"][11] = None
    a["t"][20], a["t"][21] = a["t"][21], a["t"][20]
    for k, v in (("t", a["t"][30]), ("x", 0), ("y", 0), ("z", 9.81)):
        a[k].append(v)
    r = preprocess(p, RECV)
    assert r["status"] == "ok", r
    assert r["quality"]["n_raw_samples"] == 45
    assert r["signal"]["n_samples"] == 40


def _drop(p, lo, hi):
    for k in "txyz":
        del p["accelerometer"][k][lo:hi]
    return p


def test_small_gap_is_warning():
    r = preprocess(_drop(make_batch(), 10, 13), RECV)
    assert r["status"] == "ok"
    assert "GAPS_PRESENT" in r["warnings"]


def test_large_gap_is_rejected():
    assert preprocess(_drop(make_batch(), 5, 20), RECV)["reason"] == "TOO_MANY_GAPS"


def test_partial_second_is_dropped():
    r = preprocess(make_batch(n=55), RECV)
    assert (r["signal"]["duration_s"], r["signal"]["n_samples"]) == (2, 40)


def test_output_is_strict_json_and_input_untouched():
    p = make_batch()
    before = copy.deepcopy(p)
    json.dumps(preprocess(p, RECV), allow_nan=False)
    assert p == before
