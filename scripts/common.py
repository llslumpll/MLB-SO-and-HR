"""
Shared helpers for the daily MLB projection pipeline.
Ported from the browser JS version of the HR Edge Board / Strikeouts tab,
so the math here should match what you saw in the live site.
"""

import csv
import io
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API = "https://statsapi.mlb.com/api/v1"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
SAVANT = "https://baseballsavant.mlb.com"
HEADERS = {"User-Agent": "mlb-edge-site/1.0"}

# Sourced 2026 HR park factors (thecapper.io, multi-year Statcast avg). 1.00 = league average.
PARKS = {
    "ARI": {"name": "Chase Field", "hr": 1.09, "roof": "retractable", "lat": 33.4453, "lon": -112.0667},
    "ATL": {"name": "Truist Park", "hr": 1.02, "roof": "open", "lat": 33.8908, "lon": -84.4678},
    "BAL": {"name": "Camden Yards", "hr": 1.08, "roof": "open", "lat": 39.2839, "lon": -76.6218},
    "BOS": {"name": "Fenway Park", "hr": 1.15, "roof": "open", "lat": 42.3467, "lon": -71.0972},
    "CHC": {"name": "Wrigley Field", "hr": 1.09, "roof": "open", "lat": 41.9484, "lon": -87.6553},
    "CWS": {"name": "Guaranteed Rate Field", "hr": 0.99, "roof": "open", "lat": 41.8299, "lon": -87.6338},
    "CHW": {"name": "Guaranteed Rate Field", "hr": 0.99, "roof": "open", "lat": 41.8299, "lon": -87.6338},
    "CIN": {"name": "Great American Ball Park", "hr": 1.18, "roof": "open", "lat": 39.0979, "lon": -84.5066},
    "CLE": {"name": "Cleveland Guardians Ballpark", "hr": 0.98, "roof": "open", "lat": 41.4962, "lon": -81.6852},
    "COL": {"name": "Coors Field", "hr": 1.47, "roof": "open", "lat": 39.7559, "lon": -104.9942},
    "DET": {"name": "Comerica Park", "hr": 0.95, "roof": "open", "lat": 42.3390, "lon": -83.0485},
    "HOU": {"name": "Minute Maid Park", "hr": 1.06, "roof": "retractable", "lat": 29.7573, "lon": -95.3555},
    "KC": {"name": "Kauffman Stadium", "hr": 0.96, "roof": "open", "lat": 39.0517, "lon": -94.4803},
    "LAA": {"name": "Angel Stadium", "hr": 0.94, "roof": "open", "lat": 33.8003, "lon": -117.8827},
    "LAD": {"name": "Dodger Stadium", "hr": 1.04, "roof": "open", "lat": 34.0739, "lon": -118.2400},
    "MIA": {"name": "loanDepot Park", "hr": 0.90, "roof": "retractable", "lat": 25.7781, "lon": -80.2196},
    "MIL": {"name": "American Family Field", "hr": 1.06, "roof": "retractable", "lat": 43.0280, "lon": -87.9712},
    "MIN": {"name": "Target Field", "hr": 0.99, "roof": "open", "lat": 44.9817, "lon": -93.2776},
    "NYM": {"name": "Citi Field", "hr": 0.86, "roof": "open", "lat": 40.7571, "lon": -73.8458},
    "NYY": {"name": "Yankee Stadium", "hr": 1.11, "roof": "open", "lat": 40.8296, "lon": -73.9262},
    "ATH": {"name": "Sutter Health Park", "hr": 1.05, "roof": "open", "lat": 38.5802, "lon": -121.5136},
    "OAK": {"name": "Sutter Health Park", "hr": 1.05, "roof": "open", "lat": 38.5802, "lon": -121.5136},
    "PHI": {"name": "Citizens Bank Park", "hr": 1.08, "roof": "open", "lat": 39.9061, "lon": -75.1665},
    "PIT": {"name": "PNC Park", "hr": 0.98, "roof": "open", "lat": 40.4468, "lon": -80.0057},
    "SD": {"name": "Petco Park", "hr": 0.92, "roof": "open", "lat": 32.7073, "lon": -117.1566},
    "SF": {"name": "Oracle Park", "hr": 0.86, "roof": "open", "lat": 37.7786, "lon": -122.3893},
    "SEA": {"name": "T-Mobile Park", "hr": 0.92, "roof": "retractable", "lat": 47.5914, "lon": -122.3325},
    "STL": {"name": "Busch Stadium", "hr": 1.04, "roof": "open", "lat": 38.6226, "lon": -90.1928},
    "TB": {"name": "Tropicana Field", "hr": 0.93, "roof": "dome", "lat": 27.7683, "lon": -82.6534},
    "TEX": {"name": "Globe Life Field", "hr": 1.12, "roof": "retractable", "lat": 32.7473, "lon": -97.0842},
    "TOR": {"name": "Rogers Centre", "hr": 1.00, "roof": "retractable", "lat": 43.6414, "lon": -79.3894},
    "WSH": {"name": "Nationals Park", "hr": 0.96, "roof": "open", "lat": 38.8730, "lon": -77.0074},
}

