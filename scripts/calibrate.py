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
OUTS_BIAS_BOUNDS = (-3.0, 3.0)
HITS_BIAS_BOUNDS = (-0.75, 0.75)
TB_BIAS_BOUNDS = (-1.25, 1.25)
TIERS = ["High", "Medium", "Low"]

# The stuff_factor bug fix (percentile ranks were being used as if they
# were raw K%/whiff% rates, silently clipping almost every pitcher to the
# same ceiling) changed the actual K/Outs projection math. Any K/Outs
# entry FROZEN before this cutoff was predicted under the OLD, broken
# math -- blending those samples in with new, correctly-computed
# predictions would contaminate the calibration with two different
# regimes pretending to be one consistent signal. HR/Hits/TB don't use
# stuff_factor at all, so they're unaffected and keep using full history.
# Update this to the real date/time this fix actually goes live.
STUFF_FACTOR_FIX_CUTOFF = "2026-09-10T14:24:22"

# Same contamination concern as above, but for a separate bug: HR's
# power_quality factor was using batter Savant percentiles (barrel%,
# hard-hit%, exit velocity) the same broken way stuff_factor was --
# brl_percent/hard_hit_percent clipped almost everyone to the same
# ceiling, and exit_velocity was read as if it were raw mph instead of a
# percentile, producing backwards results for genuinely elite hitters.
# HR entries frozen before this cutoff were computed under that broken
# formula. Hits/Total Bases calibration is NOT filtered by this cutoff --
# their projection math never touches power_quality at all, confirmed
# directly in build_hr.py, so their existing history remains valid.
# Update this to match whenever build_hr.py's fix actually goes live.
POWER_QUALITY_FIX_CUTOFF = "2026-09-10T15:43:50"


