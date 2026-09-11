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
    fetch_savant_percentiles, fetch_savant_exitvelo_barrels, fetch_savant_expected_stats,
    fetch_pitch_arsenal, fetch_team_last_n_record,
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


def team_hitting_rates(hit_stat):
    """K% and BABIP computed directly from the team hitting totals
    fetch_team_season_stats already retrieves -- no separate API call.
    BABIP excludes home runs from both the numerator and the ball-in-play
    denominator, the standard sabermetric definition."""
    if not hit_stat:
        return None, None
    pa = to_num(hit_stat.get("plateAppearances"))
    so = to_num(hit_stat.get("strikeOuts"))
    k_pct = (so / pa * 100) if pa and so is not None else None

    ab = to_num(hit_stat.get("atBats"))
    h = to_num(hit_stat.get("hits"))
    hr = to_num(hit_stat.get("homeRuns")) or 0
    sf = to_num(hit_stat.get("sacFlies")) or 0
    babip_denom = (ab or 0) - (so or 0) - hr + sf
    babip = ((h - hr) / babip_denom) if (h is not None and babip_denom and babip_denom > 0) else None

    return k_pct, babip


def team_pitching_k_rate(pitch_stat):
    """Real team strikeout rate for the pitching staff, computed directly
    from the team pitching totals fetch_team_season_stats already
    retrieves from the MLB Stats API -- same real-stat pattern as
    team_hitting_rates above, no Savant dependency needed at all. This
    replaces the old pitcherKPct, which averaged Savant PERCENTILE ranks
    across the pitching staff and mislabeled the result as a real K% --
    a real, cheaper fix was sitting in data already being fetched."""
    if not pitch_stat:
        return None
    bf = to_num(pitch_stat.get("battersFaced"))
    so = to_num(pitch_stat.get("strikeOuts"))
    return (so / bf * 100) if bf and so is not None else None


def fetch_team_hitting_split(team_id, year):
    """Team-level equivalent of fetch_platoon_hr in build_hr.py -- same
    statSplits/sitCodes API pattern already proven working for individual
    batters, applied to a team instead of a person. Returns {"vl": stat,
    "vr": stat} so the caller can pick the split matching today's actual
    starting pitcher's hand, same lookup pattern already used for
    individual batter platoon splits elsewhere in this codebase."""
    try:
        data = get(f"{API}/teams/{team_id}/stats", params={
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
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] team hitting split fetch failed for {team_id}: {e}")
        return {}


def fetch_roster_savant_avg(team_id, batter_ev_map, batter_xstats_map, pitcher_pct_map):
    """Roster-average Statcast quality metrics.

    barrelPct/hardHitPct/exitVelo/xwoba are now real (non-percentile)
    values, averaged from Savant's actual Exit Velocity & Barrels /
    Expected Statistics leaderboards -- see fetch_savant_exitvelo_barrels
    in common.py for the full story on why this changed. Before this
    fix, these were roster-averaged PERCENTILE RANKS (0-100 scale)
    displayed as if they were real mph/percent/xwOBA -- confirmed live
    on 2026-09-10: exitVelo was showing values like 44.3 (should be
    ~88mph), and the matchup card's "Opp xwOBA" chip was rendering
    red/"bad" on literally every team, unconditionally, because its
    0.310/0.340 thresholds assumed a real 0-1 xwOBA scale but the
    averaged-percentile values were always 30-70.

    whiffPctile/chasePctile are, honestly, still percentile averages --
    there's no bulk real-stat leaderboard for team-wide pitching-staff
    plate discipline on Savant, only per-pitch aggregation, which would
    mean 12+ heavy CSV pulls per team x 30 teams to do properly. Left as
    percentiles rather than guessing at an expensive, untested pipeline;
    named/labeled accordingly (see index.html) instead of passed off as
    real rates the way they used to be."""
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
            "barrelPct": avg_field(batters, batter_ev_map, "brl_percent"),
            "hardHitPct": avg_field(batters, batter_ev_map, "ev95percent"),
            "exitVelo": avg_field(batters, batter_ev_map, "avg_hit_speed"),
            "xwoba": avg_field(batters, batter_xstats_map, "est_woba"),
            "whiffPctile": avg_field(pitchers, pitcher_pct_map, "whiff_percent"),
            "chasePctile": avg_field(pitchers, pitcher_pct_map, "chase_percent"),
        }
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] roster savant avg failed for {team_id}: {e}")
        return {}


def build_teams(year, batter_ev_map, batter_xstats_map, pitcher_pct_map):
    print("Building team records/stats for all 30 teams...")
    team_list = fetch_team_list(year)
    standings = fetch_standings(year)

    teams = []
    for t in team_list:
        standing = standings.get(t["id"], {})
        hit, pitch = fetch_team_season_stats(t["id"], year)
        last5 = fetch_team_last_n_record(t["id"], n=5)
        savant = fetch_roster_savant_avg(t["id"], batter_ev_map, batter_xstats_map, pitcher_pct_map)
        # Real team-wide pitching K%, computed from the same team pitching
        # totals already fetched above -- no Savant call needed for this
        # one at all, see team_pitching_k_rate's docstring.
        savant["pitcherKPct"] = team_pitching_k_rate(pitch)

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