LEAGUE_AVG_HR_RATE = 0.028
LEAGUE_AVG_HIT_RATE = 0.22   # hits per PA, roughly modern MLB league average
LEAGUE_AVG_TB_RATE = 0.36    # total bases per PA, roughly modern MLB league average
LEAGUE_AVG_K9 = 8.5
LEAGUE_AVG_K_PCT = 22


def clip(v, lo, hi):
    return max(lo, min(hi, v))


def to_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        # pandas represents a missing/blank CSV cell as float NaN, not
        # None -- confirmed live on 2026-09-11: one MIN batter's missing
        # avg_hit_speed cell came through as NaN, and NaN silently
        # poisoned that whole team's average (nan + anything = nan),
        # corrupting the entire team's exitVelo from one player's gap.
        # Every other caller of to_num works from JSON or csv.DictReader,
        # neither of which can ever produce a real float NaN -- only the
        # pandas-based Savant fetches can, so this is safe everywhere
        # else in the codebase.
        return None if f != f else f  # f != f is true only for NaN
    except (TypeError, ValueError):
        return None


def get(url, params=None, retries=2):
    last_err = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5)
    raise last_err


def get_text(url, params=None, retries=2):
    last_err = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5)
    raise last_err


def parse_ip(ip_str):
    """MLB innings-pitched format: '123.1' means 123 and 1/3 innings."""
    if not ip_str:
        return None
    s = str(ip_str)
    parts = s.split(".")
    whole = int(parts[0]) if parts[0] not in ("", "-") else 0
    frac = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    extra = {1: 1 / 3, 2: 2 / 3}.get(frac, 0)
    return whole + extra


_savant_cache = {}


def fetch_savant_percentiles(kind, year):
    """kind: 'batter' or 'pitcher'. Returns {player_id_str: row_dict}."""
    key = f"{kind}_{year}"
    if key in _savant_cache:
        return _savant_cache[key]
    try:
        text = get_text(
            f"{SAVANT}/leaderboard/percentile-rankings",
            params={"type": kind, "year": year, "position": "", "team": "", "csv": "true"},
        )
        reader = csv.DictReader(io.StringIO(text))
        rows = {row["player_id"]: row for row in reader if row.get("player_id")}
        _savant_cache[key] = rows
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Savant percentile fetch failed for {kind}/{year}: {e}")
        _savant_cache[key] = {}
        return {}


_savant_ev_cache = {}


def fetch_savant_exitvelo_barrels(kind, year):
    """kind: 'batter' or 'pitcher'. Returns {player_id_str: row_dict}.

    Real (non-percentile) exit velocity/barrel/hard-hit rates from
    Savant's actual "Exit Velocity & Barrels" leaderboard --
    NOT the same endpoint as fetch_savant_percentiles above, which
    returns percentile RANKS under confusingly similar field names
    (brl_percent, hard_hit_percent, exit_velocity) despite sounding like
    raw rates. Confirmed via git history that build_teams.py was
    mistakenly averaging THOSE percentile ranks across a whole roster
    and displaying the result as if it were a real team-average exit
    velocity/barrel%/hard-hit% -- e.g. showing "44.3" as exit velocity
    when real MLB average is ~88mph. This endpoint's avg_hit_speed/
    brl_percent/ev95percent columns are genuine raw per-player values,
    verified against real Savant player pages (e.g. avg_hit_speed in the
    84-92 mph range, matching real exit velocities) before use.

    UNVERIFIED FROM THIS ENVIRONMENT: the exact CSV-trigger query param
    (assumed csv=true, matching the proven percentile-rankings pattern)
    and the "min" qualifier param (assumed min=1 for "every batter with
    at least one batted ball", broadest roster coverage). Savant isn't
    reachable from the sandbox this was written in -- confirm the first
    live run actually returns real mph/percent values before trusting
    this, the same way every other fix this session was checked against
    a real live pull."""
    key = f"{kind}_{year}"
    if key in _savant_ev_cache:
        return _savant_ev_cache[key]
    try:
        # Local import, not top-level: common.py is also imported by
        # refresh_odds.py, which runs every 10 minutes via a separate
        # workflow that only installs requests/tzdata. A top-level
        # pandas import here would break that job every single cycle.
        # pandas is only needed for these two functions, only called
        # from build_teams.py (daily.yml, which does install pandas).
        import pandas as pd
        text = get_text(
            f"{SAVANT}/leaderboard/statcast",
            params={"type": kind, "year": year, "position": "", "team": "", "min": "1", "csv": "true"},
        )
        # pandas' C parser, not Python's stdlib csv module -- see
        # fetch_savant_expected_stats's docstring for why. Kept
        # consistent between both functions even though this specific
        # endpoint's CSV happened to parse fine with csv.DictReader in
        # the 2026-09-11 live test (325/325 batters).
        df = pd.read_csv(io.StringIO(text), dtype={"player_id": str}, on_bad_lines="skip")
        rows = {row["player_id"]: row.to_dict() for _, row in df.iterrows() if row.get("player_id")}
        print(f"  [debug] exitvelo/barrels {kind}/{year}: parsed {len(rows)} rows with a player_id")
        _savant_ev_cache[key] = rows
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Savant exit-velo/barrels fetch failed for {kind}/{year}: {e}")
        _savant_ev_cache[key] = {}
        return {}