def load_all_hr_entries():
    """heuristicProb (and the powerQuality factor feeding it) recomputes
    FRESH on every heavy rebuild -- unlike K/Outs' frozen predictions,
    there's no per-entry freeze timestamp to check. The best available
    precision is the file's own generatedAt: a data/hr/<date>.json file
    only reflects its MOST RECENT rebuild (the daily job overwrites it
    multiple times a day), so if that file was last generated after the
    power_quality fix went live, its entries reflect the fixed formula."""
    entries = []
    excluded_files = 0
    for path in sorted(glob.glob("data/hr/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            generated_at = data.get("generatedAt")
            if not generated_at or generated_at < POWER_QUALITY_FIX_CUTOFF:
                excluded_files += 1
                continue
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    if excluded_files:
        print(f"  [info] HR calibration: excluded {excluded_files} day(s) last generated before the power_quality fix ({POWER_QUALITY_FIX_CUTOFF})")
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
    all_graded = [e for e in entries if e.get("graded") and e.get("actualK") is not None]
    # Only entries FROZEN after the stuff_factor fix reflect the current
    # projection math -- an entry with no frozen timestamp at all predates
    # freeze-tracking entirely, so it's older than the fix too and gets
    # excluded the same way.
    post_fix = [e for e in all_graded if e.get("prizePicksCallFrozenAt") and e["prizePicksCallFrozenAt"] >= STUFF_FACTOR_FIX_CUTOFF]
    excluded = len(all_graded) - len(post_fix)
    if excluded:
        print(f"  [info] K calibration: excluded {excluded} entries frozen before the stuff_factor fix ({STUFF_FACTOR_FIX_CUTOFF})")
    return post_fix


def load_all_outs_entries():
    """Same source files as K (outs fields live on the same ko entries),
    but filtered on actualOuts specifically rather than actualK -- kept
    separate since the two could in principle diverge even though in
    practice they're graded together from the same box score. Same
    post-stuff_factor-fix cutoff as K, using outsCallFrozenAt instead."""
    entries = []
    for path in sorted(glob.glob("data/ko/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    all_graded = [e for e in entries if e.get("graded") and e.get("actualOuts") is not None]
    post_fix = [e for e in all_graded if e.get("outsCallFrozenAt") and e["outsCallFrozenAt"] >= STUFF_FACTOR_FIX_CUTOFF]
    excluded = len(all_graded) - len(post_fix)
    if excluded:
        print(f"  [info] Outs calibration: excluded {excluded} entries frozen before the stuff_factor fix ({STUFF_FACTOR_FIX_CUTOFF})")
    return post_fix


def load_all_hits_entries():
    """Hits fields live on the same hr entries as heuristicProb, so this
    reads the same data/hr/*.json files load_all_hr_entries does --
    filtered specifically on actualHits since a graded HR entry doesn't
    guarantee a Hits prediction was ever made for it (no PrizePicks
    match), same reasoning as the K/Outs split above. Hits/TB don't use
    stuff_factor at all (that's a pitcher-only K/Outs concept), so no
    cutoff filtering needed here -- full history stays valid."""
    entries = []
    for path in sorted(glob.glob("data/hr/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    return [e for e in entries if e.get("graded") and e.get("actualHits") is not None]


def load_all_tb_entries():
    entries = []
    for path in sorted(glob.glob("data/hr/*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            entries.extend(data.get("entries", []))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] couldn't read {path}: {e}")
    return [e for e in entries if e.get("graded") and e.get("actualTotalBases") is not None]


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


def calibrate_outs(entries):
    """Same bias-correction approach as calibrate_ko, applied to
    projectedOuts/actualOuts instead. Wider bounds than K's since an outs
    total naturally runs about double a strikeout total in magnitude."""
    result = {}
    for tier in TIERS:
        tier_entries = [e for e in entries if e.get("confidence") == tier]
        n = len(tier_entries)
        if n < MIN_SAMPLE:
            result[tier] = {"bias": 0.0, "sampleSize": n, "status": "insufficient data"}
            continue

        avg_raw_bias = sum(e["actualOuts"] - e["projectedOuts"] for e in tier_entries) / n
        dampened = DAMPEN * avg_raw_bias
        dampened = max(OUTS_BIAS_BOUNDS[0], min(OUTS_BIAS_BOUNDS[1], dampened))

        result[tier] = {
            "bias": round(dampened, 4),
            "sampleSize": n,
            "avgRawBias": round(avg_raw_bias, 4),
            "status": "active",
        }
    return result


def calibrate_hits(entries):
    """Same bias-correction pattern as calibrate_ko -- projectedHits runs
    in a much smaller range (typically 0-3) than K, so the bias bounds
    are tighter accordingly."""
    result = {}
    for tier in TIERS:
        tier_entries = [e for e in entries if e.get("confidence") == tier]
        n = len(tier_entries)
        if n < MIN_SAMPLE:
            result[tier] = {"bias": 0.0, "sampleSize": n, "status": "insufficient data"}
            continue

        avg_raw_bias = sum(e["actualHits"] - e["projectedHits"] for e in tier_entries) / n
        dampened = DAMPEN * avg_raw_bias
        dampened = max(HITS_BIAS_BOUNDS[0], min(HITS_BIAS_BOUNDS[1], dampened))

        result[tier] = {
            "bias": round(dampened, 4),
            "sampleSize": n,
            "avgRawBias": round(avg_raw_bias, 4),
            "status": "active",
        }
    return result


def calibrate_total_bases(entries):
    """Same pattern again, for projectedTotalBases/actualTotalBases."""
    result = {}
    for tier in TIERS:
        tier_entries = [e for e in entries if e.get("confidence") == tier]
        n = len(tier_entries)
        if n < MIN_SAMPLE:
            result[tier] = {"bias": 0.0, "sampleSize": n, "status": "insufficient data"}
            continue

        avg_raw_bias = sum(e["actualTotalBases"] - e["projectedTotalBases"] for e in tier_entries) / n
        dampened = DAMPEN * avg_raw_bias
        dampened = max(TB_BIAS_BOUNDS[0], min(TB_BIAS_BOUNDS[1], dampened))

        result[tier] = {
            "bias": round(dampened, 4),
            "sampleSize": n,
            "avgRawBias": round(avg_raw_bias, 4),
            "status": "active",
        }
    return result


PROB_SHRINK_BOUNDS = (0.3, 1.0)  # never amplify confidence, only ever shrink it toward 50%


def calibrate_probability_shrink(entries, call_field, hit_field, prob_field):
    """Separate from the mean-projection bias correction above -- this
    corrects the model's PROBABILITY itself, not the projected number.

    The finding this exists to fix: when the model says '80%+ confident',
    real accuracy in that bucket has been running closer to 65%. The
    model's own confidence is real signal (higher-confidence picks DO win
    more) but too extreme relative to what actually happens -- a known
    pattern when Poisson math assumes less real-world randomness than
    actually exists (umpire zone, day-to-day stuff, matchup swings the
    model can't see all add variance the pure math doesn't account for).

    The fix is a single shrink factor per stat: adjusted = 0.5 +
    (raw - 0.5) * shrinkFactor. A factor of 1.0 means "trust the raw
    number completely"; 0.5 means "the real spread is half as extreme as
    claimed". Found via simple linear regression through the origin (no
    intercept, since a genuine coinflip call should show zero average
    excess correctness by symmetry) -- this is standard reliability
    calibration, not a black box: shrinkFactor = sum(x*y) / sum(x*x),
    where x is the model's claimed edge above 50% and y is whether it was
    actually right, centered the same way.

    call_field/hit_field/prob_field let one function serve all four
    stats (K, Outs, Hits, Total Bases) without duplicating this logic
    four times."""
    valid = [e for e in entries if e.get(call_field) and e.get(hit_field) is not None and e.get(prob_field) is not None]
    n = len(valid)
    if n < MIN_SAMPLE:
        return {"shrinkFactor": 1.0, "sampleSize": n, "status": "insufficient data"}

    sum_xy, sum_xx = 0.0, 0.0
    for e in valid:
        call, hit, prob = e[call_field], e[hit_field], e[prob_field]
        call_confidence = prob if call == "OVER" else (1 - prob)
        x = call_confidence - 0.5
        y_centered = (1.0 if hit else 0.0) - 0.5
        sum_xy += x * y_centered
        sum_xx += x * x

    if sum_xx <= 0:
        return {"shrinkFactor": 1.0, "sampleSize": n, "status": "insufficient data"}

    raw_shrink = sum_xy / sum_xx
    dampened = 1 + DAMPEN * (raw_shrink - 1)
    dampened = max(PROB_SHRINK_BOUNDS[0], min(PROB_SHRINK_BOUNDS[1], dampened))

    return {
        "shrinkFactor": round(dampened, 4),
        "sampleSize": n,
        "rawShrinkFactor": round(raw_shrink, 4),
        "status": "active",
    }


def run():
    hr_entries = load_all_hr_entries()
    ko_entries = load_all_ko_entries()
    outs_entries = load_all_outs_entries()
    hits_entries = load_all_hits_entries()
    tb_entries = load_all_tb_entries()

    calibration = {
        "generatedAt": datetime.utcnow().isoformat(),
        "totalHRGraded": len(hr_entries),
        "totalKGraded": len(ko_entries),
        "totalOutsGraded": len(outs_entries),
        "totalHitsGraded": len(hits_entries),
        "totalTotalBasesGraded": len(tb_entries),
        "minSampleRequired": MIN_SAMPLE,
        "dampenFactor": DAMPEN,
        "stuffFactorFixCutoff": STUFF_FACTOR_FIX_CUTOFF,
        "powerQualityFixCutoff": POWER_QUALITY_FIX_CUTOFF,
        "hr": calibrate_hr(hr_entries),
        "ko": calibrate_ko(ko_entries),
        "outs": calibrate_outs(outs_entries),
        "hits": calibrate_hits(hits_entries),
        "totalBases": calibrate_total_bases(tb_entries),
        "probShrink": {
            "ko": calibrate_probability_shrink(ko_entries, "prizePicksCall", "hit", "modelProb"),
            "outs": calibrate_probability_shrink(outs_entries, "outsCall", "outsHit", "outsModelProb"),
            "hits": calibrate_probability_shrink(hits_entries, "hitsCall", "hitsHit", "hitsModelProb"),
            "totalBases": calibrate_probability_shrink(tb_entries, "tbCall", "tbHit", "tbModelProb"),
        },
    }

    os.makedirs("data", exist_ok=True)
    with open("data/calibration.json", "w") as f:
        json.dump(calibration, f, indent=2, default=str)

    print(f"Calibration updated: {len(hr_entries)} HR graded entries, {len(ko_entries)} K graded entries (post-fix only), "
          f"{len(outs_entries)} Outs graded entries (post-fix only), {len(hits_entries)} Hits graded entries, {len(tb_entries)} Total Bases graded entries")
    for tier, v in calibration["hr"].items():
        print(f"  HR {tier}: {v}")
    for tier, v in calibration["ko"].items():
        print(f"  K {tier}: {v}")
    for tier, v in calibration["outs"].items():
        print(f"  Outs {tier}: {v}")
    for tier, v in calibration["hits"].items():
        print(f"  Hits {tier}: {v}")
    for tier, v in calibration["totalBases"].items():
        print(f"  Total Bases {tier}: {v}")
    for stat, v in calibration["probShrink"].items():
        print(f"  Prob shrink {stat}: {v}")

    return calibration


if __name__ == "__main__":
    run()
