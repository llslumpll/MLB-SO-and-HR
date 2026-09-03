"""
Lightweight, frequent-refresh script: re-pulls live Kalshi prices and
PrizePicks prop lines, merging them into TODAY's already-built HR/K boards,
tracking price movement over the course of the day (openingProb / priceDelta
on each entry). Also grades any games that have finished since the last run.

Does NOT rebuild projections, team stats, or matchup scouting reports --
that's the heavier job in run_daily.py, which runs a few times a day.
This script is meant to run frequently (every ~15 min) since everything in
it is cheap: a handful of API calls plus a JSON read/merge/write.

Grading lives here (not just in the heavy job) because MLB games finish at
wildly different times depending on time zone -- a West Coast night game can
easily still be in progress during the heavy pipeline's last run of the day,
leaving it ungraded for many hours otherwise. Running grading every 15
minutes means results show up shortly after a game actually ends, regardless
of when in the day that happens.

PrizePicks is an unofficial, undocumented endpoint (see fetch_prizepicks.py
for the caveats) -- if it ever breaks, that's wrapped so it can't take down
the Kalshi refresh alongside it.
"""

import glob
import json
import os
import sys

from common import today_iso
import fetch_kalshi
import fetch_prizepicks
import grade


def grade_all():
    total = 0
    for path in sorted(glob.glob("data/hr/*.json")):
        try:
            total += grade.grade_hr_file(path)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] grading failed for {path}: {e}")
    for path in sorted(glob.glob("data/ko/*.json")):
        try:
            total += grade.grade_ko_file(path)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] grading failed for {path}: {e}")
    if total:
        print(f"  Graded {total} newly-finished entries")
    return total


def log_pipeline_health(date, kalshi_matched=None, kalshi_total=None, pp_k_matched=None, pp_k_total=None, pp_outs_matched=None, pp_outs_total=None):
    """Appends (or updates, if today already has an entry) a snapshot of
    match rates and calibration sample sizes to data/pipeline-health.json.
    One entry per calendar day, not per run -- refresh_odds.py runs every
    ~15 minutes, and a full history at that granularity would be far more
    than needed for a weekly 'is this still healthy' check; today's entry
    just gets overwritten with the latest numbers each time this runs.

    This is the only place these numbers are persisted anywhere -- until
    this existed, match rates only ever showed up in a GitHub Actions log
    that scrolled away, and calibration sample sizes were only ever a
    single overwritten snapshot with no history to see whether they were
    actually growing."""
    path = "data/pipeline-health.json"
    try:
        with open(path) as f:
            health = json.load(f)
    except Exception:  # noqa: BLE001
        health = {"entries": []}

    calibration_snapshot = None
    try:
        with open("data/calibration.json") as f:
            cal = json.load(f)
        calibration_snapshot = {
            stat: {tier: cal.get(stat, {}).get(tier, {}).get("sampleSize", 0) for tier in ("High", "Medium", "Low")}
            for stat in ("hr", "ko", "outs")
        }
    except Exception:  # noqa: BLE001
        pass

    entry = {
        "date": date,
        "kalshiMatched": kalshi_matched, "kalshiTotal": kalshi_total,
        "ppKMatched": pp_k_matched, "ppKTotal": pp_k_total,
        "ppOutsMatched": pp_outs_matched, "ppOutsTotal": pp_outs_total,
        "calibration": calibration_snapshot,
    }

    existing_idx = next((i for i, e in enumerate(health["entries"]) if e.get("date") == date), None)
    if existing_idx is not None:
        health["entries"][existing_idx] = entry
    else:
        health["entries"].append(entry)
    health["entries"] = sorted(health["entries"], key=lambda e: e["date"])[-90:]  # keep the last ~90 days, no need to grow forever

    os.makedirs("data", exist_ok=True)
    with open(path, "w") as f:
        json.dump(health, f, indent=2, default=str)


def refresh(date):
    hr_path = f"data/hr/{date}.json"
    ko_path = f"data/ko/{date}.json"
    hr_exists = os.path.exists(hr_path)
    ko_exists = os.path.exists(ko_path)

    if not hr_exists and not ko_exists:
        print(f"No board saved yet for {date} -- nothing to refresh.")
        graded = grade_all()
        return graded > 0

    hr_records = fetch_kalshi.pull_series("KXMLBHR", "home runs")
    ko_records = fetch_kalshi.pull_series("KXMLBKS", "strikeouts")

    try:
        pp_hr_records, pp_k_records, pp_outs_records = fetch_prizepicks.fetch_mlb_props()
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] PrizePicks fetch failed entirely, continuing without it: {e}")
        pp_hr_records, pp_k_records, pp_outs_records = [], [], []

    if hr_exists:
        with open(hr_path) as f:
            hr_data = json.load(f)
        hr_data = fetch_kalshi.merge_hr(hr_data, hr_records)
        if pp_hr_records:
            hr_data = fetch_prizepicks.merge_hr(hr_data, pp_hr_records)
        hr_data["entries"].sort(key=lambda e: -e["heuristicProb"])
        with open(hr_path, "w") as f:
            json.dump(hr_data, f, indent=2, default=str)

    kalshi_matched = kalshi_total = pp_k_matched = pp_k_total = pp_outs_matched = pp_outs_total = None

    if ko_exists:
        with open(ko_path) as f:
            ko_data = json.load(f)
        ko_data = fetch_kalshi.merge_ko(ko_data, ko_records)
        kalshi_matched = ko_data.pop("_kalshiMatched", None)
        kalshi_total = ko_data.pop("_kalshiTotal", None)
        if pp_k_records:
            ko_data = fetch_prizepicks.merge_ko(ko_data, pp_k_records)
            pp_k_matched = ko_data.pop("_ppKMatched", None)
            pp_k_total = ko_data.pop("_ppKTotal", None)
            kalshi_thresholds = fetch_kalshi.k_threshold_map(ko_records)
            ko_data = fetch_prizepicks.refine_ko_with_prizepicks(ko_data, kalshi_thresholds)
        if pp_outs_records:
            ko_data = fetch_prizepicks.merge_outs(ko_data, pp_outs_records)
            pp_outs_matched = ko_data.pop("_ppOutsMatched", None)
            pp_outs_total = ko_data.pop("_ppOutsTotal", None)
            ko_data = fetch_prizepicks.refine_outs_with_prizepicks(ko_data)
        ko_data["entries"].sort(key=lambda e: -e["projectedK"])
        with open(ko_path, "w") as f:
            json.dump(ko_data, f, indent=2, default=str)

    grade_all()
    # Health snapshot happens AFTER grading, so today's calibration.json
    # (which grading feeds into via calibrate.py in the heavy pipeline)
    # reflects the most current numbers available at logging time.
    log_pipeline_health(date, kalshi_matched, kalshi_total, pp_k_matched, pp_k_total, pp_outs_matched, pp_outs_total)
    return True


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    changed = refresh(date)
    print("Odds refresh complete." if changed else "Nothing to refresh.")
