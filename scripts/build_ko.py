"""
Build today's Strikeouts board: projects K totals for every probable
starter and writes data/ko/<date>.json
"""

import json
import math
import os
import sys
from datetime import datetime

from common import (
    API, LEAGUE_AVG_K9, LEAGUE_AVG_K_PCT,
    clip, to_num, get, parse_ip,
    fetch_savant_percentiles, today_iso,
)
from build_hr import fetch_schedule, fetch_pitcher_hand, fetch_pitcher_velo_trend


def load_ko_calibration():
    """Reads data/calibration.json's 'ko' section if it exists. Returns {}
    if missing/unreadable -- calibration is purely additive and should
    never break a build."""
    try:
        with open("data/calibration.json") as f:
            return (json.load(f) or {}).get("ko", {})
    except Exception:  # noqa: BLE001
        return {}


def fetch_roster_k_percent(team_id, batter_pct_map):
    try:
        data = get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
        roster = data.get("roster") or []
        batters = [r for r in roster if (r.get("position") or {}).get("abbreviation") != "P"]
        vals = []
        for b in batters:
            row = batter_pct_map.get(str(b["person"]["id"]))
            if row:
                k = to_num(row.get("k_percent"))
                if k is not None:
                    vals.append(k)
        return sum(vals) / len(vals) if vals else None
    except Exception:  # noqa: BLE001
        return None


