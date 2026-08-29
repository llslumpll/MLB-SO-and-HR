"""
Lightweight, frequent-refresh script: re-pulls live Kalshi prices and
PrizePicks prop lines, merging them into TODAY's already-built HR/K boards,
tracking price movement over the course of the day (openingProb / priceDelta
on each entry).

Does NOT rebuild projections, team stats, or matchup scouting reports --
that's the heavier job in run_daily.py, which runs a few times a day.
This script is meant to run frequently (every ~15 min) since it's cheap:
a handful of API calls plus a JSON read/merge/write, nothing else.

PrizePicks is an unofficial, undocumented endpoint (see fetch_prizepicks.py
for the caveats) -- if it ever breaks, that's wrapped so it can't take down
the Kalshi refresh alongside it.
"""

import json
import os
import sys

from common import today_iso
import fetch_kalshi
import fetch_prizepicks


def refresh(date):
    hr_path = f"data/hr/{date}.json"
    ko_path = f"data/ko/{date}.json"
    hr_exists = os.path.exists(hr_path)
    ko_exists = os.path.exists(ko_path)

    if not hr_exists and not ko_exists:
        print(f"No board saved yet for {date} -- nothing to refresh.")
        return False

    hr_records = fetch_kalshi.pull_series("KXMLBHR", "home runs")
    ko_records = fetch_kalshi.pull_series("KXMLBKS", "strikeouts")

    try:
        pp_hr_records, pp_k_records = fetch_prizepicks.fetch_mlb_props()
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] PrizePicks fetch failed entirely, continuing without it: {e}")
        pp_hr_records, pp_k_records = [], []

    if hr_exists:
        with open(hr_path) as f:
            hr_data = json.load(f)
        hr_data = fetch_kalshi.merge_hr(hr_data, hr_records)
        if pp_hr_records:
            hr_data = fetch_prizepicks.merge_hr(hr_data, pp_hr_records)
        hr_data["entries"].sort(key=lambda e: -e["heuristicProb"])
        with open(hr_path, "w") as f:
            json.dump(hr_data, f, indent=2, default=str)

    if ko_exists:
        with open(ko_path) as f:
            ko_data = json.load(f)
        ko_data = fetch_kalshi.merge_ko(ko_data, ko_records)
        if pp_k_records:
            ko_data = fetch_prizepicks.merge_ko(ko_data, pp_k_records)
            kalshi_thresholds = fetch_kalshi.k_threshold_map(ko_records)
            ko_data = fetch_prizepicks.refine_ko_with_prizepicks(ko_data, kalshi_thresholds)
        ko_data["entries"].sort(key=lambda e: -e["projectedK"])
        with open(ko_path, "w") as f:
            json.dump(ko_data, f, indent=2, default=str)

    return True


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    changed = refresh(date)
    print("Odds refresh complete." if changed else "Nothing to refresh.")
