"""Pure-Python seismic signal processing (Contract clean_batch_v1).

No AWS SDK or third-party packages are imported here.
Includes human-handling rejection (gyro RMS/peak + gravity tilt delta),
desk-typing/impulse rejection (crest factor, active fraction, horizontal energy
ratio, seismic frequency band), and multi-second sustained shake confirmation.
"""

import math
import os
from datetime import datetime, timezone

RAD2DEG = 180.0 / math.pi


def _load_config(config=None):
    overrides = config or {}

    def _f(key, default):
        return float(overrides.get(key, os.environ.get(key, str(default))))

    def _i(key, default):
        return int(overrides.get(key, os.environ.get(key, str(default))))

    return {
        # Amplitude & STA/LTA thresholds
        "SPIKE_ACC_THRESHOLD": _f("SPIKE_ACC_THRESHOLD", 2.5),
        "MIN_TRIGGER_RMS": _f("MIN_TRIGGER_RMS", 0.75),
        "STA_LTA_RATIO": _f("STA_LTA_RATIO", 3.0),
        "LTA_FLOOR": _f("LTA_FLOOR", 0.05),
        "LTA_SEC": _i("LTA_SEC", 30),
        "WARMUP_SEC": _i("WARMUP_SEC", 15),
        # Stationary & human-handling thresholds
        "STATIONARY_WINDOW_SEC": _i("STATIONARY_WINDOW_SEC", 10),
        "STATIONARY_GYRO_RMS": _f("STATIONARY_GYRO_RMS", 0.20),
        "TRIGGER_MAX_GYRO_RMS": _f("TRIGGER_MAX_GYRO_RMS", 0.50),
        "TRIGGER_MAX_GYRO_PEAK": _f("TRIGGER_MAX_GYRO_PEAK", 1.10),
        "MAX_TILT_CHANGE_DEG": _f("MAX_TILT_CHANGE_DEG", 12.0),
        # Seismic signature filters (reject typing / table knocks / single impulses)
        "MAX_CREST_FACTOR": _f("MAX_CREST_FACTOR", 3.5),
        "ACTIVE_ACC_THRESHOLD": _f("ACTIVE_ACC_THRESHOLD", 0.6),
        "MIN_ACTIVE_FRACTION": _f("MIN_ACTIVE_FRACTION", 0.30),
        "MIN_HORIZONTAL_RATIO": _f("MIN_HORIZONTAL_RATIO", 0.30),
        "SEISMIC_FREQ_MIN_HZ": _i("SEISMIC_FREQ_MIN_HZ", 1),
        "SEISMIC_FREQ_MAX_HZ": _i("SEISMIC_FREQ_MAX_HZ", 8),
        "MIN_SUSTAINED_WINDOWS": _i("MIN_SUSTAINED_WINDOWS", 2),
    }


def _ms_to_iso(t_ms):
    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_valid_number(val):
    if isinstance(val, bool) or val is None:
        return False
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _vector_angle_deg(v1, v2):
    if not (isinstance(v1, (list, tuple)) and isinstance(v2, (list, tuple))):
        return None
    if len(v1) != 3 or len(v2) != 3:
        return None
    if not (all(_is_valid_number(x) for x in v1) and all(_is_valid_number(x) for x in v2)):
        return None
    a = [float(x) for x in v1]
    b = [float(x) for x in v2]
    n1 = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    n2 = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    cos_val = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (n1 * n2)
    cos_val = max(-1.0, min(1.0, cos_val))
    return math.acos(cos_val) * RAD2DEG


def _unit_vector(v):
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return None
    if not all(_is_valid_number(x) for x in v):
        return None
    a = [float(x) for x in v]
    norm = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    if norm < 1e-6:
        return None
    return (a[0] / norm, a[1] / norm, a[2] / norm)


def _extract_valid_location(location):
    if not isinstance(location, dict):
        return None, None, False
    lat = location.get("lat")
    lon = location.get("lon")
    if _is_valid_number(lat) and _is_valid_number(lon):
        return float(lat), float(lon), True
    return None, None, False


def _lowpass_3pt(vals):
    """Lightweight 3-point [0.25, 0.5, 0.25] low-pass filter for high sample rates (>=30 Hz)."""
    n = len(vals)
    if n < 3:
        return list(vals)
    out = [vals[0]]
    for i in range(1, n - 1):
        out.append(0.25 * vals[i - 1] + 0.5 * vals[i] + 0.25 * vals[i + 1])
    out.append(vals[-1])
    return out