def fetch_pitcher_projection(pitcher_id, opp_team_id, batter_pct_map, pitcher_pct_map, year, calibration=None):
    try:
        data = get(f"{API}/people/{pitcher_id}/stats", params={
            "stats": "season,gameLog", "group": "pitching", "season": year,
        })
    except Exception:  # noqa: BLE001
        return None

    stats = data.get("stats") or []
    season_group = next((s for s in stats if s["type"]["displayName"] == "season"), None)
    log_group = next((s for s in stats if s["type"]["displayName"] == "gameLog"), None)
    season_stat = None
    if season_group and season_group.get("splits"):
        season_stat = season_group["splits"][0]["stat"]
    if not season_stat:
        return None
    log = [s["stat"] for s in ((log_group or {}).get("splits") or [])]

    season_ip = parse_ip(season_stat.get("inningsPitched"))
    season_k = to_num(season_stat.get("strikeOuts"))

    PRIOR_IP = 30
    if season_ip is not None:
        season_k9 = (((season_k or 0) + PRIOR_IP * (LEAGUE_AVG_K9 / 9)) / (season_ip + PRIOR_IP)) * 9
    else:
        season_k9 = LEAGUE_AVG_K9

    games_started = to_num(season_stat.get("gamesStarted")) or to_num(season_stat.get("gamesPlayed")) or 1

    starts = [g for g in log if (parse_ip(g.get("inningsPitched")) or 0) > 0][-3:]
    recent_k9 = None
    recent_starts_log = []
    if starts:
        ip_sum = sum(parse_ip(g.get("inningsPitched")) or 0 for g in starts)
        k_sum = sum(to_num(g.get("strikeOuts")) or 0 for g in starts)
        RECENT_PRIOR_IP = 6
        if ip_sum > 0:
            recent_k9 = (((k_sum or 0) + RECENT_PRIOR_IP * (season_k9 / 9)) / (ip_sum + RECENT_PRIOR_IP)) * 9
        recent_starts_log = [{"k": int(to_num(g.get("strikeOuts")) or 0), "ip": parse_ip(g.get("inningsPitched"))} for g in starts]

    stuff_factor = 1.0
    p_row = pitcher_pct_map.get(str(pitcher_id))
    if p_row:
        k_pct = to_num(p_row.get("k_percent"))
        whiff_pct = to_num(p_row.get("whiff_percent"))
        parts = []
        if k_pct is not None:
            parts.append(clip(k_pct / 22.0, 0.75, 1.35))
        if whiff_pct is not None:
            parts.append(clip(whiff_pct / 25.0, 0.75, 1.35))
        if parts:
            prod = 1
            for x in parts:
                prod *= x
            stuff_factor = clip(prod ** (1 / len(parts)), 0.75, 1.35)

    base_k9 = (0.6 * season_k9 + 0.4 * recent_k9) if recent_k9 is not None else season_k9

    opp_k = fetch_roster_k_percent(opp_team_id, batter_pct_map)
    matchup_factor = clip(opp_k / LEAGUE_AVG_K_PCT, 0.75, 1.3) if opp_k is not None else 1.0

    # stuff_factor and matchup_factor are each individually bounded, but
    # straight-multiplying them let a pitcher with strong Savant numbers AND
    # a favorable matchup compound to as much as ~1.75x combined -- well
    # beyond what either factor was designed to represent alone. Blending
    # them as a geometric mean (the same technique stuff_factor already
    # uses internally on its own sub-parts) keeps each factor's real signal
    # without letting them stack multiplicatively.
    combined_factor = (stuff_factor * matchup_factor) ** 0.5
    final_k9 = base_k9 * combined_factor

    expected_ip = clip((season_ip / games_started) if (season_ip and games_started) else 5.2, 3.5, 6.7)
    projected_k = final_k9 * expected_ip / 9
    # Absolute safety ceiling on the PROJECTED MEAN specifically -- not a
    # best-case outcome; the Poisson math used downstream already accounts
    # for a great day happening above the mean. A real elite ace at ~11-12
    # K/9 with a strong 6.5 IP start averages around 8 strikeouts, not 12,
    # so this is still generous headroom rather than a tight leash.
    projected_k = clip(projected_k, 1.0, 9.5)

    velo_trend = fetch_pitcher_velo_trend(pitcher_id, year, pitcher_pct_map)

    conf_score = 0
    if season_ip is not None and season_ip >= 40:
        conf_score += 2
    elif season_ip is not None and season_ip >= 15:
        conf_score += 1
    if len(starts) >= 2:
        conf_score += 1
    if p_row:
        conf_score += 1
    if opp_k is not None:
        conf_score += 1
    confidence = "High" if conf_score >= 4 else "Medium" if conf_score >= 2 else "Low"

    calibration_applied = None
    if calibration:
        tier_cal = calibration.get(confidence, {})
        bias = tier_cal.get("bias", 0.0)
        if tier_cal.get("status") == "active" and bias != 0.0:
            projected_k = max(0.5, projected_k + bias)
            calibration_applied = bias

    return {
        "hand": fetch_pitcher_hand(pitcher_id),
        "seasonK9": season_k9, "recentK9": recent_k9, "oppK": opp_k,
        "matchupFactor": matchup_factor, "stuffFactor": stuff_factor,
        "expectedIP": expected_ip, "projectedK": projected_k, "confidence": confidence,
        "calibrationApplied": calibration_applied,
        "recentStartsLog": recent_starts_log, "veloTrend": velo_trend,
        "seasonRecord": f"{int(to_num(season_stat.get('wins')) or 0)}-{int(to_num(season_stat.get('losses')) or 0)}",
        "era": season_stat.get("era"),
    }


def join_list(arr):
    if not arr:
        return ""
    if len(arr) == 1:
        return arr[0]
    if len(arr) == 2:
        return f"{arr[0]} and {arr[1]}"
    return ", ".join(arr[:-1]) + f", and {arr[-1]}"


def reason_text(p):
    positives, negatives = [], []
    if p.get("recentK9") is not None:
        trend = p["recentK9"] - p["seasonK9"]
        if trend > 0.8:
            positives.append("trending up in recent starts")
        elif trend < -0.8:
            negatives.append("trending down in recent starts")
    if p["stuffFactor"] > 1.12:
        positives.append("elite swing-and-miss stuff (K%/whiff%)")
    elif p["stuffFactor"] < 0.9:
        negatives.append("below-average swing-and-miss stuff")
    if p["matchupFactor"] > 1.08:
        positives.append("facing a strikeout-prone lineup")
    elif p["matchupFactor"] < 0.92:
        negatives.append("facing a contact-oriented lineup")
    if p["expectedIP"] > 6.0:
        positives.append("a long-innings workload expectation")
    elif p["expectedIP"] < 4.5:
        negatives.append("a short-outing workload expectation")
    vt = p.get("veloTrend")
    if vt and vt.get("delta", 0) < -1.2:
        negatives.append("declining fastball velocity")
    elif vt and vt.get("delta", 0) > 1.2:
        positives.append("fastball velocity trending up")

    base = ("Elite strikeout stuff this season" if p["seasonK9"] > 10 else
            "Strong strikeout stuff this season" if p["seasonK9"] > 8.5 else
            "Average strikeout stuff this season" if p["seasonK9"] > 7 else
            "Below-average strikeout stuff this season")
    sentence = base
    if positives:
        sentence += f", boosted by {join_list(positives)}"
    if negatives:
        sentence += ("; " if positives else ", ") + f"tempered by {join_list(negatives)}"
    return sentence + "."


