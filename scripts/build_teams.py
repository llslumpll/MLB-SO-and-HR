"""
Build the Teams/Matchups board:
  - All 30 teams: record, last-5 record, trend, batting/pitching stats,
    and roster-average Statcast quality metrics.
  - Today's probable starters: full scouting report (season line, Statcast
    percentiles, pitch arsenal, zone attack, and how the opposing lineup's
    batters have actually hit against them historically).
Writes data/teams/<date>.json
"""

import json
import os
import sys
from datetime import datetime

from common import (
    API, clip, to_num, get,
    fetch_savant_percentiles, fetch_pitch_arsenal, fetch_team_last_n_record,
    today_iso,
)
from build_hr import fetch_pitcher_hand, fetch_vs_pitcher


# League affiliation is stable, well-known public information -- deriving it
# this way sidesteps any mismatch in exactly how the API formats the league
# field (e.g. "AL" vs "American League" vs missing entirely).
AL_ABBRS = {"NYY", "BOS", "TB", "TOR", "BAL", "CWS", "CHW", "CLE", "DET", "KC",
            "MIN", "HOU", "LAA", "OAK", "ATH", "SEA", "TEX"}
NL_ABBRS = {"ATL", "MIA", "NYM", "PHI", "WSH", "CHC", "CIN", "MIL", "PIT",
            "STL", "ARI", "COL", "LAD", "SD", "SF"}


def classify_league(abbr, api_league_abbr):
    if abbr in AL_ABBRS:
        return "AL"
    if abbr in NL_ABBRS:
        return "NL"
    # fall back to whatever the API said, in case of an unexpected team code
    return api_league_abbr


def fetch_team_list(year):
    data = get(f"{API}/teams", params={"sportId": 1, "season": year, "activeStatus": "Yes"})
    out = []
    for t in data.get("teams") or []:
        abbr = t.get("abbreviation")
        api_league = (t.get("league") or {}).get("abbreviation")
        out.append({
            "id": t["id"], "name": t.get("teamName") or t.get("name"),
            "abbr": abbr,
            "league": classify_league(abbr, api_league),
            "division": (t.get("division") or {}).get("nameShort") or (t.get("division") or {}).get("name") or "",
        })
    return out


def fetch_standings(year):
    data = get(f"{API}/standings", params={
        "leagueId": "103,104", "season": year, "standingsTypes": "regularSeason",
    })
    out = {}
    for rec in data.get("records") or []:
        for tr in rec.get("teamRecords") or []:
            out[tr["team"]["id"]] = {
                "wins": tr.get("wins"), "losses": tr.get("losses"),
                "pct": to_num(tr.get("winningPercentage")),
                "gb": tr.get("gamesBack"),
                "streak": (tr.get("streak") or {}).get("streakCode"),
                "rs": tr.get("runsScored"), "ra": tr.get("runsAllowed"),
            }
    return out


def fetch_team_season_stats(team_id, year):
    hit, pitch = None, None
    try:
        d = get(f"{API}/teams/{team_id}/stats", params={"stats": "season", "group": "hitting", "season": year})
        splits = (d.get("stats") or [{}])[0].get("splits") or []
        if splits:
            hit = splits[0]["stat"]
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] team hitting stats failed for {team_id}: {e}")
    try:
        d = get(f"{API}/teams/{team_id}/stats", params={"stats": "season", "group": "pitching", "season": year})
        splits = (d.get("stats") or [{}])[0].get("splits") or []
        if splits:
            pitch = splits[0]["stat"]
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] team pitching stats failed for {team_id}: {e}")
    return hit, pitch


def fetch_roster_savant_avg(team_id, batter_pct_map, pitcher_pct_map):
    try:
        data = get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
        roster = data.get("roster") or []
        batters = [r for r in roster if (r.get("position") or {}).get("abbreviation") != "P"]
        pitchers = [r for r in roster if (r.get("position") or {}).get("abbreviation") == "P"]

        def avg_field(rows, pct_map, field):
            vals = []
            for r in rows:
                row = pct_map.get(str(r["person"]["id"]))
                if row:
                    v = to_num(row.get(field))
                    if v is not None:
                        vals.append(v)
            return sum(vals) / len(vals) if vals else None

        return {
            "barrelPct": avg_field(batters, batter_pct_map, "brl_percent"),
            "hardHitPct": avg_field(batters, batter_pct_map, "hard_hit_percent"),
            "exitVelo": avg_field(batters, batter_pct_map, "exit_velocity"),
            "whiffPct": avg_field(pitchers, pitcher_pct_map, "whiff_percent"),
            "chasePct": avg_field(pitchers, pitcher_pct_map, "chase_percent"),
            "pitcherKPct": avg_field(pitchers, pitcher_pct_map, "k_percent"),
        }
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] roster savant avg failed for {team_id}: {e}")
        return {}


