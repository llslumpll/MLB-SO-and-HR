"""
Build today's Home Run board: fetches today's games/lineups, computes the
HR heuristic for every batter, and writes data/hr/<date>.json
"""

import json
import os
import sys
from datetime import datetime, timedelta

from common import (
    API, PARKS, LEAGUE_AVG_HR_RATE, SAVANT,
    clip, to_num, get, get_text, parse_ip,
    fetch_savant_percentiles, fetch_weather, today_iso,
)

import csv
import io


def fetch_schedule(date):
    data = get(f"{API}/schedule", params={
        "sportId": 1, "date": date, "hydrate": "team,probablePitcher,venue",
    })
    dates = data.get("dates") or []
    return dates[0]["games"] if dates else []


def fetch_boxscore(game_pk):
    return get(f"{API}/game/{game_pk}/boxscore")


def extract_batters(team_box, team_abbr, opp_abbr, game_pk, game_date, opp_pitcher):
    out = []
    for p in (team_box.get("players") or {}).values():
        batting_order = p.get("battingOrder")
        if batting_order:
            out.append({
                "id": p["person"]["id"], "name": p["person"]["fullName"],
                "pos": (p.get("position") or {}).get("abbreviation", ""),
                "order": int(batting_order) // 100,
                "team": team_abbr, "opp": opp_abbr, "gamePk": game_pk,
                "gameDate": game_date, "oppPitcher": opp_pitcher,
            })
    out.sort(key=lambda b: b["order"])
    return out


def extract_roster_fallback(team_box, team_abbr, opp_abbr, game_pk, game_date, opp_pitcher):
    out = []
    for p in (team_box.get("players") or {}).values():
        pos = (p.get("position") or {}).get("abbreviation", "")
        if pos and pos != "P":
            out.append({
                "id": p["person"]["id"], "name": p["person"]["fullName"], "pos": pos,
                "order": None, "team": team_abbr, "opp": opp_abbr, "gamePk": game_pk,
                "gameDate": game_date, "oppPitcher": opp_pitcher,
            })
    return out[:9]


def aggregate_games(games):
    pa = sum(to_num(g.get("plateAppearances")) or 0 for g in games)
    hr = sum(to_num(g.get("homeRuns")) or 0 for g in games)
    ab = sum(to_num(g.get("atBats")) or 0 for g in games)
    h = sum(to_num(g.get("hits")) or 0 for g in games)
    bb = sum(to_num(g.get("baseOnBalls")) or 0 for g in games)
    hbp = sum(to_num(g.get("hitByPitch")) or 0 for g in games)
    doubles = sum(to_num(g.get("doubles")) or 0 for g in games)
    triples = sum(to_num(g.get("triples")) or 0 for g in games)
    singles = h - doubles - triples - hr
    tb = singles + doubles * 2 + triples * 3 + hr * 4
    avg = h / ab if ab else None
    obp = (h + bb + hbp) / (ab + bb + hbp) if (ab + bb + hbp) else None
    slg = tb / ab if ab else None
    ops = (obp + slg) if (obp is not None and slg is not None) else None
    return {"games": len(games), "PA": pa, "HR": hr, "AVG": avg, "OBP": obp, "SLG": slg, "OPS": ops}


def fetch_batter_stats(batter_id, year):
    try:
        data = get(f"{API}/people/{batter_id}/stats", params={
            "stats": "season,gameLog", "group": "hitting", "season": year,
        })
        stats = data.get("stats") or []
        season_group = next((s for s in stats if s["type"]["displayName"] == "season"), None)
        log_group = next((s for s in stats if s["type"]["displayName"] == "gameLog"), None)
        season = None
        if season_group and season_group.get("splits"):
            season = season_group["splits"][0]["stat"]
        raw_splits = (log_group or {}).get("splits") or []
        log = [{**s["stat"], "date": s.get("date")} for s in raw_splits]

        last10 = log[-10:]
        l10_pa = sum(to_num(g.get("plateAppearances")) or 0 for g in last10)
        l10_hr = sum(to_num(g.get("homeRuns")) or 0 for g in last10)

        now = datetime.utcnow()

        def within_days(date_str, days):
            try:
                d = datetime.fromisoformat(date_str)
                return (now - d).days <= days
            except Exception:  # noqa: BLE001
                return False

        week = aggregate_games([g for g in log if within_days(g.get("date", ""), 7)])
        month = aggregate_games([g for g in log if within_days(g.get("date", ""), 30)])

        streak_type, streak_games = None, 0
        for g in reversed(log):
            pa = to_num(g.get("plateAppearances")) or 0
            if pa == 0:
                continue
            hit_hr = (to_num(g.get("homeRuns")) or 0) > 0
            if streak_type is None:
                streak_type = "hot" if hit_hr else "cold"
                streak_games = 1
            elif (streak_type == "hot" and hit_hr) or (streak_type == "cold" and not hit_hr):
                streak_games += 1
            else:
                break

        return {
            "season": season, "l10PA": l10_pa, "l10HR": l10_hr,
            "week": week, "month": month,
            "streakType": streak_type, "streakGames": streak_games,
        }
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] fetch_batter_stats failed for {batter_id}: {e}")
        return None