_savant_xstats_cache = {}


def fetch_savant_expected_stats(kind, year):
    """kind: 'batter' or 'pitcher'. Returns {player_id_str: row_dict}.

    Real (non-percentile) xwOBA/xBA/xSLG from Savant's "Expected
    Statistics" leaderboard -- same rationale and same unverified-CSV-
    param caveat as fetch_savant_exitvelo_barrels above."""
    key = f"{kind}_{year}"
    if key in _savant_xstats_cache:
        return _savant_xstats_cache[key]
    try:
        # Local import -- see fetch_savant_exitvelo_barrels's comment on
        # why this can't be a top-level import in this file.
        #
        # pandas instead of Python's stdlib csv module: live-tested
        # 2026-09-11, this specific endpoint's CSV (89KB) broke
        # csv.DictReader down to a single parsed row -- almost certainly
        # an unescaped quote character in a player's name somewhere in
        # the file, which makes the stdlib parser lose track of field
        # boundaries and swallow everything after it into one field.
        # pandas' C parser handles this far more gracefully, and
        # on_bad_lines="skip" means one malformed row (one missing
        # player, worst case) can't take the whole fetch down with it.
        import pandas as pd
        text = get_text(
            f"{SAVANT}/leaderboard/expected_statistics",
            params={"type": kind, "year": year, "position": "", "team": "", "min": "1", "csv": "true"},
        )
        df = pd.read_csv(io.StringIO(text), dtype={"player_id": str}, on_bad_lines="skip")
        rows = {row["player_id"]: row.to_dict() for _, row in df.iterrows() if row.get("player_id")}
        print(f"  [debug] expected-stats {kind}/{year}: parsed {len(rows)} rows with a player_id")
        _savant_xstats_cache[key] = rows
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] Savant expected-stats fetch failed for {kind}/{year}: {e}")
        _savant_xstats_cache[key] = {}
        return {}


_weather_cache = {}


def fetch_weather(lat, lon, game_date_iso):
    key = f"{lat}_{lon}_{game_date_iso[:10]}"
    if key in _weather_cache:
        return _weather_cache[key]
    try:
        game_dt = datetime.fromisoformat(game_date_iso.replace("Z", "+00:00"))
        date_str = game_dt.strftime("%Y-%m-%d")
        data = get(
            OPEN_METEO,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relative_humidity_2m",
                "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
                "timezone": "auto", "start_date": date_str, "end_date": date_str,
            },
        )
        hourly = data.get("hourly")
        if not hourly:
            _weather_cache[key] = None
            return None
        times = hourly["time"]
        best_idx, best_diff = 0, None
        for i, t in enumerate(times):
            t_dt = datetime.fromisoformat(t)
            diff = abs((t_dt - game_dt.replace(tzinfo=None)).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff, best_idx = diff, i
        result = {
            "tempF": hourly["temperature_2m"][best_idx],
            "windMph": hourly["windspeed_10m"][best_idx],
            "windDir": hourly["winddirection_10m"][best_idx],
            "humidity": hourly["relative_humidity_2m"][best_idx],
        }
        _weather_cache[key] = result
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] weather fetch failed: {e}")
        _weather_cache[key] = None
        return None


