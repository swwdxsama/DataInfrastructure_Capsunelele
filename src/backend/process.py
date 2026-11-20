"""Pure-Python seismic signal processing (Contract clean_batch_v1).

No AWS SDK or third-party packages are imported here.
"""

import math
import os
from datetime import datetime, timezone


def _load_config(config=None):
    overrides = config or {}
    return {
        "SPIKE_ACC_THRESHOLD": float(
            overrides.get(
                "SPIKE_ACC_THRESHOLD",
                os.environ.get("SPIKE_ACC_THRESHOLD", "2.5"),
            )
        ),
        "STATIONARY_GYRO_RMS": float(
            overrides.get(
                "STATIONARY_GYRO_RMS",
                os.environ.get("STATIONARY_GYRO_RMS", "1.0"),
            )
        ),
        "STA_LTA_RATIO": float(
            overrides.get(
                "STA_LTA_RATIO",
                os.environ.get("STA_LTA_RATIO", "3.0"),
            )
        ),
        "LTA_SEC": int(
            overrides.get(
                "LTA_SEC",
                os.environ.get("LTA_SEC", "30"),
            )
        ),
        "WARMUP_SEC": int(
            overrides.get(
                "WARMUP_SEC",
                os.environ.get("WARMUP_SEC", "15"),
            )
        ),
        "STATIONARY_WINDOW_SEC": int(
            overrides.get(
                "STATIONARY_WINDOW_SEC",
                os.environ.get("STATIONARY_WINDOW_SEC", "10"),
            )
        ),
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


def _extract_valid_location(location):
    if not isinstance(location, dict):
        return None, None, False
    lat = location.get("lat")
    lon = location.get("lon")
    if _is_valid_number(lat) and _is_valid_number(lon):
        return float(lat), float(lon), True
    return None, None, False


def _dominant_frequency_hz(mags, sample_rate_hz):
    n = len(mags)
    if n == 0 or sample_rate_hz < 2:
        return 0

    mean_m = sum(mags) / n
    zero_mean = [m - mean_m for m in mags]
    max_freq = sample_rate_hz // 2

    best_freq = 1
    best_amp = -1.0
    two_pi_over_n = (2.0 * math.pi) / n

    for freq in range(1, max_freq + 1):
        re = 0.0
        im = 0.0
        w = two_pi_over_n * freq
        for idx, val in enumerate(zero_mean):
            angle = w * idx
            re += val * math.cos(angle)
            im -= val * math.sin(angle)
        amp = math.hypot(re, im)
        if amp > best_amp:
            best_amp = amp
            best_freq = freq

    return best_freq


def process_clean_batch(clean_batch, history, config=None):
    """Extract 1-second seismic features and trigger decisions from clean_batch_v1."""
    cfg = _load_config(config)

    device_id = str(clean_batch["device_id"])
    t0 = int(clean_batch["t0"])
    sample_rate_hz = int(clean_batch.get("sample_rate_hz", 20))
    acc = clean_batch["acc"]
    gyro = clean_batch.get("gyro")
    warnings = list(clean_batch.get("warnings") or [])

    lat, lon, has_valid_location = _extract_valid_location(clean_batch.get("location"))

    acc_x = acc["x"]
    acc_y = acc["y"]
    acc_z = acc["z"]
    total_samples = min(len(acc_x), len(acc_y), len(acc_z))
    num_windows = total_samples // sample_rate_hz if sample_rate_hz > 0 else 0

    # Normalize history (oldest -> newest)
    prior_windows = []
    for item in history or []:
        prior_windows.append(
            {
                "t_ms": int(item.get("t_ms", 0)),
                "rms": float(item.get("rms", 0.0)),
                "is_stationary": bool(item.get("is_stationary", False)),
            }
        )
    prior_windows.sort(key=lambda w: w["t_ms"])

    features = []
    triggers = []

    lta_sec = cfg["LTA_SEC"]
    warmup_sec = cfg["WARMUP_SEC"]
    stationary_window_sec = cfg["STATIONARY_WINDOW_SEC"]
    stationary_gyro_rms = cfg["STATIONARY_GYRO_RMS"]
    sta_lta_ratio = cfg["STA_LTA_RATIO"]
    spike_acc_threshold = cfg["SPIKE_ACC_THRESHOLD"]

    for k in range(num_windows):
        start = k * sample_rate_hz
        end = start + sample_rate_hz
        window_t_ms = t0 + k * 1000
        window_ts = _ms_to_iso(window_t_ms)

        mags = [
            math.sqrt(
                float(acc_x[i]) ** 2 + float(acc_y[i]) ** 2 + float(acc_z[i]) ** 2
            )
            for i in range(start, end)
        ]

        n = len(mags)
        rms = math.sqrt(sum(m * m for m in mags) / n)
        pga = max(mags)
        mean_m = sum(mags) / n
        variance = sum((m - mean_m) ** 2 for m in mags) / n
        dominant_freq_hz = _dominant_frequency_hz(mags, sample_rate_hz)

        # Gyroscope RMS for this window
        gyro_rms = None
        if (
            isinstance(gyro, dict)
            and len(gyro.get("x", [])) >= end
            and len(gyro.get("y", [])) >= end
            and len(gyro.get("z", [])) >= end
        ):
            gx = gyro["x"]
            gy = gyro["y"]
            gz = gyro["z"]
            gyro_sq_sum = sum(
                float(gx[i]) ** 2 + float(gy[i]) ** 2 + float(gz[i]) ** 2
                for i in range(start, end)
            )
            gyro_rms = math.sqrt(gyro_sq_sum / n)

        is_stationary = (
            gyro_rms is not None and gyro_rms < stationary_gyro_rms
        )

        # STA/LTA against preceding LTA_SEC windows
        if len(prior_windows) == 0:
            sta_lta = 1.0
        else:
            lta_slice = prior_windows[-lta_sec:] if lta_sec > 0 else prior_windows
            lta_mean = sum(w["rms"] for w in lta_slice) / len(lta_slice)
            sta_lta = rms / max(lta_mean, 1e-6)

        warmed_up = len(prior_windows) >= warmup_sec

        if stationary_window_sec <= 0:
            was_stationary = True
        elif len(prior_windows) >= stationary_window_sec:
            was_stationary = all(
                w["is_stationary"] for w in prior_windows[-stationary_window_sec:]
            )
        else:
            was_stationary = False

        is_trigger = (
            warmed_up
            and sta_lta >= sta_lta_ratio
            and pga >= spike_acc_threshold
            and was_stationary
            and (gyro is not None)
            and has_valid_location
        )

        feature = {
            "device_id": device_id,
            "t_ms": window_t_ms,
            "timestamp": window_ts,
            "rms": round(rms, 6),
            "pga": round(pga, 6),
            "variance": round(variance, 6),
            "dominant_freq_hz": dominant_freq_hz,
            "gyro_rms": round(gyro_rms, 6) if gyro_rms is not None else None,
            "is_stationary": is_stationary,
            "sta_lta": round(sta_lta, 6),
            "is_trigger": is_trigger,
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
                    "timestamp": window_ts,
                    "lat": lat,
                    "lon": lon,
                    "pga": round(pga, 6),
                    "rms": round(rms, 6),
                    "sta_lta": round(sta_lta, 6),
                    "dominant_freq_hz": dominant_freq_hz,
                }
            )

        prior_windows.append(
            {
                "t_ms": window_t_ms,
                "rms": rms,
                "is_stationary": is_stationary,
            }
        )

    return {
        "features": features,
        "triggers": triggers,
    }