def fetch_pitcher_hr9(pitcher_id, year):
    try:
        data = get(f"{API}/people/{pitcher_id}/stats", params={
            "stats": "season", "group": "pitching", "season": year,
        })
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            return None
        stat = splits[0]["stat"]
        ip = parse_ip(stat.get("inningsPitched"))
        hr = to_num(stat.get("homeRuns"))
        if not ip or hr is None:
            return None
        return {"hr9": (hr / ip) * 9}
    except Exception:  # noqa: BLE001
        return None


def fetch_pitcher_hand(pitcher_id):
    try:
        data = get(f"{API}/people/{pitcher_id}")
        people = data.get("people") or []
        if people and people[0].get("pitchHand"):
            return people[0]["pitchHand"]["code"]
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_platoon_hr(batter_id, year):
    try:
        data = get(f"{API}/people/{batter_id}/stats", params={
            "stats": "statSplits", "group": "hitting", "gameType": "R",
            "sitCodes": "vl,vr", "season": year,
        })
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        out = {}
        for s in splits:
            code = s.get("split", {}).get("code")
            if code in ("vl", "vr"):
                out[code] = s["stat"]
        return out
    except Exception:  # noqa: BLE001
        return {}


def fetch_vs_pitcher(batter_id, pitcher_id):
    try:
        data = get(f"{API}/people/{batter_id}/stats", params={
            "stats": "vsPlayer", "group": "hitting",
            "opposingPlayerId": pitcher_id, "sportId": 1,
        })
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            return None
        stat = splits[0]["stat"]
        ab = to_num(stat.get("atBats"))
        if not ab or ab < 3:
            return None
        return {
            "atBats": int(ab), "hits": int(to_num(stat.get("hits")) or 0),
            "homeRuns": int(to_num(stat.get("homeRuns")) or 0),
            "avg": stat.get("avg"), "obp": stat.get("obp"),
            "slg": stat.get("slg"), "ops": stat.get("ops"),
        }
    except Exception:  # noqa: BLE001
        return None


_velo_cache = {}


def fetch_pitcher_velo_trend(pitcher_id, year, savant_pitcher_map):
    if pitcher_id in _velo_cache:
        return _velo_cache[pitcher_id]
    try:
        end = today_iso()
        start = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        text = get_text(f"{SAVANT}/statcast_search/csv", params={
            "all": "true", "hfGT": "R", "player_type": "pitcher",
            "game_date_gt": start, "game_date_lt": end,
            "pitchers_lookup[]": pitcher_id, "type": "details",
        })
        reader = csv.DictReader(io.StringIO(text))
        speeds = []
        for row in reader:
            if (row.get("pitch_type") or "").upper() == "FF":
                spd = to_num(row.get("release_speed"))
                if spd is not None:
                    speeds.append(spd)
        if len(speeds) < 8:
            _velo_cache[pitcher_id] = None
            return None
        recent_avg = sum(speeds) / len(speeds)
        row = savant_pitcher_map.get(str(pitcher_id))
        season_fb = to_num(row.get("fb_velocity")) if row else None
        if season_fb is None:
            _velo_cache[pitcher_id] = None
            return None
        result = {"recentAvg": recent_avg, "seasonFB": season_fb, "delta": recent_avg - season_fb}
        _velo_cache[pitcher_id] = result
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] velo trend failed for {pitcher_id}: {e}")
        _velo_cache[pitcher_id] = None
        return None