def today_iso():
    # MLB's "today" follows US local time, not UTC -- during US evening hours,
    # UTC has already rolled over to the next calendar day, which was causing
    # every script to silently build/look up the wrong date's board.
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


_arsenal_cache = {}


def fetch_pitch_arsenal(pitcher_id, year):
    """Full-season pitch mix (type/usage/velocity) and zone-attack breakdown,
    aggregated from raw Statcast pitch-by-pitch data."""
    if pitcher_id in _arsenal_cache:
        return _arsenal_cache[pitcher_id]
    try:
        start = f"{year}-01-01"
        end = today_iso()
        text = get_text(f"{SAVANT}/statcast_search/csv", params={
            "all": "true", "hfGT": "R", "player_type": "pitcher",
            "game_date_gt": start, "game_date_lt": end,
            "pitchers_lookup[]": pitcher_id, "type": "details",
        })
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            _arsenal_cache[pitcher_id] = None
            return None
        groups = {}
        zone_counts = {}
        in_zone, zone_known = 0, 0
        for r in rows:
            name = (r.get("pitch_name") or "").strip() or r.get("pitch_type") or "Unknown"
            g = groups.setdefault(name, {"count": 0, "speed_sum": 0.0, "speed_n": 0})
            g["count"] += 1
            spd = to_num(r.get("release_speed"))
            if spd is not None:
                g["speed_sum"] += spd
                g["speed_n"] += 1
            z = r.get("zone")
            try:
                z = int(float(z))
            except (TypeError, ValueError):
                z = None
            if z is not None:
                zone_known += 1
                if 1 <= z <= 9:
                    in_zone += 1
                    zone_counts[z] = zone_counts.get(z, 0) + 1
        total = len(rows)
        arsenal = sorted(
            [{"name": n, "count": g["count"], "usage": g["count"] / total,
              "avgVelo": (g["speed_sum"] / g["speed_n"]) if g["speed_n"] else None}
             for n, g in groups.items()],
            key=lambda a: -a["count"],
        )
        result = {
            "arsenal": arsenal,
            "zonePct": (in_zone / zone_known) if zone_known else None,
            "zoneCounts": zone_counts,
            "totalPitches": total,
        }
        _arsenal_cache[pitcher_id] = result
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] pitch arsenal fetch failed for {pitcher_id}: {e}")
        _arsenal_cache[pitcher_id] = None
        return None


_batter_hrs_cache = {}


def fetch_player_home_runs(batter_id, year):
    """This season's home runs for one batter, each with real per-event
    exit velocity/launch angle/distance -- for the homepage Statcast
    Spotlight widget.

    Deliberately reuses fetch_pitch_arsenal's exact request shape above
    (same endpoint, same param style, just batters_lookup[] instead of
    pitchers_lookup[] and player_type=batter) rather than guessing a new
    server-side event-type filter param -- that's the same full-season
    per-pitch pull already proven to work in production for every
    starting pitcher via fetch_pitch_arsenal, just filtered for
    events=='home_run' in code afterward, where it can be verified
    directly instead of trusted blind. Costs one more full-season fetch
    per call (a lot of rows for one player) but is the safe, boring
    choice over an untested filter param -- see fetch_savant_
    exitvelo_barrels's docstring for why that kind of guess needs
    checking against real live data before it's trusted."""
    if batter_id in _batter_hrs_cache:
        return _batter_hrs_cache[batter_id]
    try:
        start = f"{year}-01-01"
        end = today_iso()
        text = get_text(f"{SAVANT}/statcast_search/csv", params={
            "all": "true", "hfGT": "R", "player_type": "batter",
            "game_date_gt": start, "game_date_lt": end,
            "batters_lookup[]": batter_id, "type": "details",
        })
        reader = csv.DictReader(io.StringIO(text))
        home_runs = []
        for r in reader:
            if (r.get("events") or "").strip() != "home_run":
                continue
            ev = to_num(r.get("launch_speed"))
            la = to_num(r.get("launch_angle"))
            dist = to_num(r.get("hit_distance_sc"))
            if ev is None or la is None or dist is None:
                continue
            home_runs.append({
                "exitVelo": ev, "launchAngle": la, "distance": dist,
                "date": r.get("game_date"),
            })
        _batter_hrs_cache[batter_id] = home_runs
        return home_runs
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] home run event fetch failed for {batter_id}: {e}")
        _batter_hrs_cache[batter_id] = []
        return []


