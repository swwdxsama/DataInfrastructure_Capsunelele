"""Preprocesare batch-uri de senzori: contract v1 (payload telefon) -> clean_batch_v1.

Python pur (math, datetime, os). Nu atinge baza de date: primeste date, intoarce date.
"""
import math
import os
from datetime import datetime

FORMAT = "clean_batch_v1"
G_MS2 = 9.81
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi

MAX_ACC_MS2 = 80.0
MAX_GYRO_RADS = 35.0
GPS_MAX_ACCURACY_M = 100.0
GRAVITY_TOLERANCE_MS2 = 1.0
TIMESTAMP_TOLERANCE_MS = 1000
MIN_GYRO_COVERAGE = 0.5  # fractiune din fereastra acoperita de giroscop

ACC_UNITS = {"m/s^2": 1.0, "m/s2": 1.0, "g": G_MS2}
GYRO_UNITS = {"rad/s": 1.0, "deg/s": DEG2RAD}


def default_config():
    return {
        "MIN_BATCH_SEC": float(os.environ.get("MIN_BATCH_SEC", "1")),
        "MAX_BATCH_AGE_SEC": float(os.environ.get("MAX_BATCH_AGE_SEC", "60")),
        "MAX_GAP_FRACTION": float(os.environ.get("MAX_GAP_FRACTION", "0.2")),
        "STATIC_GYRO_RMS": float(os.environ.get("STATIC_GYRO_RMS", "0.15")),
        "STATIC_GYRO_PEAK": float(os.environ.get("STATIC_GYRO_PEAK", "0.35")),
        "STATIC_ACC_RMS": float(os.environ.get("STATIC_ACC_RMS", "0.35")),
        "STATIC_ACC_PGA": float(os.environ.get("STATIC_ACC_PGA", "1.0")),
        "MAX_CALM_TILT_DEG": float(os.environ.get("MAX_CALM_TILT_DEG", "6.0")),
    }


class Rejected(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- helpers

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def vector_angle_deg(v1, v2):
    """Angle in degrees between two 3D vectors; returns None if either is invalid/zero."""
    if not (isinstance(v1, (list, tuple)) and isinstance(v2, (list, tuple))):
        return None
    if len(v1) != 3 or len(v2) != 3:
        return None
    if not (all(_is_num(x) for x in v1) and all(_is_num(x) for x in v2)):
        return None
    n1 = math.sqrt(v1[0] * v1[0] + v1[1] * v1[1] + v1[2] * v1[2])
    n2 = math.sqrt(v2[0] * v2[0] + v2[1] * v2[1] + v2[2] * v2[2])
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    cos_val = (v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]) / (n1 * n2)
    cos_val = max(-1.0, min(1.0, cos_val))
    return math.acos(cos_val) * RAD2DEG


def _reject(payload, reason, detail):
    device_id = payload.get("device_id") if isinstance(payload, dict) else None
    acc = payload.get("accelerometer") if isinstance(payload, dict) else None
    t0 = acc.get("t0") if isinstance(acc, dict) else None
    return {
        "format": FORMAT,
        "status": "rejected",
        "device_id": device_id if isinstance(device_id, str) else None,
        "source_t0_ms": t0 if _is_num(t0) else None,
        "reason": reason,
        "detail": detail,
    }


def _check_sensor(name, s):
    if not isinstance(s, dict):
        raise Rejected("SCHEMA_INVALID", name + " must be an object")
    if not _is_num(s.get("t0")):
        raise Rejected("SCHEMA_INVALID", name + ".t0 must be a number")
    lists = [s.get(k) for k in ("t", "x", "y", "z")]
    if not all(isinstance(l, list) for l in lists):
        raise Rejected("SCHEMA_INVALID", name + ".t/x/y/z must be lists")
    lens = {len(l) for l in lists}
    if len(lens) != 1:
        raise Rejected("SCHEMA_INVALID", name + ": t/x/y/z have different lengths %s" % sorted(lens))