def compute_heuristic(b, savant_batter_map):
    factors = {}
    s = (b.get("batterStats") or {}).get("season")
    season_pa = to_num(s.get("plateAppearances")) if s else None
    season_hr = to_num(s.get("homeRuns")) if s else None

    PRIOR_PA = 200
    if season_pa is not None:
        season_rate = ((season_hr or 0) + PRIOR_PA * LEAGUE_AVG_HR_RATE) / (season_pa + PRIOR_PA)
    else:
        season_rate = LEAGUE_AVG_HR_RATE

    order = b.get("order")
    expected_pa = 4.5 if (order and order <= 2) else 4.2 if (order and order <= 5) else 3.9 if (order and order <= 7) else 3.6 if order else 3.8
    base = 1 - (1 - clip(season_rate, 0, 0.15)) ** expected_pa
    factors["base"] = base

    form = 1.0
    bs = b.get("batterStats") or {}
    if bs.get("l10PA", 0) >= 15:
        L10_PRIOR_PA = 20
        l10_rate = ((bs.get("l10HR") or 0) + L10_PRIOR_PA * season_rate) / (bs["l10PA"] + L10_PRIOR_PA)
        form = clip((l10_rate / season_rate) if season_rate > 0 else 1, 0.5, 1.8)
    factors["form"] = form

    pitcher_vuln = 1.0
    if b.get("pitcherHR9") and b["pitcherHR9"].get("hr9") is not None:
        pitcher_vuln = clip(b["pitcherHR9"]["hr9"] / 1.2, 0.7, 1.5)
    factors["pitcherVuln"] = pitcher_vuln

    power_quality = 1.0
    sv = savant_batter_map.get(str(b["id"]))
    b["savant"] = sv
    if sv:
        brl = to_num(sv.get("brl_percent"))
        hard_hit = to_num(sv.get("hard_hit_percent"))
        ev = to_num(sv.get("exit_velocity"))
        parts = []
        if brl is not None:
            parts.append(clip(brl / 8.0, 0.6, 1.8))
        if hard_hit is not None:
            parts.append(clip(hard_hit / 38.0, 0.7, 1.5))
        if ev is not None:
            parts.append(clip(1 + (ev - 88.5) * 0.04, 0.85, 1.25))
        if parts:
            prod = 1
            for x in parts:
                prod *= x
            power_quality = clip(prod ** (1 / len(parts)), 0.65, 1.7)
    factors["powerQuality"] = power_quality

    park = clip((b.get("park") or {}).get("hr", 1.0), 0.8, 1.5)
    factors["park"] = park

    weather = 1.0
    w = b.get("weather")
    if w and not w.get("climateControlled") and w.get("tempF") is not None:
        t = w["tempF"]
        temp_factor = 1.08 if t > 85 else 1.04 if t > 75 else 1.00 if t > 60 else 0.96 if t > 45 else 0.90
        wind_factor = clip(1 + (w.get("windMph", 6) - 6) * 0.006, 0.94, 1.10)
        humidity_factor = clip(1 + (w.get("humidity", 50) - 50) * 0.0008, 0.97, 1.04)
        weather = clip(temp_factor * wind_factor * humidity_factor, 0.85, 1.15)
    factors["weather"] = weather

    hand = 1.0
    throws_hand = b.get("oppThrows")
    platoon = b.get("platoon") or {}
    if throws_hand and platoon:
        split = platoon.get("vl") if throws_hand == "L" else platoon.get("vr")
        if split and (to_num(split.get("atBats")) or 0) >= 15 and season_pa:
            split_rate = (to_num(split.get("homeRuns")) or 0) / (to_num(split.get("atBats")) or 1)
            season_ab_rate = (season_hr or 0) / (to_num(s.get("atBats")) or 1)
            if season_ab_rate > 0:
                hand = clip(split_rate / season_ab_rate, 0.7, 1.4)
    factors["handedness"] = hand

    prob = base * form * pitcher_vuln * power_quality * park * weather * hand
    prob = clip(prob, 0.01, 0.45)

    conf_score = 0
    if season_pa is not None and season_pa >= 150:
        conf_score += 2
    elif season_pa is not None and season_pa >= 50:
        conf_score += 1
    if order:
        conf_score += 1
    if sv:
        conf_score += 1
    if w or ((b.get("park") or {}).get("roof") != "open"):
        conf_score += 1
    confidence = "High" if conf_score >= 4 else "Medium" if conf_score >= 2 else "Low"

    return {"heuristicProb": prob, "factors": factors, "seasonRate": season_rate, "confidence": confidence}