def fetch_vs_team(person_id, team_id, group):
    """group: 'hitting' or 'pitching'. Returns {"season": stat_or_None,
    "career": stat_or_None} for one player against one specific team.

    IMPORTANT: vsTeam (no "Total") is SEASON-scoped, not career -- verified
    via the mlbstatsapi wrapper's own documented examples showing vsplayer
    (2 games) vs vsplayertotal (4 games) for the same two players, the
    same season/career split applies to vsTeam/vsTeamTotal. Confirmed via
    MLB's own official statTypes endpoint (https://statsapi.mlb.com/api/v1/statTypes)
    that both vsTeam and vsTeamTotal are real, separate, supported types.
    Fetching both matters: a trend like "5 HR this season vs the
    Dodgers" and a trend like "124 career IP vs the Rangers at a 2.69
    ERA" are genuinely different kinds of facts and must never be
    mislabeled as the other -- this was flagged directly by the user
    after the first version of this fetch only pulled the season split.

    UNVERIFIED FROM THIS ENVIRONMENT: MLB Stats API isn't reachable from
    the sandbox this was written in, so the exact param name
    (opposingTeamId) is inferred by direct analogy to fetch_vs_pitcher's
    already-proven opposingPlayerId, not independently confirmed live.
    Check the first real run's output before trusting this -- same
    standard as every other new endpoint added this session."""
    def _fetch(stat_type):
        try:
            data = get(f"{API}/people/{person_id}/stats", params={
                "stats": stat_type, "group": group,
                "opposingTeamId": team_id, "sportId": 1,
            })
            splits = (data.get("stats") or [{}])[0].get("splits") or []
            return splits[0]["stat"] if splits else None
        except Exception:  # noqa: BLE001
            return None
    return {"season": _fetch("vsTeam"), "career": _fetch("vsTeamTotal")}


def fetch_vs_pitcher_full(batter_id, pitcher_id):
    """Same season/career split as fetch_vs_team above, but for one
    batter against one specific opposing pitcher -- the vsPlayer/
    vsPlayerTotal pair. Deliberately separate from fetch_vs_pitcher
    above (which stays exactly as it was, still season-only, still
    used by Teams/Matchups' opposing-batters table) rather than
    changing that function's behavior for existing call sites; this is
    purely for the new batter-vs-pitcher trend chip."""
    def _fetch(stat_type):
        try:
            data = get(f"{API}/people/{batter_id}/stats", params={
                "stats": stat_type, "group": "hitting",
                "opposingPlayerId": pitcher_id, "sportId": 1,
            })
            splits = (data.get("stats") or [{}])[0].get("splits") or []
            return splits[0]["stat"] if splits else None
        except Exception:  # noqa: BLE001
            return None
    return {"season": _fetch("vsPlayer"), "career": _fetch("vsPlayerTotal")}


def batter_vs_team_trend(splits):
    """A notable trend against today's specific opponent team, checking
    CAREER first (the more meaningful long-term signal, e.g. Sonny
    Gray's 124 career IP vs the Rangers) and falling back to THIS
    SEASON only if career doesn't clear the bar (e.g. Elly De La Cruz's
    5 HR vs the Dodgers this year) -- either way the result says which
    timeframe it actually is, never left ambiguous. splits is the
    {"season":..., "career":...} dict fetch_vs_team returns.

    Thresholds (10%+ HR rate, i.e. roughly 1 HR every 10 AB, or .950+
    OPS, 15+ AB floor) are a judgment call agreed with the user on
    2026-09-18 -- deliberately not "any deviation from average", since
    a trend chip on every card would make the notable ones invisible.
    HR RATE, not a flat HR count: a flat "4+ HR" threshold looked right
    for a single season but was too easy to clear over a whole CAREER
    just by accumulation (8 HR in 200 AB across many years is a
    perfectly average ~4% rate, not a real trend, yet would have passed
    a flat count check) -- caught by testing against a deliberately
    unremarkable mock before this ever reached production. Rate scales
    correctly regardless of which timeframe (season or career) actually
    produced the sample. 15 AB is a real floor, not the 3 AB
    fetch_vs_pitcher uses for vs-one-pitcher matchups -- vs-team
    samples are naturally much bigger (every pitcher on that team, not
    just one), so a small sample here is less excusable."""
    def _check(stat):
        if not stat:
            return None
        ab = to_num(stat.get("atBats"))
        if not ab or ab < 15:
            return None
        hr = int(to_num(stat.get("homeRuns")) or 0)
        ops = to_num(stat.get("ops"))
        if (hr / ab) >= 0.10 or (ops is not None and ops >= 0.950):
            return {
                "atBats": int(ab), "hits": int(to_num(stat.get("hits")) or 0),
                "homeRuns": hr, "avg": stat.get("avg"), "ops": stat.get("ops"),
            }
        return None
    if not splits:
        return None
    career_hit = _check(splits.get("career"))
    if career_hit:
        career_hit["timeframe"] = "career"
        return career_hit
    season_hit = _check(splits.get("season"))
    if season_hit:
        season_hit["timeframe"] = "season"
        return season_hit
    return None


