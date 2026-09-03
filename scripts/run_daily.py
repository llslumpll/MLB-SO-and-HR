"""
Full daily pipeline, run on a schedule by GitHub Actions:
  1. Build today's HR board
  2. Build today's Strikeouts board
  3. Fetch live Kalshi odds and merge into both
  4. Grade any past days whose games have finished
  5. Update data/index.json (list of dates with saved boards, for History)
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from common import today_iso
import build_hr
import build_ko
import build_teams
import calibrate
import fetch_kalshi
import grade


def preserve_opening_prices(new_entries, old_path, preserve_market_comparison=True):
    """Heavy rebuilds regenerate every entry from scratch, which would
    otherwise silently reset each player's 'opening price' (used for
    price-movement tracking), 'opening line' (PrizePicks' own line
    movement tracking), AND -- this is the important one -- the actual
    prediction itself (projectedK/projectedOuts and the resulting
    OVER/UNDER call). Without preserving the prediction specifically, a
    game graded at the end of the day would be checked against whatever
    the *last* rebuild happened to compute, not what was actually
    predicted that morning -- which defeats the entire purpose of tracking
    predictions and calibrating against them. Carries all of it forward
    from the previous file, matched by (playerId, gamePk) so a
    doubleheader doesn't cross-contaminate two different games for the
    same player.

    preserve_market_comparison controls edge/marketProb specifically.
    K/Outs deliberately freeze the whole projection, so edge/marketProb
    staying frozen alongside it is consistent. HR's heuristicProb is
    NOT frozen -- it's meant to keep updating with fresh matchup data on
    every rebuild -- so preserving a stale edge next to a fresh
    heuristicProb would silently show a market comparison against a
    probability that no longer exists. Pass False for HR."""
    if not os.path.exists(old_path):
        return new_entries
    try:
        with open(old_path) as f:
            old_data = json.load(f)
    except Exception:  # noqa: BLE001
        return new_entries

    def index_by(field):
        return {
            (e.get("playerId"), e.get("gamePk")): e[field]
            for e in old_data.get("entries", [])
            if e.get(field) is not None
        }

    opening_prob_by_key = index_by("openingProb")
    opening_line_by_key = index_by("prizePicksOpeningLine")
    opening_outs_line_by_key = index_by("prizePicksOutsOpeningLine")
    # The frozen prediction package: the projection AS IT WAS the first
    # time it was computed today, plus everything derived from it.
    projected_k_by_key = index_by("projectedK")
    projected_outs_by_key = index_by("projectedOuts")
    prize_picks_call_by_key = index_by("prizePicksCall")
    model_prob_by_key = index_by("modelProb")
    market_threshold_by_key = index_by("marketThreshold")
    outs_call_by_key = index_by("outsCall")
    outs_model_prob_by_key = index_by("outsModelProb")
    outs_market_threshold_by_key = index_by("outsMarketThreshold")
    # The DISPLAY fields that go alongside the frozen prediction above --
    # without these, a heavy rebuild would correctly restore the frozen
    # OVER/UNDER call and edge, but the line/market% shown right next to
    # them would blank out until the next successful PrizePicks match,
    # making the display briefly self-contradictory (a real prediction and
    # edge shown with no line to explain where they came from).
    k_line_by_key = index_by("prizePicksKLine")
    outs_line_by_key = index_by("prizePicksOutsLine")
    market_prob_by_key = index_by("marketProb") if preserve_market_comparison else {}
    edge_by_key = index_by("edge") if preserve_market_comparison else {}
    prediction_line_by_key = index_by("predictionLine")
    prediction_outs_line_by_key = index_by("predictionOutsLine")

    preserved = 0
    for e in new_entries:
        key = (e.get("playerId"), e.get("gamePk"))
        touched = False
        if key in opening_prob_by_key:
            e["openingProb"] = opening_prob_by_key[key]
            touched = True
        if key in opening_line_by_key:
            e["prizePicksOpeningLine"] = opening_line_by_key[key]
            touched = True
        if key in opening_outs_line_by_key:
            e["prizePicksOutsOpeningLine"] = opening_outs_line_by_key[key]
            touched = True
        if key in projected_k_by_key:
            e["projectedK"] = projected_k_by_key[key]
            touched = True
        if key in projected_outs_by_key:
            e["projectedOuts"] = projected_outs_by_key[key]
            touched = True
        if key in prize_picks_call_by_key:
            e["prizePicksCall"] = prize_picks_call_by_key[key]
            touched = True
        if key in model_prob_by_key:
            e["modelProb"] = model_prob_by_key[key]
            touched = True
        if key in market_threshold_by_key:
            e["marketThreshold"] = market_threshold_by_key[key]
            touched = True
        if key in outs_call_by_key:
            e["outsCall"] = outs_call_by_key[key]
            touched = True
        if key in outs_model_prob_by_key:
            e["outsModelProb"] = outs_model_prob_by_key[key]
            touched = True
        if key in outs_market_threshold_by_key:
            e["outsMarketThreshold"] = outs_market_threshold_by_key[key]
            touched = True
        if key in k_line_by_key:
            e["prizePicksKLine"] = k_line_by_key[key]
            touched = True
        if key in outs_line_by_key:
            e["prizePicksOutsLine"] = outs_line_by_key[key]
            touched = True
        if key in market_prob_by_key:
            e["marketProb"] = market_prob_by_key[key]
            touched = True
        if key in edge_by_key:
            e["edge"] = edge_by_key[key]
            touched = True
        if key in prediction_line_by_key:
            e["predictionLine"] = prediction_line_by_key[key]
            touched = True
        if key in prediction_outs_line_by_key:
            e["predictionOutsLine"] = prediction_outs_line_by_key[key]
            touched = True
        if touched:
            preserved += 1
    if preserved:
        print(f"  Preserved opening prices/lines and frozen predictions for {preserved} entries from the previous build")
    return new_entries


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    year = int(date[:4])

    os.makedirs("data/hr", exist_ok=True)
    os.makedirs("data/ko", exist_ok=True)
    os.makedirs("data/teams", exist_ok=True)

    print("=" * 60)
    print(f"STEP 1: Build HR board for {date}")
    print("=" * 60)
    hr_result = build_hr.build(date, year)
    hr_result["entries"] = preserve_opening_prices(hr_result["entries"], f"data/hr/{date}.json", preserve_market_comparison=False)
    with open(f"data/hr/{date}.json", "w") as f:
        json.dump(hr_result, f, indent=2, default=str)
    print(f"Wrote data/hr/{date}.json with {len(hr_result['entries'])} entries")

    print("=" * 60)
    print(f"STEP 2: Build Strikeouts board for {date}")
    print("=" * 60)
    ko_result = build_ko.build(date, year)
    ko_result["entries"] = preserve_opening_prices(ko_result["entries"], f"data/ko/{date}.json")
    with open(f"data/ko/{date}.json", "w") as f:
        json.dump(ko_result, f, indent=2, default=str)
    print(f"Wrote data/ko/{date}.json with {len(ko_result['entries'])} entries")

    print("=" * 60)
    print(f"STEP 2b: Build Teams/Matchups board for {date}")
    print("=" * 60)
    try:
        games_today = build_hr.fetch_schedule(date)
        teams_result = build_teams.build(date, year, games_today)
        with open(f"data/teams/{date}.json", "w") as f:
            json.dump(teams_result, f, indent=2, default=str)
        print(f"Wrote data/teams/{date}.json with {len(teams_result['teams'])} teams, {len(teams_result['matchups'])} matchups")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Teams/Matchups build failed, continuing without it: {e}")

    print("=" * 60)
    print("STEP 3: Fetch and merge Kalshi odds")
    print("=" * 60)
    try:
        hr_records = fetch_kalshi.pull_series("KXMLBHR", "home runs")
        ko_records = fetch_kalshi.pull_series("KXMLBKS", "strikeouts")

        with open(f"data/hr/{date}.json") as f:
            hr_data = json.load(f)
        hr_data = fetch_kalshi.merge_hr(hr_data, hr_records)
        hr_data["entries"].sort(key=lambda e: -e["heuristicProb"])
        with open(f"data/hr/{date}.json", "w") as f:
            json.dump(hr_data, f, indent=2, default=str)

        with open(f"data/ko/{date}.json") as f:
            ko_data = json.load(f)
        ko_data = fetch_kalshi.merge_ko(ko_data, ko_records)
        ko_data["entries"].sort(key=lambda e: -e["projectedK"])
        with open(f"data/ko/{date}.json", "w") as f:
            json.dump(ko_data, f, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Kalshi step failed, continuing without odds: {e}")

    print("=" * 60)
    print("STEP 4: Grade past days")
    print("=" * 60)
    total_graded = 0
    for path in sorted(glob.glob("data/hr/*.json")):
        total_graded += grade.grade_hr_file(path)
    for path in sorted(glob.glob("data/ko/*.json")):
        total_graded += grade.grade_ko_file(path)
    print(f"Graded {total_graded} entries total")

    print("=" * 60)
    print("STEP 4b: Recalibrate from graded history")
    print("=" * 60)
    try:
        calibrate.run()
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Calibration step failed, next build will use last known calibration: {e}")

    print("=" * 60)
    print("STEP 5: Update date index")
    print("=" * 60)
    hr_dates = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob("data/hr/*.json"))
    ko_dates = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob("data/ko/*.json"))
    teams_dates = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob("data/teams/*.json"))
    with open("data/index.json", "w") as f:
        json.dump({"hrDates": hr_dates, "koDates": ko_dates, "teamsDates": teams_dates, "updatedAt": today_iso()}, f, indent=2)
    print(f"data/index.json: {len(hr_dates)} HR days, {len(ko_dates)} K days, {len(teams_dates)} team-board days")

    print("Done.")


if __name__ == "__main__":
    main()