def _dominant_frequency_hz(ax_win, ay_win, az_win, sample_rate_hz):
    """Compute dominant oscillation frequency via 3-axis signed DFT (avoids rectification doubling)."""
    n = len(ax_win)
    if n == 0 or sample_rate_hz < 2:
        return 0

    mx = sum(ax_win) / n
    my = sum(ay_win) / n
    mz = sum(az_win) / n
    zx = [v - mx for v in ax_win]
    zy = [v - my for v in ay_win]
    zz = [v - mz for v in az_win]

    max_freq = sample_rate_hz // 2
    best_freq = 1
    best_power = -1.0
    two_pi_over_n = (2.0 * math.pi) / n

    for freq in range(1, max_freq + 1):
        rx = ix = ry = iy = rz = iz = 0.0
        w = two_pi_over_n * freq
        for idx in range(n):
            angle = w * idx
            c = math.cos(angle)
            s = math.sin(angle)
            rx += zx[idx] * c
            ix -= zx[idx] * s
            ry += zy[idx] * c
            iy -= zy[idx] * s
            rz += zz[idx] * c
            iz -= zz[idx] * s
        power = (rx * rx + ix * ix) + (ry * ry + iy * iy) + (rz * rz + iz * iz)
        if power > best_power:
            best_power = power
            best_freq = freq

    return best_freq


