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
LEAGUE_AVG_K9 = 8.5
LEAGUE_AVG_K_PCT = 22


def clip(v, lo, hi):
    return max(lo, min(hi, v))


def to_num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
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