def _validate_schema(p):
    if not isinstance(p, dict):
        raise Rejected("SCHEMA_INVALID", "payload must be a JSON object")
    if "schema_version" not in p:
        raise Rejected("SCHEMA_INVALID", "missing schema_version")
    v = p["schema_version"]
    if not (_is_num(v) and v == 1):
        raise Rejected("UNSUPPORTED_VERSION", "schema_version=%r, expected 1" % (v,))
    if p.get("event_type", "raw_sensor_batch") != "raw_sensor_batch":
        raise Rejected("SCHEMA_INVALID", "event_type must be raw_sensor_batch")
    if not isinstance(p.get("device_id"), str) or not p["device_id"].strip():
        raise Rejected("SCHEMA_INVALID", "missing device_id")
    if not _is_num(p.get("sent_at")):
        raise Rejected("SCHEMA_INVALID", "sent_at must be epoch ms")
    fs = p.get("sample_rate_hz")
    if not (_is_num(fs) and fs > 0 and float(fs).is_integer()):
        raise Rejected("SCHEMA_INVALID", "sample_rate_hz must be a positive integer")
    if p.get("unit", "m/s^2") not in ACC_UNITS:
        raise Rejected("SCHEMA_INVALID", "unsupported unit %r" % (p.get("unit"),))
    if p.get("gyro_unit", "rad/s") not in GYRO_UNITS:
        raise Rejected("SCHEMA_INVALID", "unsupported gyro_unit %r" % (p.get("gyro_unit"),))
    loc = p.get("location")
    if not isinstance(loc, dict) or not _is_num(loc.get("lat")) or not _is_num(loc.get("lon")):
        raise Rejected("SCHEMA_INVALID", "location.lat/lon missing")
    if not (-90 <= loc["lat"] <= 90 and -180 <= loc["lon"] <= 180):
        raise Rejected("SCHEMA_INVALID", "location.lat/lon out of range")
    _check_sensor("accelerometer", p.get("accelerometer"))
    gyro = p.get("gyroscope")
    if gyro is not None:
        _check_sensor("gyroscope", gyro)