def join_list(arr):
    if not arr:
        return ""
    if len(arr) == 1:
        return arr[0]
    if len(arr) == 2:
        return f"{arr[0]} and {arr[1]}"
    return ", ".join(arr[:-1]) + f", and {arr[-1]}"


def reason_text(b):
    f = b.get("factors") or {}
    positives, negatives = [], []
    if f.get("form", 1) > 1.15:
        positives.append("hot recent form")
    elif f.get("form", 1) < 0.85:
        negatives.append("cold recent form")
    if f.get("pitcherVuln", 1) > 1.15:
        positives.append("a homer-prone opposing pitcher")
    elif f.get("pitcherVuln", 1) < 0.85:
        negatives.append("a stingy opposing pitcher")
    if f.get("powerQuality", 1) > 1.15:
        positives.append("elite quality of contact (barrel/hard-hit/exit velo)")
    elif f.get("powerQuality", 1) < 0.85:
        negatives.append("below-average quality of contact")
    if f.get("park", 1) > 1.10:
        positives.append("a hitter-friendly park")
    elif f.get("park", 1) < 0.92:
        negatives.append("a pitcher-friendly park")
    if f.get("weather", 1) > 1.06:
        positives.append("warm/humid, carrying conditions")
    elif f.get("weather", 1) < 0.94:
        negatives.append("cool, dead-air conditions")
    if f.get("handedness", 1) > 1.15:
        positives.append("a favorable platoon matchup")
    elif f.get("handedness", 1) < 0.85:
        negatives.append("a tough platoon matchup")
    vp = b.get("vsPitcher")
    if vp and vp.get("homeRuns", 0) > 0:
        positives.append(f"a track record vs this pitcher ({vp['homeRuns']} HR in {vp['atBats']} AB)")
    elif vp and vp.get("atBats", 0) >= 8 and (to_num(vp.get("avg")) or 1) < 0.180:
        negatives.append("a history of struggling vs this pitcher")
    vt = b.get("veloTrend")
    if vt and vt.get("delta", 0) < -1.2:
        positives.append("the opposing pitcher's fastball velocity trending down")
    bs = b.get("batterStats") or {}
    if bs.get("streakType") == "hot" and bs.get("streakGames", 0) >= 2:
        positives.append(f"a {bs['streakGames']}-game HR streak")
    elif bs.get("streakType") == "cold" and bs.get("streakGames", 0) >= 15:
        negatives.append(f"a {bs['streakGames']}-game HR drought")
    if b.get("confirmedLineup") is False:
        negatives.append("the lineup isn't confirmed yet")

    r = b.get("seasonRate") or 0
    base = ("Elite home run power this season" if r > 0.045 else
            "Strong power profile this season" if r > 0.032 else
            "An average power profile this season" if r > 0.022 else
            "A modest power profile this season")
    sentence = base
    if positives:
        sentence += f", boosted by {join_list(positives)}"
    if negatives:
        sentence += ("; " if positives else ", ") + f"tempered by {join_list(negatives)}"
    return sentence + "."


