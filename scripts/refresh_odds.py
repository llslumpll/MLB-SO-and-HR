"""
Lightweight, frequent-refresh script: re-pulls live Kalshi prices and merges
them into TODAY's already-built HR/K boards, tracking price movement over
the course of the day (openingProb / priceDelta on each entry).

Does NOT rebuild projections, team stats, or matchup scouting reports --
that's the heavier job in run_daily.py, which runs a few times a day.
This script is meant to run frequently (every ~15 min) since it's cheap:
two Kalshi API calls plus a JSON read/merge/write, nothing else.
"""

import json
import os
import sys

from common import today_iso
import fetch_kalshi


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

    kalshi_k_names = sorted(set(r["name"] for r in ko_records))
    print(f"DEBUG: {len(kalshi_k_names)} unique pitcher name(s) in today's live Kalshi K markets:")
    for n in kalshi_k_names:
        print(f"  {n!r}  (normalized: {fetch_kalshi.norm_name(n)!r})")

    if ko_exists:
        with open(ko_path) as f:
            ko_data_preview = json.load(f)
        board_names = sorted(set(e["name"] for e in ko_data_preview["entries"]))
        print(f"DEBUG: {len(board_names)} pitcher(s) in today's saved K board:")
        for n in board_names:
            print(f"  {n!r}  (normalized: {fetch_kalshi.norm_name(n)!r})")
        overlap = set(fetch_kalshi.norm_name(n) for n in kalshi_k_names) & set(fetch_kalshi.norm_name(n) for n in board_names)
        print(f"DEBUG: {len(overlap)} name(s) overlap after normalization: {sorted(overlap)}")

    if hr_exists:
        with open(hr_path) as f:
            hr_data = json.load(f)
        hr_data = fetch_kalshi.merge_hr(hr_data, hr_records)
        hr_data["entries"].sort(key=lambda e: -e["heuristicProb"])
        with open(hr_path, "w") as f:
            json.dump(hr_data, f, indent=2, default=str)

    if ko_exists:
        with open(ko_path) as f:
            ko_data = json.load(f)
        ko_data = fetch_kalshi.merge_ko(ko_data, ko_records)
        ko_data["entries"].sort(key=lambda e: -e["projectedK"])
        with open(ko_path, "w") as f:
            json.dump(ko_data, f, indent=2, default=str)

    return True


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    changed = refresh(date)
    print("Odds refresh complete." if changed else "Nothing to refresh.")
