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
    API, LEAGUE_AVG_K9, LEAGUE_AVG_K_PCT, PARKS,
    clip, to_num, get, parse_ip,
    fetch_savant_percentiles, fetch_weather, today_iso,
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


def load_outs_calibration():
    """Same as load_ko_calibration but for the 'outs' section -- this
    correction only ever affects the projection math, never the site's
    visible History tab."""
    try:
        with open("data/calibration.json") as f:
            return (json.load(f) or {}).get("outs", {})
    except Exception:  # noqa: BLE001
        return {}


def percentile_to_factor(percentile, lo=0.75, hi=1.35):
    """Maps a 0-100 Savant percentile rank to a bounded factor centered
    at 1.0 for a league-average (50th percentile) pitcher. This is the
    correct way to use percentile-rankings data as a multiplier -- unlike
    dividing the percentile by some raw-rate-style number, which silently
    clips almost everyone above a modest percentile to the same ceiling."""
    if percentile is None:
        return None
    if percentile >= 50:
        factor = 1.0 + (percentile - 50) / 50 * (hi - 1.0)
    else:
        factor = 1.0 - (50 - percentile) / 50 * (1.0 - lo)
    return clip(factor, lo, hi)


def fetch_roster_k_percent(team_id, year):
    """Real team strikeout rate from season hitting stats (K / plate
    appearances), NOT an average of Savant percentile ranks. That earlier
    approach was averaging 0-100 percentile-rank numbers (Savant's
    percentile-rankings endpoint returns rank, not the raw stat) and
    displaying/using the result as if it were a real K% -- percentile
    ranks scatter widely around 50 by construction, which is exactly the
    implausibly wide, too-high pattern that was showing up on the site."""
    try:
        data = get(f"{API}/teams/{team_id}/stats", params={"stats": "season", "group": "hitting", "season": year})
        splits = ((data.get("stats") or [{}])[0]).get("splits") or []
        if not splits:
            return None
        stat = splits[0].get("stat") or {}
        so = to_num(stat.get("strikeOuts"))
        pa = to_num(stat.get("plateAppearances"))
        if so is None or not pa:
            return None
        return (so / pa) * 100
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] team K%% fetch failed for team {team_id}: {e}")
        return None


# Recent-form matchup refinement: caches expensive per-team lookups across
# all of today's games sharing the same opponent, same reasoning as the
# opponent-profile caching already used in build_teams.py.
_RECENT_MATCHUP_SCHEDULE_CACHE = {}
_PITCHER_HAND_CACHE = {}

LEAGUE_AVG_BB_PCT_RECENT = 8.5
LEAGUE_AVG_OBP_RECENT = 0.315
MIN_RECENT_MATCHUP_GAMES = 15  # below this, the sample is too thin to trust at all