def build_teams(year, batter_pct_map, pitcher_pct_map):
    print("Building team records/stats for all 30 teams...")
    team_list = fetch_team_list(year)
    standings = fetch_standings(year)

    teams = []
    for t in team_list:
        standing = standings.get(t["id"], {})
        hit, pitch = fetch_team_season_stats(t["id"], year)
        last5 = fetch_team_last_n_record(t["id"], n=5)
        savant = fetch_roster_savant_avg(t["id"], batter_pct_map, pitcher_pct_map)

        trend = None
        if last5 and last5["games"] and standing.get("pct") is not None:
            last5_pct = last5["wins"] / last5["games"]
            delta = last5_pct - standing["pct"]
            trend = "up" if delta > 0.15 else "down" if delta < -0.15 else "flat"

        teams.append({
            "id": t["id"], "abbr": t["abbr"], "name": t["name"],
            "league": t["league"], "division": t["division"],
            "wins": standing.get("wins"), "losses": standing.get("losses"),
            "pct": standing.get("pct"), "gb": standing.get("gb"), "streak": standing.get("streak"),
            "last5": last5, "trend": trend,
            "hitting": hit, "pitching": pitch, "savant": savant,
        })
        print(f"  {t['abbr']}: {standing.get('wins')}-{standing.get('losses')}, trend={trend}")
    return teams


def build_matchups(games, year, batter_pct_map, pitcher_pct_map):
    print("Building today's pitcher matchup scouting reports...")
    matchups = []
    jobs = []
    for g in games:
        away_p = g["teams"]["away"].get("probablePitcher")
        home_p = g["teams"]["home"].get("probablePitcher")
        if away_p:
            jobs.append({"pitcher": away_p, "team": g["teams"]["away"]["team"], "opp": g["teams"]["home"]["team"]})
        if home_p:
            jobs.append({"pitcher": home_p, "team": g["teams"]["home"]["team"], "opp": g["teams"]["away"]["team"]})

    for job in jobs:
        pid = job["pitcher"]["id"]
        try:
            season_data = get(f"{API}/people/{pid}/stats", params={"stats": "season", "group": "pitching", "season": year})
            splits = (season_data.get("stats") or [{}])[0].get("splits") or []
            season_stat = splits[0]["stat"] if splits else None
        except Exception:  # noqa: BLE001
            season_stat = None

        arsenal = fetch_pitch_arsenal(pid, year)

        # opposing lineup's actual history vs this pitcher
        matchup_table = []
        try:
            roster_data = get(f"{API}/teams/{job['opp']['id']}/roster", params={"rosterType": "active"})
            roster = [r for r in (roster_data.get("roster") or []) if (r.get("position") or {}).get("abbreviation") != "P"]
            for b in roster:
                stat = fetch_vs_pitcher(b["person"]["id"], pid)
                if stat:
                    matchup_table.append({"name": b["person"]["fullName"], **stat})
            matchup_table.sort(key=lambda m: -(to_num(m.get("ops")) or 0))
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] matchup table failed for pitcher {pid}: {e}")

        matchups.append({
            "pitcherId": pid, "name": job["pitcher"]["fullName"],
            "team": job["team"]["abbreviation"], "opp": job["opp"]["abbreviation"],
            "hand": fetch_pitcher_hand(pid),
            "season": season_stat,
            "savant": pitcher_pct_map.get(str(pid)),
            "arsenal": arsenal,
            "matchupTable": matchup_table[:12],
        })
        print(f"  {job['pitcher']['fullName']} ({job['team']['abbreviation']} vs {job['opp']['abbreviation']}): "
              f"{len(matchup_table)} opposing batters with history")
    return matchups


def build(date, year, games):
    batter_pct_map = fetch_savant_percentiles("batter", year)
    pitcher_pct_map = fetch_savant_percentiles("pitcher", year)

    teams = build_teams(year, batter_pct_map, pitcher_pct_map)
    matchups = build_matchups(games, year, batter_pct_map, pitcher_pct_map)

    return {
        "date": date, "generatedAt": datetime.utcnow().isoformat(),
        "teams": teams, "matchups": matchups,
    }


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    from build_hr import fetch_schedule

    date = sys.argv[1] if len(sys.argv) > 1 else today_iso()
    year = int(date[:4])
    games = fetch_schedule(date)
    result = build(date, year, games)
    os.makedirs("data/teams", exist_ok=True)
    with open(f"data/teams/{date}.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"Wrote data/teams/{date}.json with {len(result['teams'])} teams, {len(result['matchups'])} matchups")