def build_matchups(games, year, batter_ev_map, batter_xstats_map, pitcher_pct_map):
    print("Building today's pitcher matchup scouting reports...")
    matchups = []
    jobs = []
    for g in games:
        away_p = g["teams"]["away"].get("probablePitcher")
        home_p = g["teams"]["home"].get("probablePitcher")
        if away_p:
            jobs.append({"pitcher": away_p, "team": g["teams"]["away"]["team"], "opp": g["teams"]["home"]["team"], "gameDate": g.get("gameDate")})
        if home_p:
            jobs.append({"pitcher": home_p, "team": g["teams"]["home"]["team"], "opp": g["teams"]["away"]["team"], "gameDate": g.get("gameDate")})

    opp_team_stats_cache = {}
    opp_platoon_cache = {}

    def get_opp_team_profile(opp_team_id):
        # Cached per team since multiple pitchers today could share the
        # same opponent across different games -- no reason to re-fetch
        # the same team's aggregate stats twice in one run.
        if opp_team_id in opp_team_stats_cache:
            return opp_team_stats_cache[opp_team_id]
        hit_stat, _ = fetch_team_season_stats(opp_team_id, year)
        k_pct, babip = team_hitting_rates(hit_stat)
        # xwoba/hardHitPct now come from fetch_roster_savant_avg's real
        # (non-percentile) values -- this is the exact call that was
        # feeding the matchup card's "Opp xwOBA" chip a 30-70 scale
        # percentile average against thresholds written for a real 0-1
        # xwOBA, making it render red/"bad" unconditionally. Fixed at
        # the source; no change needed to the chip's thresholds.
        roster_savant = fetch_roster_savant_avg(opp_team_id, batter_ev_map, batter_xstats_map, pitcher_pct_map)
        profile = {"kPct": k_pct, "babip": babip, "xwoba": roster_savant.get("xwoba"), "hardHitPct": roster_savant.get("hardHitPct")}
        opp_team_stats_cache[opp_team_id] = profile
        return profile

    def get_opp_platoon_profile(opp_team_id, pitcher_hand):
        # Both vl and vr are fetched together and cached per team -- the
        # per-pitcher lookup just picks the side matching today's actual
        # starter, same pattern already used for individual batter
        # platoon splits elsewhere in this codebase.
        if opp_team_id not in opp_platoon_cache:
            opp_platoon_cache[opp_team_id] = fetch_team_hitting_split(opp_team_id, year)
        splits = opp_platoon_cache[opp_team_id]
        split_code = "vl" if pitcher_hand == "L" else "vr"
        split_stat = splits.get(split_code)
        if not split_stat:
            return None
        k_pct, babip = team_hitting_rates(split_stat)
        return {
            "avg": to_num(split_stat.get("avg")),
            "kPct": k_pct,
            "babip": babip,
            "vsHand": pitcher_hand,
            "plateAppearances": to_num(split_stat.get("plateAppearances")),
        }

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

        pitcher_hand = fetch_pitcher_hand(pid)
        opp_profile = get_opp_team_profile(job["opp"]["id"])
        opp_platoon_profile = get_opp_platoon_profile(job["opp"]["id"], pitcher_hand) if pitcher_hand else None

        matchups.append({
            "pitcherId": pid, "name": job["pitcher"]["fullName"],
            "team": job["team"]["abbreviation"], "opp": job["opp"]["abbreviation"],
            "hand": pitcher_hand,
            "season": season_stat,
            "savant": pitcher_pct_map.get(str(pid)),
            "arsenal": arsenal,
            "matchupTable": matchup_table[:12],
            "gameDate": job.get("gameDate"),
            "oppProfile": opp_profile,
            "oppPlatoonProfile": opp_platoon_profile,
        })
        print(f"  {job['pitcher']['fullName']} ({job['team']['abbreviation']} vs {job['opp']['abbreviation']}): "
              f"{len(matchup_table)} opposing batters with history")
    return matchups


def build(date, year, games):
    # pitcher_pct_map is still percentile-based -- used for whiffPctile/
    # chasePctile (honestly labeled, see fetch_roster_savant_avg) and for
    # each individual starter's own scouting-card percentile chips, which
    # were already using this data correctly.
    pitcher_pct_map = fetch_savant_percentiles("pitcher", year)
    # Real (non-percentile) batter quality-of-contact, replacing the old
    # batter_pct_map-based average. batter_pct_map itself is no longer
    # needed here now that nothing averages it.
    batter_ev_map = fetch_savant_exitvelo_barrels("batter", year)
    batter_xstats_map = fetch_savant_expected_stats("batter", year)

    teams = build_teams(year, batter_ev_map, batter_xstats_map, pitcher_pct_map)
    matchups = build_matchups(games, year, batter_ev_map, batter_xstats_map, pitcher_pct_map)

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