def batter_vs_pitcher_trend(splits):
    """Same career-first-then-season logic as batter_vs_team_trend, for
    one batter against today's specific opposing PITCHER. Same HR-rate
    reasoning (see batter_vs_team_trend's docstring), lower thresholds
    than the vs-team version (10%+ HR rate or .900+ OPS, 8+ AB floor)
    since a single pitcher's sample is naturally much smaller than a
    whole team's -- fetch_vs_pitcher's existing 3 AB floor (used
    elsewhere, unaffected by this) shows that a meaningful vs-one-
    pitcher trend can exist at a much smaller sample than vs-team."""
    def _check(stat):
        if not stat:
            return None
        ab = to_num(stat.get("atBats"))
        if not ab or ab < 8:
            return None
        hr = int(to_num(stat.get("homeRuns")) or 0)
        ops = to_num(stat.get("ops"))
        if (hr / ab) >= 0.10 or (ops is not None and ops >= 0.900):
            return {
                "atBats": int(ab), "hits": int(to_num(stat.get("hits")) or 0),
                "homeRuns": hr, "avg": stat.get("avg"), "ops": stat.get("ops"),
            }
        return None
    if not splits:
        return None
    career_hit = _check(splits.get("career"))
    if career_hit:
        career_hit["timeframe"] = "career"
        return career_hit
    season_hit = _check(splits.get("season"))
    if season_hit:
        season_hit["timeframe"] = "season"
        return season_hit
    return None


def pitcher_vs_team_trend(splits):
    """Same career-first-then-season logic, for pitchers -- notable in
    EITHER direction (dominant or has really struggled), since both are
    real, useful trends, not just the flattering one. ERA thresholds
    (<=3.00 dominant, >=6.00 has struggled) with a 15+ IP floor.

    Also checks K% (strikeOuts/battersFaced) as its own separate trigger,
    added 2026-09-24 at the user's request specifically for strikeout
    props -- ERA and K% can genuinely diverge (a pitcher can run a great
    ERA against a team via weak contact without missing many bats, or
    the reverse), so a real strikeout-specific signal matters here even
    when ERA alone wouldn't have flagged anything. battersFaced is
    already a proven field from this exact stat shape (used elsewhere
    in build_ko.py's own season-stat fetch), so this needed no new
    endpoint. Thresholds (>=28% elite, <=15% really struggles to miss
    bats) are set relative to a roughly 22-23% MLB-average K% -- a
    reasoned judgment call, not backtested against this project's own
    accumulated vsTeam history the way the HR factors were, since this
    specific check is new."""
    def _check(stat):
        if not stat:
            return None
        ip = to_num(stat.get("inningsPitched"))
        if not ip or ip < 15:
            return None
        era = to_num(stat.get("era"))
        strike_outs = to_num(stat.get("strikeOuts")) or 0
        batters_faced = to_num(stat.get("battersFaced"))
        k_pct = (strike_outs / batters_faced * 100) if batters_faced else None
        era_trigger = era is not None and (era <= 3.00 or era >= 6.00)
        k_trigger = k_pct is not None and (k_pct >= 28.0 or k_pct <= 15.0)
        if era_trigger or k_trigger:
            return {
                "inningsPitched": stat.get("inningsPitched"), "era": stat.get("era"),
                "whip": stat.get("whip"), "strikeOuts": int(strike_outs),
                "kPct": round(k_pct, 1) if k_pct is not None else None,
            }
        return None
    if not splits:
        return None
    career_hit = _check(splits.get("career"))
    if career_hit:
        career_hit["timeframe"] = "career"
        return career_hit
    season_hit = _check(splits.get("season"))
    if season_hit:
        season_hit["timeframe"] = "season"
        return season_hit
    return None