def fetch_recent_matchup_split(opp_team_id, pitcher_hand, year, today_iso_date):
    """Same 'baby matchup' idea as the technique this was built from: is
    THIS opponent's offense actually weak RIGHT NOW, specifically against
    pitchers throwing the same hand as tonight's starter -- not just
    'weak on average across the whole season' the way the existing
    matchup_factor works. Returns None (not a bad multiplier, an honest
    absence) if the opposing-starter data isn't available or too thin a
    sample was found, so the caller can safely fall back to the existing
    season-long factor alone."""
    cache_key = opp_team_id
    if cache_key not in _RECENT_MATCHUP_SCHEDULE_CACHE:
        try:
            # ~120 days back is enough runway to usually find 30+ games
            # against a specific handedness even in a short stretch of
            # the season, without fetching the entire year's schedule.
            from datetime import datetime, timedelta
            end = datetime.strptime(today_iso_date, "%Y-%m-%d")
            start = end - timedelta(days=120)
            sched = get(f"{API}/schedule", params={
                "teamId": opp_team_id, "startDate": start.strftime("%Y-%m-%d"),
                "endDate": today_iso_date, "sportId": 1, "hydrate": "probablePitcher",
                "gameType": "R",
            })
            games = []
            for d in sched.get("dates") or []:
                games.extend(d.get("games") or [])

            games_with_starters = []
            for g in games:
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue  # only completed games have a real, final starter
                away_id = g["teams"]["away"]["team"]["id"]
                home_id = g["teams"]["home"]["team"]["id"]
                if away_id == opp_team_id:
                    opp_pitcher = g["teams"]["home"].get("probablePitcher")
                elif home_id == opp_team_id:
                    opp_pitcher = g["teams"]["away"].get("probablePitcher")
                else:
                    continue
                if opp_pitcher and opp_pitcher.get("id"):
                    games_with_starters.append({"gamePk": g["gamePk"], "opposingPitcherId": opp_pitcher["id"]})

            hitting_data = get(f"{API}/teams/{opp_team_id}/stats", params={
                "stats": "gameLog", "group": "hitting", "season": year,
            })
            hitting_log = [{"gamePk": s.get("game", {}).get("gamePk"), **s["stat"]}
                            for s in ((hitting_data.get("stats") or [{}])[0].get("splits") or [])]

            _RECENT_MATCHUP_SCHEDULE_CACHE[cache_key] = (games_with_starters, hitting_log)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] recent matchup schedule fetch failed for team {opp_team_id}: {e}")
            _RECENT_MATCHUP_SCHEDULE_CACHE[cache_key] = ([], [])

    games_with_starters, hitting_log = _RECENT_MATCHUP_SCHEDULE_CACHE[cache_key]
    if not games_with_starters or not hitting_log:
        return None

    # Dedupe pitcher-hand lookups -- a team facing the same opposing
    # starter multiple times in the window only needs one real lookup.
    unique_pitcher_ids = {g["opposingPitcherId"] for g in games_with_starters}
    for pid in unique_pitcher_ids:
        if pid not in _PITCHER_HAND_CACHE:
            _PITCHER_HAND_CACHE[pid] = fetch_pitcher_hand(pid)

    matching = [g for g in games_with_starters if _PITCHER_HAND_CACHE.get(g["opposingPitcherId"]) == pitcher_hand]
    matching = matching[-30:]  # most recent 30, not the oldest 30
    if len(matching) < MIN_RECENT_MATCHUP_GAMES:
        return None

    target_pks = {g["gamePk"] for g in matching}
    pa_sum = bb_sum = ab_sum = hits_sum = hbp_sum = sf_sum = 0
    matched = 0
    for g in hitting_log:
        if g.get("gamePk") not in target_pks:
            continue
        pa_sum += to_num(g.get("plateAppearances")) or 0
        bb_sum += to_num(g.get("baseOnBalls")) or 0
        ab_sum += to_num(g.get("atBats")) or 0
        hits_sum += to_num(g.get("hits")) or 0
        hbp_sum += to_num(g.get("hitByPitch")) or 0
        sf_sum += to_num(g.get("sacFlies")) or 0
        matched += 1
    if matched < MIN_RECENT_MATCHUP_GAMES or pa_sum == 0:
        return None

    bb_pct = (bb_sum / pa_sum) * 100
    obp_denom = ab_sum + bb_sum + hbp_sum + sf_sum
    obp = (hits_sum + bb_sum + hbp_sum) / obp_denom if obp_denom > 0 else None
    if obp is None:
        return None

    return {"bbPct": bb_pct, "obp": obp, "gamesFound": matched, "vsHand": pitcher_hand}


_BULLPEN_FATIGUE_CACHE = {}
LEAGUE_AVG_RELIEF_IP_PER_GAME = 3.5  # roughly matches modern bullpen usage norms
MIN_BULLPEN_GAMES = 2


def fetch_bullpen_fatigue(team_id, today_iso_date, lookback_games=3):
    """Is THIS team's own bullpen (not the opponent's -- this is about
    whether tonight's manager has extra incentive to stretch the starter)
    genuinely gassed right now. Fetches the team's last few completed
    games' boxscores, sums up everyone who pitched EXCEPT that game's
    starter, and compares the average relief workload against a league
    norm. Returns None (an honest absence, not a neutral 1.0) whenever
    fewer than MIN_BULLPEN_GAMES games could actually be checked, so the
    caller can distinguish 'genuinely average' from 'couldn't find out'."""
    cache_key = (team_id, today_iso_date)
    if cache_key in _BULLPEN_FATIGUE_CACHE:
        return _BULLPEN_FATIGUE_CACHE[cache_key]

    result = None
    try:
        from datetime import datetime, timedelta
        end = datetime.strptime(today_iso_date, "%Y-%m-%d")
        start = end - timedelta(days=14)  # a couple weeks is plenty to find 3 completed games
        sched = get(f"{API}/schedule", params={
            "teamId": team_id, "startDate": start.strftime("%Y-%m-%d"),
            "endDate": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            "sportId": 1, "hydrate": "probablePitcher", "gameType": "R",
        })
        games = []
        for d in sched.get("dates") or []:
            games.extend(d.get("games") or [])
        completed = [g for g in games if g.get("status", {}).get("abstractGameState") == "Final"]
        recent = completed[-lookback_games:]

        total_relief_ip = 0.0
        games_counted = 0
        for g in recent:
            away_id = g["teams"]["away"]["team"]["id"]
            home_id = g["teams"]["home"]["team"]["id"]
            side = "away" if away_id == team_id else "home" if home_id == team_id else None
            if side is None:
                continue
            starter = g["teams"][side].get("probablePitcher")
            if not starter:
                continue
            try:
                box = get(f"{API}/game/{g['gamePk']}/boxscore")
            except Exception:  # noqa: BLE001
                continue
            team_box = (box.get("teams") or {}).get(side) or {}
            players = team_box.get("players") or {}
            relief_ip = 0.0
            for player_key, player_data in players.items():
                pid = (player_data.get("person") or {}).get("id")
                if pid is None or pid == starter.get("id"):
                    continue
                pitching_stat = ((player_data.get("stats") or {}).get("pitching") or {})
                ip = parse_ip(pitching_stat.get("inningsPitched"))
                if ip:
                    relief_ip += ip
            total_relief_ip += relief_ip
            games_counted += 1

        if games_counted >= MIN_BULLPEN_GAMES:
            avg_relief_ip = total_relief_ip / games_counted
            raw_factor = clip(avg_relief_ip / LEAGUE_AVG_RELIEF_IP_PER_GAME, 0.85, 1.25)
            dampened = 1 + (raw_factor - 1) * 0.5
            result = {"factor": round(dampened, 3), "avgRecentReliefIP": round(avg_relief_ip, 1), "gamesCounted": games_counted}
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] bullpen fatigue fetch failed for team {team_id}: {e}")
        result = None

    _BULLPEN_FATIGUE_CACHE[cache_key] = result
    return result