def build(date, year):
    print(f"Building HR board for {date}...")
    games = fetch_schedule(date)
    if not games:
        print("No games today.")
        return {"date": date, "generatedAt": datetime.utcnow().isoformat(), "entries": []}

    savant_batter_map = fetch_savant_percentiles("batter", year)
    entries = []

    for g in games:
        try:
            box = fetch_boxscore(g["gamePk"])
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] boxscore failed for game {g['gamePk']}: {e}")
            continue

        away_pitcher = g["teams"]["away"].get("probablePitcher")
        home_pitcher = g["teams"]["home"].get("probablePitcher")
        away_abbr = g["teams"]["away"]["team"]["abbreviation"]
        home_abbr = g["teams"]["home"]["team"]["abbreviation"]

        away_batters = extract_batters(box["teams"]["away"], away_abbr, home_abbr, g["gamePk"], g["gameDate"], home_pitcher)
        home_batters = extract_batters(box["teams"]["home"], home_abbr, away_abbr, g["gamePk"], g["gameDate"], away_pitcher)
        lineup_posted = bool(away_batters) and bool(home_batters)
        if not lineup_posted:
            away_batters = extract_roster_fallback(box["teams"]["away"], away_abbr, home_abbr, g["gamePk"], g["gameDate"], home_pitcher)
            home_batters = extract_roster_fallback(box["teams"]["home"], home_abbr, away_abbr, g["gamePk"], g["gameDate"], away_pitcher)

        batters = away_batters + home_batters

        pitcher_ids = [p["id"] for p in (away_pitcher, home_pitcher) if p]
        pitcher_hr9 = {pid: fetch_pitcher_hr9(pid, year) for pid in pitcher_ids}
        pitcher_hand = {pid: fetch_pitcher_hand(pid) for pid in pitcher_ids}
        savant_pitcher_map = fetch_savant_percentiles("pitcher", year)
        velo_trend = {pid: fetch_pitcher_velo_trend(pid, year, savant_pitcher_map) for pid in pitcher_ids}

        home_key = home_abbr
        park = PARKS.get(home_key)
        weather = None
        if park and park["roof"] == "open":
            weather = fetch_weather(park["lat"], park["lon"], g["gameDate"])

        for b in batters:
            b["batterStats"] = fetch_batter_stats(b["id"], year)
            opp_pitcher = b.get("oppPitcher")
            b["pitcherHR9"] = pitcher_hr9.get(opp_pitcher["id"]) if opp_pitcher else None
            b["oppThrows"] = pitcher_hand.get(opp_pitcher["id"]) if opp_pitcher else None
            b["veloTrend"] = velo_trend.get(opp_pitcher["id"]) if opp_pitcher else None
            b["confirmedLineup"] = lineup_posted
            b["park"] = park
            b["weather"] = weather if (park and park["roof"] == "open") else {"climateControlled": True}
            b["platoon"] = fetch_platoon_hr(b["id"], year)
            b["vsPitcher"] = fetch_vs_pitcher(b["id"], opp_pitcher["id"]) if opp_pitcher else None

            h = compute_heuristic(b, savant_batter_map)
            b.update(h)
            b["reason"] = reason_text(b)

            entries.append({
                "gamePk": b["gamePk"], "playerId": b["id"], "name": b["name"],
                "team": b["team"], "opp": b["opp"], "order": b["order"],
                "heuristicProb": b["heuristicProb"], "confidence": b["confidence"],
                "reason": b["reason"], "factors": b["factors"],
                "savant": b.get("savant"), "vsPitcher": b.get("vsPitcher"),
                "veloTrend": b.get("veloTrend"), "streak": {
                    "type": (b.get("batterStats") or {}).get("streakType"),
                    "games": (b.get("batterStats") or {}).get("streakGames"),
                },
                "trendWeekOPS": (b.get("batterStats") or {}).get("week", {}).get("OPS"),
                "trendMonthOPS": (b.get("batterStats") or {}).get("month", {}).get("OPS"),
                "seasonOPS": to_num((b.get("batterStats") or {}).get("season", {}).get("ops")) if b.get("batterStats") and b["batterStats"].get("season") else None,
                "marketProb": None, "edge": None,
                "graded": False, "hr": None,
            })
        print(f"  {away_abbr} @ {home_abbr}: {len(batters)} batters" + ("" if lineup_posted else " (roster fallback, lineup not posted)"))

    seen = set()
    deduped = []
    for e in entries:
        key = (e["playerId"], e["gamePk"])
        if key in seen:
            print(f"  [dedup] dropped duplicate entry for {e['name']} in the same game")
            continue
        seen.add(key)
        deduped.append(e)
    entries = deduped

    entries.sort(key=lambda e: -e["heuristicProb"])
    return {"date": date, "generatedAt": datetime.utcnow().isoformat(), "entries": entries}


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    year = int(date[:4])
    result = build(date, year)
    os.makedirs("data/hr", exist_ok=True)
    with open(f"data/hr/{date}.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"Wrote data/hr/{date}.json with {len(result['entries'])} entries")