def pitcher_vs_batter_trend(splits):
    """The pitcher's side of the exact same at-bats batter_vs_pitcher_trend
    already checks -- no new fetch, this is fetch_vs_pitcher_full's
    output (batting-side stat line: AB, HR, AVG, OPS) reused, just with
    a threshold that also checks the direction batter_vs_pitcher_trend
    doesn't: the PITCHER dominating this specific batter (very low OPS
    against), not just the batter dominating the pitcher. Same career-
    first-then-season logic, same 8+ AB floor, same 10%+ HR rate or
    .900+ OPS for batter dominance; pitcher dominance is OPS <=.500 with
    the same 8+ AB floor (a real shutdown trend, not just a cold
    streak)."""
    def _check(stat):
        if not stat:
            return None
        ab = to_num(stat.get("atBats"))
        if not ab or ab < 8:
            return None
        hr = int(to_num(stat.get("homeRuns")) or 0)
        ops = to_num(stat.get("ops"))
        if ops is not None and ops <= 0.500:
            return {
                "atBats": int(ab), "hits": int(to_num(stat.get("hits")) or 0),
                "homeRuns": hr, "avg": stat.get("avg"), "ops": stat.get("ops"),
                "direction": "pitcher",
            }
        if (hr / ab) >= 0.10 or (ops is not None and ops >= 0.900):
            return {
                "atBats": int(ab), "hits": int(to_num(stat.get("hits")) or 0),
                "homeRuns": hr, "avg": stat.get("avg"), "ops": stat.get("ops"),
                "direction": "batter",
            }
        return None
    if not splits:
        return None
    career_hit = _check(splits.get("career"))
    if career_hit:
        career_hit["timeframe"] = "career"
        return career_hit
    season_hit = _check(splits.get("season"))
    if season_hit:
        season_hit["timeframe"] = "season"
        return season_hit
    return None


def fetch_team_last_n_record(team_id, n=5, lookback_days=20):
    """Record over the team's last N completed games."""
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=lookback_days)
        data = get(f"{API}/schedule", params={
            "teamId": team_id, "sportId": 1, "gameType": "R",
            "startDate": start.strftime("%Y-%m-%d"), "endDate": end.strftime("%Y-%m-%d"),
        })
        games = []
        for d in data.get("dates") or []:
            games.extend(d.get("games") or [])
        finals = [g for g in games if g.get("status", {}).get("abstractGameState") == "Final"]
        finals.sort(key=lambda g: g.get("gameDate", ""))
        last_n = finals[-n:]
        wins, losses = 0, 0
        for g in last_n:
            home = g["teams"]["home"]
            away = g["teams"]["away"]
            is_home = home["team"]["id"] == team_id
            me, opp = (home, away) if is_home else (away, home)
            if me.get("isWinner"):
                wins += 1
            elif opp.get("isWinner"):
                losses += 1
        return {"wins": wins, "losses": losses, "games": len(last_n)}
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] last-N record fetch failed for team {team_id}: {e}")
        return None


def prob_to_american_odds(prob):
    prob = clip(prob, 0.01, 0.99)
    if prob >= 0.5:
        odds = -100 * prob / (1 - prob)
    else:
        odds = 100 * (1 - prob) / prob
    return int(round(odds))


def norm_name(s):
    import unicodedata
    import re
    s = (s or "").lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_prior_season_ip_cache = {}


def fetch_prior_season_ip(pitcher_id, year):
    """Total innings pitched in the PRIOR season -- same endpoint/param
    shape as the already-proven season stat fetch in
    fetch_pitcher_projection (build_ko.py), just season=year-1 instead
    of the current year.

    Built 2026-09-21 alongside the documented hook-risk limitation in
    build_ko.py: a pitcher already at or past his full prior-season IP
    total is at real, concrete risk of being protected/shut down or
    given a shorter leash down the stretch -- this is a real roster-
    management fact many teams follow, not a vague guess, but it's a
    NEW signal with no backtested data behind it yet (unlike the HR
    factor work, there's no way to reconstruct what this value would
    have been for historical entries without having already been
    fetching and storing it). Applied dampened and monitored, not
    blindly trusted -- see its use in fetch_pitcher_projection."""
    if pitcher_id in _prior_season_ip_cache:
        return _prior_season_ip_cache[pitcher_id]
    try:
        data = get(f"{API}/people/{pitcher_id}/stats", params={
            "stats": "season", "group": "pitching", "season": year - 1,
        })
        stats = data.get("stats") or []
        season_group = next((s for s in stats if s["type"]["displayName"] == "season"), None)
        if season_group and season_group.get("splits"):
            stat = season_group["splits"][0]["stat"]
            ip = parse_ip(stat.get("inningsPitched"))
            _prior_season_ip_cache[pitcher_id] = ip
            return ip
        _prior_season_ip_cache[pitcher_id] = None
        return None
    except Exception:  # noqa: BLE001
        _prior_season_ip_cache[pitcher_id] = None
        return None