def _parse_iso_ms(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000.0
    except (AttributeError, ValueError):
        return None


def _clean(sensor, scale):
    """-> lista sortata de (t_abs_ms, x, y, z) fara valori lipsa/NaN si fara t duplicat."""
    t0 = sensor["t0"]
    rows = []
    for t, x, y, z in zip(sensor["t"], sensor["x"], sensor["y"], sensor["z"]):
        if _is_num(t) and _is_num(x) and _is_num(y) and _is_num(z):
            rows.append((t0 + t, x * scale, y * scale, z * scale))
    rows.sort(key=lambda r: r[0])
    out = []
    for r in rows:
        if not out or r[0] != out[-1][0]:
            out.append(r)
    return out


def _push(xs, ys, zs, row):
    xs.append(row[1])
    ys.append(row[2])
    zs.append(row[3])


def _interp(rows, grid):
    """Interpolare liniara pe grila; in afara intervalului se pastreaza valoarea de la capat."""
    xs, ys, zs = [], [], []
    j = 0
    last = len(rows) - 1
    for tg in grid:
        while j < last - 1 and rows[j + 1][0] <= tg:
            j += 1
        a, b = rows[j], rows[min(j + 1, last)]
        if tg <= a[0] or a is b:
            _push(xs, ys, zs, a)
        elif tg >= b[0]:
            _push(xs, ys, zs, b)
        else:
            w = (tg - a[0]) / (b[0] - a[0])
            xs.append(a[1] + w * (b[1] - a[1]))
            ys.append(a[2] + w * (b[2] - a[2]))
            zs.append(a[3] + w * (b[3] - a[3]))
    return xs, ys, zs


def _moving_average(v, w):
    """Medie mobila centrata pe w esantioane (taiata la margini), O(n)."""
    n = len(v)
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    half = w // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i - half + w)
        out.append((pre[hi] - pre[lo]) / (hi - lo))
    return out


def _r(v, nd=5):
    return [round(x, nd) for x in v]


def _vec_stats(vec_dict):
    """Return (rms, peak) of Euclidean magnitude for {'x': [...], 'y': [...], 'z': [...]}."""
    if not isinstance(vec_dict, dict):
        return None, None
    xs, ys, zs = vec_dict.get("x", []), vec_dict.get("y", []), vec_dict.get("z", [])
    n = min(len(xs), len(ys), len(zs))
    if n == 0:
        return None, None
    sq_sum = 0.0
    peak = 0.0
    for i in range(n):
        m = math.sqrt(xs[i] * xs[i] + ys[i] * ys[i] + zs[i] * zs[i])
        sq_sum += m * m
        if m > peak:
            peak = m
    return math.sqrt(sq_sum / n), peak


def assess_batch_calmness(clean, baseline_gravity=None, config=None):
    """Pure-Python stability gate: decide if a clean_batch_v1 represents a static phone.

    Used by ingest.py to advance/reset a device's ARMED eligibility without keeping
    raw 30-batch queues in memory.
    """
    cfg = default_config()
    if config:
        cfg.update(config)

    sig = clean.get("signal") or {}
    qual = clean.get("quality") or {}

    acc_rms, acc_pga = _vec_stats(sig.get("acc"))
    gyro_rms, gyro_peak = _vec_stats(sig.get("gyro"))

    gvec = qual.get("gravity_vector")
    g_end = qual.get("gravity_end") or gvec
    intra_tilt = qual.get("intra_batch_tilt_deg")

    tilt_vs_baseline = None
    if baseline_gravity is not None:
        a1 = vector_angle_deg(baseline_gravity, gvec) if gvec is not None else None
        a2 = vector_angle_deg(baseline_gravity, g_end) if g_end is not None else None
        angles = [a for a in (a1, a2) if a is not None]
        if angles:
            tilt_vs_baseline = max(angles)

    reasons = []
    if gyro_rms is None or gyro_peak is None:
        reasons.append("NO_GYRO")
    else:
        if gyro_rms >= cfg["STATIC_GYRO_RMS"]:
            reasons.append("GYRO_RMS_HIGH")
        if gyro_peak >= cfg["STATIC_GYRO_PEAK"]:
            reasons.append("GYRO_PEAK_HIGH")

    if acc_rms is None or acc_pga is None:
        reasons.append("NO_ACC")
    else:
        if acc_rms >= cfg["STATIC_ACC_RMS"]:
            reasons.append("ACC_RMS_HIGH")
        if acc_pga >= cfg["STATIC_ACC_PGA"]:
            reasons.append("ACC_PGA_HIGH")

    max_calm_tilt = cfg["MAX_CALM_TILT_DEG"]
    if intra_tilt is not None and intra_tilt >= max_calm_tilt:
        reasons.append("INTRA_BATCH_TILT")
    if tilt_vs_baseline is not None and tilt_vs_baseline >= max_calm_tilt:
        reasons.append("BASELINE_TILT_DRIFT")

    return {
        "is_calm": len(reasons) == 0,
        "reasons": reasons,
        "acc_rms": round(acc_rms, 6) if acc_rms is not None else None,
        "acc_pga": round(acc_pga, 6) if acc_pga is not None else None,
        "gyro_rms": round(gyro_rms, 6) if gyro_rms is not None else None,
        "gyro_peak": round(gyro_peak, 6) if gyro_peak is not None else None,
        "intra_batch_tilt_deg": round(intra_tilt, 3) if intra_tilt is not None else None,
        "tilt_vs_baseline_deg": round(tilt_vs_baseline, 3) if tilt_vs_baseline is not None else None,
    }


# ---------------------------------------------------------------- main

def preprocess(payload, received_at_ms, config=None):
    cfg = default_config()
    if config:
        cfg.update(config)
    try:
        return _preprocess(payload, received_at_ms, cfg)
    except Rejected as e:
        return _reject(payload, e.reason, e.detail)


def _preprocess(p, received_at_ms, cfg):
    warnings = []

    # 1. Schema
    _validate_schema(p)
    fs = int(p["sample_rate_hz"])
    step = 1000.0 / fs

    # 2. Prospetime
    age_s = (received_at_ms - p["sent_at"]) / 1000.0
    if age_s > cfg["MAX_BATCH_AGE_SEC"]:
        raise Rejected("BATCH_STALE", "batch age %.1fs > %ss" % (age_s, cfg["MAX_BATCH_AGE_SEC"]))
    if "timestamp" in p:
        ts = _parse_iso_ms(p["timestamp"])
        if ts is None or abs(ts - p["sent_at"]) > TIMESTAMP_TOLERANCE_MS:
            warnings.append("TIMESTAMP_MISMATCH")

    # 3. Curatare (+ conversie unitati)
    acc_raw = p["accelerometer"]
    n_raw = len(acc_raw["t"])
    acc = _clean(acc_raw, ACC_UNITS[p.get("unit", "m/s^2")])
    gyro_src = p.get("gyroscope")
    gyro = _clean(gyro_src, GYRO_UNITS[p.get("gyro_unit", "rad/s")]) if gyro_src else []

    # 4. Domeniu fizic
    for t, x, y, z in acc:
        if math.sqrt(x * x + y * y + z * z) >= MAX_ACC_MS2:
            raise Rejected("OUT_OF_RANGE", "|a| >= %s m/s^2 at t=%s" % (MAX_ACC_MS2, t))
    for t, x, y, z in gyro:
        if math.sqrt(x * x + y * y + z * z) >= MAX_GYRO_RADS:
            raise Rejected("OUT_OF_RANGE", "|w| >= %s rad/s at t=%s" % (MAX_GYRO_RADS, t))

    # 5. Durata (secunde intregi pe grila; jumatate de pas toleranta pentru jitter la capat)
    if len(acc) < 2:
        raise Rejected("BATCH_TOO_SHORT", "%d valid accelerometer samples" % len(acc))
    span = acc[-1][0] - acc[0][0]
    n_grid = int((span + step / 2.0) // step) + 1
    duration_s = n_grid // fs
    if duration_s < cfg["MIN_BATCH_SEC"] or duration_s < 1:
        raise Rejected("BATCH_TOO_SHORT", "usable duration %d s (span %.0f ms)" % (duration_s, span))
    n = duration_s * fs

    # 6. Re-esantionare + goluri
    t_start = acc[0][0]
    grid = [t_start + i * step for i in range(n)]
    window_end = grid[-1]
    gap_ms = 0.0
    for a, b in zip(acc, acc[1:]):
        if a[0] >= window_end:
            break
        dt = min(b[0], window_end) - a[0]
        if b[0] - a[0] > 2 * step:
            gap_ms += dt
    gap_fraction = gap_ms / (window_end - t_start) if window_end > t_start else 0.0
    if gap_fraction > cfg["MAX_GAP_FRACTION"]:
        raise Rejected("TOO_MANY_GAPS", "gap_fraction %.3f > %s" % (gap_fraction, cfg["MAX_GAP_FRACTION"]))
    if gap_fraction > 0:
        warnings.append("GAPS_PRESENT")
    ax, ay, az = _interp(acc, grid)

    # 7. Giroscop pe aceeasi grila
    gyro_out = None
    if len(gyro) >= 2:
        covered = min(gyro[-1][0], window_end) - max(gyro[0][0], t_start)
        if window_end == t_start or covered / (window_end - t_start) >= MIN_GYRO_COVERAGE:
            gx, gy, gz = _interp(gyro, grid)
            gyro_out = {"x": _r(gx), "y": _r(gy), "z": _r(gz)}
    if gyro_out is None:
        warnings.append("NO_GYRO")

    # 8. Gravitatie + masurare schimbare de orientare in interiorul batch-ului
    gvec = [sum(ax) / n, sum(ay) / n, sum(az) / n]
    gnorm = math.sqrt(sum(g * g for g in gvec))
    if abs(gnorm - G_MS2) > GRAVITY_TOLERANCE_MS2:
        warnings.append("GRAVITY_MISMATCH")

    seg = max(1, min(fs // 2, n // 3)) if n >= 3 else 1
    g_start = [sum(ax[:seg]) / seg, sum(ay[:seg]) / seg, sum(az[:seg]) / seg]
    g_end = [sum(ax[-seg:]) / seg, sum(ay[-seg:]) / seg, sum(az[-seg:]) / seg]
    intra_tilt_deg = vector_angle_deg(g_start, g_end) or 0.0

    ax = [v - gvec[0] for v in ax]
    ay = [v - gvec[1] for v in ay]
    az = [v - gvec[2] for v in az]

    # 9. Trece-sus ~1 Hz: scade media mobila pe 1 s
    out_acc = {}
    for k, v in (("x", ax), ("y", ay), ("z", az)):
        ma = _moving_average(v, fs)
        out_acc[k] = _r([a - m for a, m in zip(v, ma)])

    # 10. GPS (doar avertisment)
    loc = p["location"]
    accuracy = loc.get("accuracy")
    accuracy = accuracy if _is_num(accuracy) else None
    gps_ok = accuracy is not None and accuracy <= GPS_MAX_ACCURACY_M
    if not gps_ok:
        warnings.append("GPS_LOW_ACCURACY")

    # 11. Iesire
    return {
        "format": FORMAT,
        "status": "ok",
        "device_id": p["device_id"],
        "platform": p.get("platform"),
        "source_t0_ms": acc_raw["t0"],
        "sent_at_ms": p["sent_at"],
        "received_at_ms": received_at_ms,
        "location": {"lat": loc["lat"], "lon": loc["lon"], "accuracy_m": accuracy},
        "signal": {
            "fs_hz": fs,
            "t_start_ms": t_start,
            "n_samples": n,
            "duration_s": duration_s,
            "acc": out_acc,
            "gyro": gyro_out,
        },
        "quality": {
            "has_gyro": gyro_out is not None,
            "gps_ok": gps_ok,
            "gap_fraction": round(gap_fraction, 4),
            "gravity_vector": _r(gvec, 4),
            "gravity_norm": round(gnorm, 4),
            "gravity_start": _r(g_start, 4),
            "gravity_end": _r(g_end, 4),
            "intra_batch_tilt_deg": round(intra_tilt_deg, 3),
            "n_raw_samples": n_raw,
        },
        "warnings": warnings,
    }