def build(date, year):
    print(f"Building Strikeouts board for {date}...")
    games = fetch_schedule(date)
    if not games:
        print("No games today.")
        return {"date": date, "generatedAt": datetime.utcnow().isoformat(), "entries": []}

    calibration = load_ko_calibration()

    batter_pct_map = fetch_savant_percentiles("batter", year)
    pitcher_pct_map = fetch_savant_percentiles("pitcher", year)

    jobs = []
    for g in games:
        away_p = g["teams"]["away"].get("probablePitcher")
        home_p = g["teams"]["home"].get("probablePitcher")
        if away_p:
            jobs.append({"pitcher": away_p, "team": g["teams"]["away"]["team"], "opp": g["teams"]["home"]["team"], "gamePk": g["gamePk"]})
        if home_p:
            jobs.append({"pitcher": home_p, "team": g["teams"]["home"]["team"], "opp": g["teams"]["away"]["team"], "gamePk": g["gamePk"]})

    entries = []
    for job in jobs:
        proj = fetch_pitcher_projection(job["pitcher"]["id"], job["opp"]["id"], batter_pct_map, pitcher_pct_map, year, calibration)
        if not proj:
            continue
        p = {
            "id": job["pitcher"]["id"], "gamePk": job["gamePk"], "name": job["pitcher"]["fullName"],
            "team": job["team"]["abbreviation"], "opp": job["opp"]["abbreviation"], "oppTeamId": job["opp"]["id"],
            **proj,
        }
        p["reason"] = reason_text(p)
        entries.append({
            "gamePk": p["gamePk"], "playerId": p["id"], "name": p["name"],
            "team": p["team"], "opp": p["opp"], "oppTeamId": p["oppTeamId"],
            "hand": p["hand"], "seasonRecord": p["seasonRecord"], "era": p["era"],
            "seasonK9": p["seasonK9"], "recentK9": p["recentK9"], "oppK": p["oppK"],
            "matchupFactor": p["matchupFactor"], "stuffFactor": p["stuffFactor"],
            "expectedIP": p["expectedIP"], "projectedK": p["projectedK"],
            "confidence": p["confidence"], "reason": p["reason"],
            "calibrationApplied": p.get("calibrationApplied"),
            "recentStartsLog": p["recentStartsLog"], "veloTrend": p["veloTrend"],
            "marketThreshold": None, "marketProb": None, "modelProb": None, "edge": None,
            "graded": False, "actualK": None, "hit": None,
        })
        print(f"  {p['name']} ({p['team']} vs {p['opp']}): {p['projectedK']:.1f} proj K")

    seen_ids = set()
    deduped = []
    for e in entries:
        if e["playerId"] in seen_ids:
            print(f"  [dedup] dropped duplicate entry for {e['name']} (likely a doubleheader or schedule quirk)")
            continue
        seen_ids.add(e["playerId"])
        deduped.append(e)
    entries = deduped

    entries.sort(key=lambda e: -e["projectedK"])
    return {"date": date, "generatedAt": datetime.utcnow().isoformat(), "entries": entries}


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    year = int(date[:4])
    result = build(date, year)
    os.makedirs("data/ko", exist_ok=True)
    with open(f"data/ko/{date}.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"Wrote data/ko/{date}.json with {len(result['entries'])} entries")
