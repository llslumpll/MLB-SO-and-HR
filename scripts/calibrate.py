"""
Empirical calibration: reads every graded HR and K entry ever saved, checks
whether the model's own past predictions actually matched what happened,
and writes a gentle correction to data/calibration.json for build_hr.py and
build_ko.py to apply on their next run.

This is NOT machine learning and does not touch the underlying formulas --
it's a well-understood statistical technique (empirical bias correction /
reliability calibration): compare predicted probability to actual outcome
rate, in buckets, and nudge future predictions toward reality.

Safety rails, stated plainly:
  - MIN_SAMPLE: no adjustment is applied to a confidence tier until it has
    at least this many graded entries. Below that, the correction is
    reported as "insufficient data" and left at neutral (no-op).
  - DAMPEN: only 30% of the raw computed correction is actually applied,
    every time this runs. Same "shrink toward the prior" philosophy used
    throughout the rest of the model -- this keeps a single unlucky/lucky
    stretch of games from swinging future projections too hard.
  - Bounded output: even after dampening, the final adjustment is clipped
    to a modest range as an extra rail.
  - Uses ALL graded history (not just recent days) while data is scarce.
    Worth revisiting once there's a few months of volume -- a rolling
    window would let the model adapt to real drift (e.g. league-wide K
    rates trending up) rather than being anchored to old seasons forever.
"""

import glob
import json
import os
from datetime import datetime

MIN_SAMPLE = 100
DAMPEN = 0.3
HR_MULT_BOUNDS = (0.75, 1.25)
K_BIAS_BOUNDS = (-1.5, 1.5)
TIERS = ["High", "Medium", "Low"]


def load_all_hr_entries():
    entries = []
    for path in sorted(glob.glob("data/hr/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    return [e for e in entries if e.get("graded") and e.get("heuristicProb")]


def load_all_ko_entries():
    entries = []
    for path in sorted(glob.glob("data/ko/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    return [e for e in entries if e.get("graded") and e.get("actualK") is not None]


def calibrate_hr(entries):
    result = {}
    for tier in TIERS:
        tier_entries = [e for e in entries if e.get("confidence") == tier]
        n = len(tier_entries)
        if n < MIN_SAMPLE:
            result[tier] = {"multiplier": 1.0, "sampleSize": n, "status": "insufficient data"}
            continue

        avg_predicted = sum(e["heuristicProb"] for e in tier_entries) / n
        actual_rate = sum(1 for e in tier_entries if e.get("hr")) / n

        if avg_predicted <= 0:
            result[tier] = {"multiplier": 1.0, "sampleSize": n, "status": "insufficient data"}
            continue

        raw_ratio = actual_rate / avg_predicted
        dampened = 1 + DAMPEN * (raw_ratio - 1)
        dampened = max(HR_MULT_BOUNDS[0], min(HR_MULT_BOUNDS[1], dampened))

        result[tier] = {
            "multiplier": round(dampened, 4),
            "sampleSize": n,
            "avgPredicted": round(avg_predicted, 4),
            "actualRate": round(actual_rate, 4),
            "rawRatio": round(raw_ratio, 4),
            "status": "active",
        }
    return result


def calibrate_ko(entries):
    result = {}
    for tier in TIERS:
        tier_entries = [e for e in entries if e.get("confidence") == tier]
        n = len(tier_entries)
        if n < MIN_SAMPLE:
            result[tier] = {"bias": 0.0, "sampleSize": n, "status": "insufficient data"}
            continue

        avg_raw_bias = sum(e["actualK"] - e["projectedK"] for e in tier_entries) / n
        dampened = DAMPEN * avg_raw_bias
        dampened = max(K_BIAS_BOUNDS[0], min(K_BIAS_BOUNDS[1], dampened))

        result[tier] = {
            "bias": round(dampened, 4),
            "sampleSize": n,
            "avgRawBias": round(avg_raw_bias, 4),
            "status": "active",
        }
    return result


def run():
    hr_entries = load_all_hr_entries()
    ko_entries = load_all_ko_entries()

    calibration = {
        "generatedAt": datetime.utcnow().isoformat(),
        "totalHRGraded": len(hr_entries),
        "totalKGraded": len(ko_entries),
        "minSampleRequired": MIN_SAMPLE,
        "dampenFactor": DAMPEN,
        "hr": calibrate_hr(hr_entries),
        "ko": calibrate_ko(ko_entries),
    }

    os.makedirs("data", exist_ok=True)
    with open("data/calibration.json", "w") as f:
        json.dump(calibration, f, indent=2, default=str)

    print(f"Calibration updated: {len(hr_entries)} HR graded entries, {len(ko_entries)} K graded entries")
    for tier, v in calibration["hr"].items():
        print(f"  HR {tier}: {v}")
    for tier, v in calibration["ko"].items():
        print(f"  K {tier}: {v}")

    return calibration


if __name__ == "__main__":
    run()