def fetch_team_games_back(team_id, year):
    """Real games-back for one team, from the same proven /standings
    endpoint already used in build_teams.py's fetch_standings (kept as
    its own scoped function here rather than importing across build_ko/
    build_teams, which aren't currently coupled).

    Returns {"gamesBack": float, "wins": int, "losses": int} or None.
    gamesBack is 0.0 for a division leader. Used as a simple, honestly-
    imperfect "postseason stakes" proxy: DIVISION race proximity only --
    this does NOT account for the wildcard race, so a team whose real
    drama is a wildcard chase, not the division, may get misclassified
    as "no stakes" here. Built 2026-09-21 alongside the season-IP
    signal, same dampened/monitored treatment, not a fully validated
    correction."""
    try:
        data = get(f"{API}/standings", params={
            "leagueId": "103,104", "season": year, "standingsTypes": "regularSeason",
        })
        for rec in data.get("records") or []:
            for tr in rec.get("teamRecords") or []:
                if tr["team"]["id"] == team_id:
                    gb_raw = tr.get("gamesBack")
                    gb = 0.0 if gb_raw in (None, "-") else to_num(gb_raw)
                    return {"gamesBack": gb, "wins": tr.get("wins"), "losses": tr.get("losses")}
        return None
    except Exception:  # noqa: BLE001
        return None


NEWS_FEEDS = {
    "MLB.com": "https://www.mlb.com/feeds/news/rss.xml",
    "ESPN": "https://www.espn.com/espn/rss/mlb/news",
}


def fetch_mlb_news():
    """Real MLB news headlines from two official/major RSS feeds --
    confirmed live and working 2026-09-21 (pulled real current headlines
    from both before building this, not assumed). Plain XML via the
    standard library's ElementTree, no new dependency.

    Built alongside the player-card news matching in run_daily.py: this
    turns out to be a genuine safety feature, not just a nice-to-have --
    confirmed live the same day that this exact feed had "Cease
    (shoulder) undergoes MRI, scratched from next start" and "Skubal to
    bereavement list" for two pitchers who had real entries on that
    day's actual K board. A frozen, hours-old prediction for someone who
    just got scratched is exactly the kind of thing this project's own
    "be honest about limitations" philosophy says to catch, not ship
    quietly.

    Returns a list of {title, link, pubDate, source} dicts, most recent
    first within each feed (feed order preserved, not globally re-
    sorted -- pubDate formats differ slightly between sources and
    parsing them all into one guaranteed-correct sort order isn't worth
    the risk of getting it subtly wrong; two clearly-labeled recent
    lists is more honest than one falsely-precise merged one)."""
    import xml.etree.ElementTree as ET

    items = []
    for source, url in NEWS_FEEDS.items():
        try:
            text = get_text(url)
            root = ET.fromstring(text)
            for item in root.findall(".//item")[:30]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "pubDate": pub_date, "source": source})
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] news feed fetch failed for {source}: {e}")
    return items


def match_news_to_players(news_items, player_names):
    """Match news headlines to player names by last-name, whole-word,
    case-insensitive substring. Returns {player_name: [matching items]}.

    A helper for SURFACING headlines, not a hard automated filter or
    decision -- always keeps the original headline text attached so a
    person can make the actual judgment call quickly (e.g. "Cease
    scratched" is obviously relevant to a Dylan Cease projection; a
    coincidental name match on a common surname might not be), rather
    than silently acting on a possibly-wrong match.

    Skips last names under 4 characters (e.g. "Lee", "Wu") -- too short
    to match reliably without a real risk of matching an unrelated
    story that happens to contain the same short, common word."""
    import re
    matches = {}
    for player_name in player_names:
        if not player_name:
            continue
        last_name = player_name.split()[-1]
        if len(last_name) < 4:
            continue
        pattern = re.compile(r"\b" + re.escape(last_name) + r"\b", re.IGNORECASE)
        found = [item for item in news_items if pattern.search(item["title"])]
        if found:
            matches[player_name] = found
    return matches


def pitcher_first_meeting_this_season(splits):
    """True when this pitcher hasn't faced this specific opponent team
    at ALL yet this season (regardless of career history against them).
    Added 2026-09-24 at the user's request, from a real handicapper
    writeup (WizBetz) making exactly this point: no facing them yet
    this season means no in-season familiarity or adjustment on either
    side, which is a real, simple, useful fact on its own.

    Reuses the same splits dict fetch_vs_team already returns (season +
    career) -- no new fetch. If the season split is None, the MLB Stats
    API returned no split at all for this pairing this year, which
    means zero games faced (any real appearance, even a token relief
    outing, would return a real stat line with some innings on it)."""
    if not splits:
        return False
    return splits.get("season") is None