def process_clean_batch(clean_batch, history, config=None, device_state=None):
    """Extract 1-second seismic features, trigger decisions, and disarm feedback.

    Args:
        clean_batch: Adapted clean_batch_v1 dictionary.
        history: List of prior 1-second feature records for this device.
        config: Optional dict overriding env thresholds.
        device_state: Optional persistent DeviceState dict (state, static_sec,
            baseline_gravity, lta_rms).

    Returns:
        {
            "features": [...],
            "triggers": [...],
            "disarm_device": bool,
            "disarm_reason": str,
        }
    """
    cfg = _load_config(config)

    device_id = str(clean_batch["device_id"])
    t0 = int(clean_batch["t0"])
    sample_rate_hz = int(clean_batch.get("sample_rate_hz", 20))
    acc = clean_batch["acc"]
    gyro = clean_batch.get("gyro")
    warnings = list(clean_batch.get("warnings") or [])

    lat, lon, has_valid_location = _extract_valid_location(clean_batch.get("location"))

    acc_x = [float(v) for v in acc["x"]]
    acc_y = [float(v) for v in acc["y"]]
    acc_z = [float(v) for v in acc["z"]]
    total_samples = min(len(acc_x), len(acc_y), len(acc_z))
    acc_x, acc_y, acc_z = acc_x[:total_samples], acc_y[:total_samples], acc_z[:total_samples]

    # Suppress >10 Hz desk-tap harmonics on high-rate devices without altering 20 Hz signals
    if sample_rate_hz >= 30:
        acc_x = _lowpass_3pt(acc_x)
        acc_y = _lowpass_3pt(acc_y)
        acc_z = _lowpass_3pt(acc_z)

    num_windows = total_samples // sample_rate_hz if sample_rate_hz > 0 else 0

    # Gravity orientation metadata for tilt detection & horizontal/vertical decomposition
    gvec = clean_batch.get("gravity_vector")
    g_end = clean_batch.get("gravity_end") or gvec
    intra_tilt_deg = clean_batch.get("intra_batch_tilt_deg")
    g_unit = _unit_vector(gvec)

    state = device_state if isinstance(device_state, dict) else {}
    baseline_gravity = state.get("baseline_gravity")
    state_armed = state.get("state") == "ARMED"
    state_static_sec = int(state.get("static_sec", 0))
    state_lta_rms = state.get("lta_rms")
    state_lta_rms = float(state_lta_rms) if _is_valid_number(state_lta_rms) else None

    tilt_angles = []
    if _is_valid_number(intra_tilt_deg):
        tilt_angles.append(float(intra_tilt_deg))
    if baseline_gravity is not None:
        a1 = _vector_angle_deg(baseline_gravity, gvec) if gvec is not None else None
        a2 = _vector_angle_deg(baseline_gravity, g_end) if g_end is not None else None
        for ang in (a1, a2):
            if ang is not None:
                tilt_angles.append(ang)
    tilt_change_deg = max(tilt_angles) if tilt_angles else None
    tilt_changed = (
        tilt_change_deg is not None and tilt_change_deg >= cfg["MAX_TILT_CHANGE_DEG"]
    )

    # Normalize history (oldest -> newest)
    prior_windows = []
    for item in history or []:
        prior_windows.append(
            {
                "t_ms": int(item.get("t_ms", 0)),
                "rms": float(item.get("rms", 0.0)),
                "is_stationary": bool(item.get("is_stationary", False)),
                "is_candidate": bool(item.get("is_candidate", item.get("is_trigger", False))),
            }
        )
    prior_windows.sort(key=lambda w: w["t_ms"])

    # Separate calm LTA baseline windows so active shaking does not corrupt LTA
    lta_windows = [w for w in prior_windows if not w["is_candidate"]]

    # Track trailing consecutive candidate windows from history
    consecutive_candidates = 0
    last_window_t_ms = None
    for w in prior_windows:
        if last_window_t_ms is not None and (w["t_ms"] - last_window_t_ms) > 2500:
            consecutive_candidates = 0
        if w["is_candidate"]:
            consecutive_candidates += 1
        else:
            consecutive_candidates = 0
        last_window_t_ms = w["t_ms"]

    features = []
    triggers = []
    disarm_device = False
    disarm_reason = ""

    lta_sec = cfg["LTA_SEC"]
    warmup_sec = cfg["WARMUP_SEC"]
    stationary_window_sec = cfg["STATIONARY_WINDOW_SEC"]
    stationary_gyro_rms = cfg["STATIONARY_GYRO_RMS"]
    trigger_max_gyro_rms = cfg["TRIGGER_MAX_GYRO_RMS"]
    trigger_max_gyro_peak = cfg["TRIGGER_MAX_GYRO_PEAK"]
    sta_lta_ratio = cfg["STA_LTA_RATIO"]
    lta_floor = cfg["LTA_FLOOR"]
    spike_acc_threshold = cfg["SPIKE_ACC_THRESHOLD"]
    min_trigger_rms = cfg["MIN_TRIGGER_RMS"]
    max_crest_factor = cfg["MAX_CREST_FACTOR"]
    active_acc_threshold = cfg["ACTIVE_ACC_THRESHOLD"]
    min_active_fraction = cfg["MIN_ACTIVE_FRACTION"]
    min_horizontal_ratio = cfg["MIN_HORIZONTAL_RATIO"]
    freq_min_hz = cfg["SEISMIC_FREQ_MIN_HZ"]
    freq_max_hz = min(cfg["SEISMIC_FREQ_MAX_HZ"], max(1, sample_rate_hz // 2))
    min_sustained_windows = max(1, cfg["MIN_SUSTAINED_WINDOWS"])

    for k in range(num_windows):
        start = k * sample_rate_hz
        end = start + sample_rate_hz
        window_t_ms = t0 + k * 1000
        window_ts = _ms_to_iso(window_t_ms)

        if last_window_t_ms is not None and (window_t_ms - last_window_t_ms) > 2500:
            consecutive_candidates = 0

        mags = []
        horiz_sq_sum = 0.0
        vert_sq_sum = 0.0
        for i in range(start, end):
            ax_i, ay_i, az_i = acc_x[i], acc_y[i], acc_z[i]
            sq = ax_i * ax_i + ay_i * ay_i + az_i * az_i
            mags.append(math.sqrt(sq))
            if g_unit is not None:
                v_proj = ax_i * g_unit[0] + ay_i * g_unit[1] + az_i * g_unit[2]
                v_sq = v_proj * v_proj
                vert_sq_sum += v_sq
                horiz_sq_sum += max(0.0, sq - v_sq)

        n = len(mags)
        rms = math.sqrt(sum(m * m for m in mags) / n)
        pga = max(mags)
        mean_m = sum(mags) / n
        variance = sum((m - mean_m) ** 2 for m in mags) / n
        crest_factor = pga / max(rms, 1e-6)
        active_fraction = sum(1 for m in mags if m >= active_acc_threshold) / n
        dominant_freq_hz = _dominant_frequency_hz(acc_x[start:end], acc_y[start:end], acc_z[start:end], sample_rate_hz)

        horiz_ratio = None
        if g_unit is not None:
            horiz_rms = math.sqrt(horiz_sq_sum / n)
            horiz_ratio = horiz_rms / max(rms, 1e-6)

        # Gyroscope RMS and peak for this window
        gyro_rms = None
        gyro_peak = None
        if (
            isinstance(gyro, dict)
            and len(gyro.get("x", [])) >= end
            and len(gyro.get("y", [])) >= end
            and len(gyro.get("z", [])) >= end
        ):
            gx = gyro["x"]
            gy = gyro["y"]
            gz = gyro["z"]
            g_mags = [
                math.sqrt(
                    float(gx[i]) ** 2 + float(gy[i]) ** 2 + float(gz[i]) ** 2
                )
                for i in range(start, end)
            ]
            gyro_rms = math.sqrt(sum(gm * gm for gm in g_mags) / n)
            gyro_peak = max(g_mags)

        high_gyro = (
            (gyro_rms is not None and gyro_rms >= trigger_max_gyro_rms)
            or (gyro_peak is not None and gyro_peak >= trigger_max_gyro_peak)
        )
        is_handling = high_gyro or tilt_changed

        if is_handling:
            disarm_device = True
            if not disarm_reason:
                disarm_reason = "TILT_CHANGE" if tilt_changed else "HIGH_GYRO"

        is_stationary = (
            gyro_rms is not None
            and gyro_rms < stationary_gyro_rms
            and (gyro_peak is None or gyro_peak < trigger_max_gyro_peak)
            and not tilt_changed
        )

        # STA/LTA against preceding calm LTA windows (or DeviceState baseline LTA)
        if len(lta_windows) > 0:
            lta_slice = lta_windows[-lta_sec:] if lta_sec > 0 else lta_windows
            lta_mean = sum(w["rms"] for w in lta_slice) / len(lta_slice)
            if state_lta_rms is not None and len(lta_slice) < warmup_sec:
                # Blend persistent calm baseline when history table is still short
                w_hist = len(lta_slice) / max(warmup_sec, 1)
                lta_mean = w_hist * lta_mean + (1.0 - w_hist) * state_lta_rms
            sta_lta = rms / max(lta_mean, lta_floor)
        elif state_lta_rms is not None:
            sta_lta = rms / max(state_lta_rms, lta_floor)
        else:
            sta_lta = 1.0

        warmed_up = state_armed or (len(prior_windows) >= warmup_sec)

        if state_armed:
            was_stationary = True
        elif stationary_window_sec <= 0:
            was_stationary = True
        elif len(prior_windows) >= stationary_window_sec:
            was_stationary = all(
                w["is_stationary"] for w in prior_windows[-stationary_window_sec:]
            )
        else:
            was_stationary = False

        freq_in_band = freq_min_hz <= dominant_freq_hz <= freq_max_hz
        horiz_ok = horiz_ratio is None or horiz_ratio >= min_horizontal_ratio

        is_candidate = (
            warmed_up
            and was_stationary
            and not is_handling
            and (gyro_rms is not None)
            and has_valid_location
            and sta_lta >= sta_lta_ratio
            and pga >= spike_acc_threshold
            and rms >= min_trigger_rms
            and crest_factor <= max_crest_factor
            and active_fraction >= min_active_fraction
            and freq_in_band
            and horiz_ok
        )

        if is_candidate:
            consecutive_candidates += 1
        else:
            consecutive_candidates = 0

        is_trigger = is_candidate and (consecutive_candidates >= min_sustained_windows)
        t_onset_ms = (
            window_t_ms - (consecutive_candidates - 1) * 1000
            if is_candidate
            else None
        )

        feature = {
            "device_id": device_id,
            "t_ms": window_t_ms,
            "timestamp": window_ts,
            "rms": round(rms, 6),
            "pga": round(pga, 6),
            "variance": round(variance, 6),
            "crest_factor": round(crest_factor, 4),
            "active_fraction": round(active_fraction, 4),
            "horiz_ratio": round(horiz_ratio, 4) if horiz_ratio is not None else None,
            "dominant_freq_hz": dominant_freq_hz,
            "gyro_rms": round(gyro_rms, 6) if gyro_rms is not None else None,
            "gyro_peak": round(gyro_peak, 6) if gyro_peak is not None else None,
            "tilt_change_deg": round(tilt_change_deg, 3) if tilt_change_deg is not None else None,
            "is_stationary": is_stationary,
            "is_handling": is_handling,
            "is_candidate": is_candidate,
            "sta_lta": round(sta_lta, 6),
            "is_trigger": is_trigger,
            "t_onset_ms": t_onset_ms,
            "lat": lat,
            "lon": lon,
            "warnings": warnings,
        }
        features.append(feature)

        if is_trigger:
            triggers.append(
                {
                    "device_id": device_id,
                    "t_ms": window_t_ms,
                    "t_onset_ms": t_onset_ms,
                    "timestamp": window_ts,
                    "lat": lat,
                    "lon": lon,
                    "pga": round(pga, 6),
                    "rms": round(rms, 6),
                    "sta_lta": round(sta_lta, 6),
                    "crest_factor": round(crest_factor, 4),
                    "active_fraction": round(active_fraction, 4),
                    "horiz_ratio": round(horiz_ratio, 4) if horiz_ratio is not None else None,
                    "dominant_freq_hz": dominant_freq_hz,
                }
            )

        window_summary = {
            "t_ms": window_t_ms,
            "rms": rms,
            "is_stationary": is_stationary,
            "is_candidate": is_candidate,
        }
        prior_windows.append(window_summary)
        if not is_candidate and not is_handling:
            lta_windows.append(window_summary)
        last_window_t_ms = window_t_ms

    return {
        "features": features,
        "triggers": triggers,
        "disarm_device": disarm_device,
        "disarm_reason": disarm_reason,
    }