def fetch_pitcher_projection(pitcher_id, opp_team_id, batter_pct_map, pitcher_pct_map, year, calibration=None, outs_calibration=None, park=None, weather=None, today_iso_date=None, own_team_id=None):
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

    # Pitch-count/control profile for the outs "long leash" reasoning --
    # all pulled from the SAME season stats call above, no new API fetch.
    # numberOfPitches and battersFaced are standard MLB Stats API fields.
    num_pitches = to_num(season_stat.get("numberOfPitches"))
    batters_faced = to_num(season_stat.get("battersFaced"))
    season_bb = to_num(season_stat.get("baseOnBalls"))
    bb_pct = (season_bb / batters_faced * 100) if (season_bb is not None and batters_faced) else None
    k_pct_of_pa = (season_k / batters_faced * 100) if (season_k is not None and batters_faced) else None
    k_bb_pct = (k_pct_of_pa - bb_pct) if (k_pct_of_pa is not None and bb_pct is not None) else None
    p_per_ip = (num_pitches / season_ip) if (num_pitches and season_ip and season_ip > 0) else None

    # BABIP-against, computed directly from the same season_stat pulled
    # above -- separates what's actually within the pitcher's control
    # (K rate, walk rate) from what's largely batted-ball luck. A pitcher
    # allowing an unusually high BABIP despite good K stuff is more likely
    # a pitcher who's been unlucky than one who's actually getting hit
    # hard -- same sabermetric logic already used for team_hitting_rates
    # in build_teams.py, applied here to a pitcher's own line against.
    hits_against = to_num(season_stat.get("hits"))
    ab_against = to_num(season_stat.get("atBats"))
    hr_against = to_num(season_stat.get("homeRuns")) or 0
    sf_against = to_num(season_stat.get("sacFlies")) or 0
    babip_denom = (ab_against or 0) - (season_k or 0) - hr_against + sf_against
    babip_against = ((hits_against - hr_against) / babip_denom) if (hits_against is not None and babip_denom and babip_denom > 0) else None

    PRIOR_IP = 30
    if season_ip is not None:
        season_k9 = (((season_k or 0) + PRIOR_IP * (LEAGUE_AVG_K9 / 9)) / (season_ip + PRIOR_IP)) * 9
    else:
        season_k9 = LEAGUE_AVG_K9

    games_started = to_num(season_stat.get("gamesStarted")) or to_num(season_stat.get("gamesPlayed")) or 1
    np_per_game = (num_pitches / games_started) if (num_pitches and games_started) else None

    starts = [g for g in log if (parse_ip(g.get("inningsPitched")) or 0) > 0][-3:]

    # Conditional hit-rate pool, same technique as "when he throws 75+
    # pitches, he's over this line in 10/12 games" -- filters the FULL
    # season log (not just recent starts) to outings where he wasn't
    # pulled early, so the K/outs numbers reflect a normal workload
    # rather than being dragged down by short, unrepresentative outings.
    # Computed at MULTIPLE thresholds (not just one) so the breakdown can
    # show how the pattern holds up as workload increases -- a pitcher
    # who clears his line in 10/12 starts of 60+ pitches but only 3/4 of
    # 80+ pitch starts is telling a genuinely different story than a flat
    # single number would.
    QUALIFYING_THRESHOLDS = [60, 70, 80]
    all_starts = [g for g in log if (parse_ip(g.get("inningsPitched")) or 0) > 0]
    qualifying_starts_k_by_threshold = {}
    qualifying_starts_outs_by_threshold = {}
    for threshold in QUALIFYING_THRESHOLDS:
        qualifying_starts = [g for g in all_starts if (to_num(g.get("numberOfPitches")) or 0) >= threshold]
        qualifying_starts_k_by_threshold[threshold] = [int(to_num(g.get("strikeOuts")) or 0) for g in qualifying_starts]
        qualifying_starts_outs_by_threshold[threshold] = [int(round((parse_ip(g.get("inningsPitched")) or 0) * 3)) for g in qualifying_starts]
    # The 60-pitch tier stays the baseline "did he even have a real start"
    # floor used elsewhere (workload streak below), same reasoning as
    # before -- not a strict/loose choice, just the floor for "normal".
    MIN_QUALIFYING_PITCHES = 60

    # Role/workload stability streak, same idea as "he's thrown 88+
    # pitches in 6 straight games" -- confirms the recent-starts sample
    # actually reflects a stable, full-workload role right now, not a
    # pitcher who's been yanked early or is working back from something.
    # Counts consecutive starts from the MOST RECENT backward, stopping
    # at the first one that falls short of the same threshold used above.
    workload_streak = 0
    for g in reversed(all_starts):
        if (to_num(g.get("numberOfPitches")) or 0) >= MIN_QUALIFYING_PITCHES:
            workload_streak += 1
        else:
            break
    most_recent_pitch_count = to_num(all_starts[-1].get("numberOfPitches")) if all_starts else None

    recent_k9 = None
    recent_starts_log = []
    rolling_k_bb_pct = None
    if starts:
        ip_sum = sum(parse_ip(g.get("inningsPitched")) or 0 for g in starts)
        k_sum = sum(to_num(g.get("strikeOuts")) or 0 for g in starts)
        RECENT_PRIOR_IP = 6
        if ip_sum > 0:
            recent_k9 = (((k_sum or 0) + RECENT_PRIOR_IP * (season_k9 / 9)) / (ip_sum + RECENT_PRIOR_IP)) * 9
        recent_starts_log = [{
            "k": int(to_num(g.get("strikeOuts")) or 0), "ip": parse_ip(g.get("inningsPitched")),
            "bb": int(to_num(g.get("baseOnBalls")) or 0), "battersFaced": to_num(g.get("battersFaced")),
        } for g in starts]

        # Rolling K-BB% over just these last (up to 3) starts -- a distinct,
        # more volatile signal from the season-long K-BB% already used for
        # the outs projection. Tells us if command is locked in RIGHT NOW,
        # not across the whole season.
        rolling_bf = sum(g["battersFaced"] or 0 for g in recent_starts_log)
        rolling_bb = sum(g["bb"] for g in recent_starts_log)
        if rolling_bf and rolling_bf > 0:
            rolling_k_pct = (k_sum / rolling_bf) * 100
            rolling_bb_pct = (rolling_bb / rolling_bf) * 100
            rolling_k_bb_pct = rolling_k_pct - rolling_bb_pct

    stuff_factor = 1.0
    p_row = pitcher_pct_map.get(str(pitcher_id))
    if p_row:
        # k_percent/whiff_percent here are 0-100 PERCENTILE RANKS from
        # Savant's percentile-rankings endpoint (confirmed elsewhere in
        # this codebase, e.g. the matchup card's "FB Velo %ile"/"xERA
        # %ile" labels) -- NOT raw K%/whiff% rates. The original
        # k_pct/22.0-style division was designed for a raw rate around
        # 20-27%, so almost any pitcher at or above roughly the 30th
        # percentile was silently clipped to the exact same 1.35
        # ceiling, destroying any real differentiation between a merely
        # good pitcher and a genuinely elite one. percentile_to_factor
        # instead maps the percentile itself to a factor centered at 1.0
        # for a league-average (50th percentile) pitcher.
        k_pct = to_num(p_row.get("k_percent"))
        whiff_pct = to_num(p_row.get("whiff_percent"))
        parts = []
        if k_pct is not None:
            parts.append(percentile_to_factor(k_pct))
        if whiff_pct is not None:
            parts.append(percentile_to_factor(whiff_pct))
        if parts:
            prod = 1
            for x in parts:
                prod *= x
            stuff_factor = clip(prod ** (1 / len(parts)), 0.75, 1.35)

    base_k9 = (0.6 * season_k9 + 0.4 * recent_k9) if recent_k9 is not None else season_k9

    opp_k = fetch_roster_k_percent(opp_team_id, year)
    season_matchup_factor = clip(opp_k / LEAGUE_AVG_K_PCT, 0.75, 1.3) if opp_k is not None else 1.0

    # Recent-form refinement: is this opponent's offense actually weak
    # RIGHT NOW against a same-handed pitcher, not just weak on average
    # across the whole season. Same "individually bounded, then geometric
    # mean" blending already used for stuff_factor/matchup_factor below,
    # rather than letting this stack multiplicatively on top of the
    # season-long number. Falls back to the season-long factor alone
    # (recent_form_factor = 1.0, a genuine no-op) whenever the rolling
    # window can't find enough same-handed games to trust.
    recent_matchup_applied = None
    recent_form_factor = 1.0
    hand_for_matchup = fetch_pitcher_hand(pitcher_id)
    if hand_for_matchup and today_iso_date:
        recent_split = fetch_recent_matchup_split(opp_team_id, hand_for_matchup, year, today_iso_date)
        if recent_split:
            bb_factor = clip(LEAGUE_AVG_BB_PCT_RECENT / recent_split["bbPct"], 0.85, 1.15) if recent_split["bbPct"] else 1.0
            obp_factor = clip(LEAGUE_AVG_OBP_RECENT / recent_split["obp"], 0.85, 1.15) if recent_split["obp"] else 1.0
            recent_form_factor = (bb_factor * obp_factor) ** 0.5
            recent_matchup_applied = {"factor": round(recent_form_factor, 3), "gamesFound": recent_split["gamesFound"], "vsHand": hand_for_matchup}

    matchup_factor = clip((season_matchup_factor * recent_form_factor) ** 0.5, 0.75, 1.3) if recent_matchup_applied else season_matchup_factor

    # stuff_factor and matchup_factor are each individually bounded, but
    # straight-multiplying them let a pitcher with strong Savant numbers AND
    # a favorable matchup compound to as much as ~1.75x combined -- well
    # beyond what either factor was designed to represent alone. Blending
    # them as a geometric mean (the same technique stuff_factor already
    # uses internally on its own sub-parts) keeps each factor's real signal
    # without letting them stack multiplicatively.
    combined_factor = (stuff_factor * matchup_factor) ** 0.5
    final_k9 = base_k9 * combined_factor

    # Environment factor: reuses the exact same park/weather data HR
    # already fetches, but applied far more conservatively here. A
    # pitcher-friendly park may embolden a pitcher to attack the zone, and
    # cold/dense air can suppress hard, clean contact -- both real,
    # discussed effects, but neither has anywhere near as direct a
    # physical relationship to strikeouts as weather does to a batted
    # ball's flight (that's HR's domain). Bounds here are half of HR's.
    park_hr_factor = (park or {}).get("hr", 1.0)
    park_k_factor = clip(1.0 + (1.0 - park_hr_factor) * 0.1, 0.96, 1.04)
    weather_k_factor = 1.0
    if weather and not weather.get("climateControlled") and weather.get("tempF") is not None:
        t = weather["tempF"]
        # Colder/denser air modestly favors the pitcher; hot/thin air modestly favors the batter.
        weather_k_factor = clip(1.0 + (65 - t) * 0.0015, 0.94, 1.06)
    environment_factor = clip((park_k_factor * weather_k_factor) ** 0.5, 0.95, 1.05)

    expected_ip = clip((season_ip / games_started) if (season_ip and games_started) else 5.2, 3.5, 6.7)
    projected_k = final_k9 * environment_factor * expected_ip / 9
    # Absolute safety ceiling on the PROJECTED MEAN specifically -- not a
    # best-case outcome; the Poisson math used downstream already accounts
    # for a great day happening above the mean. A real elite ace at ~11-12
    # K/9 with a strong 6.5 IP start averages around 8 strikeouts, not 12,
    # so this is still generous headroom rather than a tight leash.
    projected_k = clip(projected_k, 1.0, 9.5)

    # Pitching outs: reuses the same season/recent IP data already fetched
    # above for expected_ip, rather than a separate model. A favorable
    # matchup (fewer baserunners, cleaner innings) plausibly correlates
    # with a deeper outing too, but applied at only 40% strength -- full
    # strength here would repeat the exact compounding mistake found and
    # fixed in the strikeout projection above.
    season_ip_per_start = (season_ip / games_started) if (season_ip and games_started) else None
    recent_ip_avg = (ip_sum / len(starts)) if starts and ip_sum > 0 else None
    if season_ip_per_start is not None and recent_ip_avg is not None:
        base_ip = 0.6 * season_ip_per_start + 0.4 * recent_ip_avg
    elif season_ip_per_start is not None:
        base_ip = season_ip_per_start
    else:
        base_ip = 5.2
    dampened_matchup = 1 + (matchup_factor - 1) * 0.4
    # "Long leash" efficiency profile: a pitcher who works quick, efficient
    # innings (low pitches/inning) and pounds the zone (high K-BB%) tends to
    # stay in games longer before a manager pulls them, independent of the
    # day's specific matchup. Each piece is individually bounded, then
    # blended via geometric mean (not multiplied straight through) and
    # dampened AGAIN on top of that -- two layers of caution, learned
    # directly from the earlier K-projection compounding bug. Missing data
    # (rookies, incomplete season logs) falls back to a neutral 1.0 rather
    # than skewing the projection off partial information.
    p_per_ip_factor = clip(16.5 / p_per_ip, 0.85, 1.15) if p_per_ip else 1.0
    k_bb_factor = clip(1.0 + (k_bb_pct - 15.0) / 100.0, 0.85, 1.15) if k_bb_pct is not None else 1.0
    raw_efficiency = (p_per_ip_factor * k_bb_factor) ** 0.5
    dampened_efficiency = 1 + (raw_efficiency - 1) * 0.5

    # Bullpen fatigue: a manager whose relievers have been heavily used
    # the last few games has a real, concrete reason to stretch tonight's
    # starter deeper -- not a vague vibe, an actual incentive. Already
    # dampened once inside fetch_bullpen_fatigue itself; this is a
    # SEPARATE signal from the efficiency/matchup factors above (this
    # pitcher's own team's situation, not his own stuff or tonight's
    # opponent), so it's applied as its own independent multiplier rather
    # than folded into either existing blend.
    bullpen_fatigue = fetch_bullpen_fatigue(own_team_id, today_iso_date) if (own_team_id and today_iso_date) else None
    bullpen_factor = bullpen_fatigue["factor"] if bullpen_fatigue else 1.0

    projected_ip_outs = clip(base_ip * dampened_matchup * dampened_efficiency * bullpen_factor, 3.0, 7.0)
    projected_outs = round(projected_ip_outs * 3, 1)

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

    outs_calibration_applied = None
    if outs_calibration:
        outs_tier_cal = outs_calibration.get(confidence, {})
        outs_bias = outs_tier_cal.get("bias", 0.0)
        if outs_tier_cal.get("status") == "active" and outs_bias != 0.0:
            projected_outs = max(3.0, round(projected_outs + outs_bias, 1))
            outs_calibration_applied = outs_bias

    return {
        "hand": hand_for_matchup,
        "seasonK9": season_k9, "recentK9": recent_k9, "oppK": opp_k,
        "matchupFactor": matchup_factor, "stuffFactor": stuff_factor,
        "recentMatchupApplied": recent_matchup_applied,
        "expectedIP": expected_ip, "projectedK": projected_k, "confidence": confidence,
        "projectedOuts": projected_outs,
        "npPerGame": np_per_game, "pitchesPerIP": p_per_ip,
        "bbPct": bb_pct, "kBBPct": k_bb_pct, "rollingKBBPct": rolling_k_bb_pct,
        "parkFactor": park_hr_factor, "weatherFactor": weather_k_factor,
        "calibrationApplied": calibration_applied,
        "outsCalibrationApplied": outs_calibration_applied,
        "bullpenFatigue": bullpen_fatigue,
        "recentStartsLog": recent_starts_log, "veloTrend": velo_trend,
        "qualifyingStartsKByThreshold": qualifying_starts_k_by_threshold, "qualifyingStartsOutsByThreshold": qualifying_starts_outs_by_threshold,
        "babipAgainst": babip_against,
        "workloadStreak": workload_streak, "mostRecentPitchCount": most_recent_pitch_count,
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
    rm = p.get("recentMatchupApplied")
    if rm and rm["factor"] >= 1.08:
        positives.append(f"an opponent that's been genuinely weak vs {rm['vsHand']}HP lately, not just weak on paper (their last {rm['gamesFound']} games specifically vs this handedness)")
    elif rm and rm["factor"] <= 0.92:
        negatives.append(f"an opponent that's actually been hitting {rm['vsHand']}HP well lately (their last {rm['gamesFound']} games specifically vs this handedness)")
    if p["expectedIP"] > 6.0:
        positives.append("a long-innings workload expectation")
    elif p["expectedIP"] < 4.5:
        negatives.append("a short-outing workload expectation")
    vt = p.get("veloTrend")
    if vt and vt.get("delta", 0) < -1.2:
        negatives.append("declining fastball velocity")
    elif vt and vt.get("delta", 0) > 1.2:
        positives.append("fastball velocity trending up")
    if p.get("rollingKBBPct") is not None:
        if p["rollingKBBPct"] >= 22.0:
            positives.append(f"locked-in command over their last starts (K-BB% of {p['rollingKBBPct']:.1f}%)")
        elif p["rollingKBBPct"] < 5.0:
            negatives.append(f"shaky recent command (K-BB% of only {p['rollingKBBPct']:.1f}%)")
    if p.get("parkFactor") is not None and p["parkFactor"] < 0.92:
        positives.append("pitching in a park that favors attacking the zone")
    if p.get("weatherFactor") is not None:
        if p["weatherFactor"] > 1.02:
            positives.append("cold weather likely suppressing hard contact")
        elif p["weatherFactor"] < 0.98:
            negatives.append("warm weather that can favor the hitter")
    # Skill vs. luck: BABIP-against is mostly variance, not something a
    # pitcher directly controls the way K rate or walk rate is. Framed
    # here as context on the season line itself, not as a boost/tempered
    # factor the way the others are, since a bad-luck BABIP doesn't
    # actually predict tonight's outcome -- it just explains why the
    # season numbers above might look worse (or better) than the
    # pitcher's actual underlying stuff.
    babip_note = ""
    if p.get("babipAgainst") is not None:
        if p["babipAgainst"] >= 0.320:
            babip_note = f" His .{round(p['babipAgainst']*1000):03d} BABIP-against is well above the league norm, which often reflects bad luck on balls in play more than actually getting hit hard."
        elif p["babipAgainst"] <= 0.260:
            babip_note = f" His .{round(p['babipAgainst']*1000):03d} BABIP-against is well below the league norm, which can mean a real soft-contact profile -- or a stretch of good luck that's due to normalize."

    base = ("Elite strikeout stuff this season" if p["seasonK9"] > 10 else
            "Strong strikeout stuff this season" if p["seasonK9"] > 8.5 else
            "Average strikeout stuff this season" if p["seasonK9"] > 7 else
            "Below-average strikeout stuff this season")
    sentence = base
    if positives:
        sentence += f", boosted by {join_list(positives)}"
    if negatives:
        sentence += ("; " if positives else ", ") + f"tempered by {join_list(negatives)}"
    return sentence + "." + babip_note


def outs_reason_text(p):
    """Reasoning specifically for the pitching-outs 'long leash' projection.
    Every factor referenced here is something that actually feeds the
    projection math above -- pitches/game, pitches/inning, BB%, K-BB%,
    workload (IP/GS), and now bullpen fatigue (the pitcher's own team's
    recent relief usage). First-pitch strike % and 3rd-time-through-the-
    order splits still aren't included -- no verified data source for
    either from this environment."""
    positives, negatives = [], []

    if p.get("npPerGame") is not None:
        if p["npPerGame"] >= 95:
            positives.append(f"a track record of a long leash ({p['npPerGame']:.0f} pitches/game this season)")
        elif p["npPerGame"] <= 80:
            negatives.append(f"a shorter historical leash ({p['npPerGame']:.0f} pitches/game this season)")

    if p["expectedIP"] >= 6.0:
        positives.append(f"getting deep into games ({p['expectedIP']:.1f} IP/start)")
    elif p["expectedIP"] < 4.5:
        negatives.append(f"a short-outing workload pattern ({p['expectedIP']:.1f} IP/start)")

    if p.get("pitchesPerIP") is not None:
        if p["pitchesPerIP"] <= 15.0:
            positives.append(f"efficient innings ({p['pitchesPerIP']:.1f} pitches/inning)")
        elif p["pitchesPerIP"] >= 18.0:
            negatives.append(f"laboring through innings ({p['pitchesPerIP']:.1f} pitches/inning)")

    if p.get("kBBPct") is not None:
        if p["kBBPct"] >= 20.0:
            positives.append(f"elite command (K-BB% of {p['kBBPct']:.1f}%)")
        elif p["kBBPct"] < 8.0:
            negatives.append(f"shaky command (K-BB% of only {p['kBBPct']:.1f}%)")
    if p.get("bbPct") is not None and p["bbPct"] >= 9.0:
        negatives.append(f"a walk rate that tends to spike pitch counts ({p['bbPct']:.1f}% BB)")

    bf = p.get("bullpenFatigue")
    if bf and bf["factor"] >= 1.06:
        positives.append(f"a bullpen that's been leaned on hard lately ({bf['avgRecentReliefIP']:.1f} relief innings/game over their last {bf['gamesCounted']}), giving the manager real reason to stretch him out")
    elif bf and bf["factor"] <= 0.96:
        negatives.append(f"a well-rested bullpen right now ({bf['avgRecentReliefIP']:.1f} relief innings/game over their last {bf['gamesCounted']}), which removes some of the incentive to push him deep")

    base = (f"Projected for {p['projectedOuts']:.1f} outs ({p['expectedIP']:.1f} innings)")
    sentence = base
    if positives:
        sentence += f", supported by {join_list(positives)}"
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
    outs_calibration = load_outs_calibration()

    batter_pct_map = fetch_savant_percentiles("batter", year)
    pitcher_pct_map = fetch_savant_percentiles("pitcher", year)

    jobs = []
    for g in games:
        home_abbr = g["teams"]["home"]["team"]["abbreviation"]
        park = PARKS.get(home_abbr)
        weather = None
        if park and park["roof"] == "open":
            try:
                weather = fetch_weather(park["lat"], park["lon"], g["gameDate"])
            except Exception:  # noqa: BLE001
                weather = None
        away_p = g["teams"]["away"].get("probablePitcher")
        home_p = g["teams"]["home"].get("probablePitcher")
        if away_p:
            jobs.append({"pitcher": away_p, "team": g["teams"]["away"]["team"], "opp": g["teams"]["home"]["team"], "gamePk": g["gamePk"], "park": park, "weather": weather})
        if home_p:
            jobs.append({"pitcher": home_p, "team": g["teams"]["home"]["team"], "opp": g["teams"]["away"]["team"], "gamePk": g["gamePk"], "park": park, "weather": weather})

    entries = []
    for job in jobs:
        proj = fetch_pitcher_projection(job["pitcher"]["id"], job["opp"]["id"], batter_pct_map, pitcher_pct_map, year, calibration, outs_calibration, job.get("park"), job.get("weather"), today_iso_date=date, own_team_id=job["team"]["id"])
        if not proj:
            continue
        p = {
            "id": job["pitcher"]["id"], "gamePk": job["gamePk"], "name": job["pitcher"]["fullName"],
            "team": job["team"]["abbreviation"], "opp": job["opp"]["abbreviation"], "oppTeamId": job["opp"]["id"],
            **proj,
        }
        p["reason"] = reason_text(p)
        p["outsReason"] = outs_reason_text(p)
        entries.append({
            "gamePk": p["gamePk"], "playerId": p["id"], "name": p["name"],
            "team": p["team"], "opp": p["opp"], "oppTeamId": p["oppTeamId"],
            "hand": p["hand"], "seasonRecord": p["seasonRecord"], "era": p["era"],
            "seasonK9": p["seasonK9"], "recentK9": p["recentK9"], "oppK": p["oppK"],
            "matchupFactor": p["matchupFactor"], "stuffFactor": p["stuffFactor"],
            "recentMatchupApplied": p.get("recentMatchupApplied"),
            "expectedIP": p["expectedIP"], "projectedK": p["projectedK"],
            "confidence": p["confidence"], "reason": p["reason"],
            "calibrationApplied": p.get("calibrationApplied"),
            "recentStartsLog": p["recentStartsLog"], "veloTrend": p["veloTrend"],
            "qualifyingStartsKByThreshold": p["qualifyingStartsKByThreshold"], "qualifyingStartsOutsByThreshold": p["qualifyingStartsOutsByThreshold"],
            "babipAgainst": p.get("babipAgainst"),
            "workloadStreak": p.get("workloadStreak"), "mostRecentPitchCount": p.get("mostRecentPitchCount"),
            "marketThreshold": None, "marketProb": None, "modelProb": None, "edge": None,
            "predictionLine": None, "predictionOutsLine": None,
            "graded": False, "actualK": None, "hit": None,
            "projectedOuts": p["projectedOuts"], "outsReason": p["outsReason"],
            "npPerGame": p.get("npPerGame"), "pitchesPerIP": p.get("pitchesPerIP"),
            "bbPct": p.get("bbPct"), "kBBPct": p.get("kBBPct"),
            "rollingKBBPct": p.get("rollingKBBPct"), "parkFactor": p.get("parkFactor"),
            "weatherFactor": p.get("weatherFactor"),
            "outsCalibrationApplied": p.get("outsCalibrationApplied"),
            "bullpenFatigue": p.get("bullpenFatigue"),
            "outsMarketThreshold": None, "outsMarketProb": None, "outsModelProb": None, "outsEdge": None,
            "actualOuts": None, "outsHit": None,
        })
        print(f"  {p['name']} ({p['team']} vs {p['opp']}): {p['projectedK']:.1f} proj K, {p['projectedOuts']:.1f} proj outs")

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
