"""
Homepage highlights: the Statcast Spotlight and Today's Top Matchup
widgets shown at the top of the HR (landing) view.

Deliberately NOT a new prediction or ranking system -- both widgets just
pick which of today's ALREADY-COMPUTED picks to feature, using data the
HR/K/Teams boards already produced. No new heuristics, no new model.
"""

from common import fetch_player_home_runs


def build_statcast_spotlight(hr_data, year):
    """Today's #1 HR pick's hardest-hit home run this season, with real
    exit velocity/launch angle/distance. Ties the flashy per-pitch
    numbers back to an actual today's-pick claim ("here's proof this
    specific player can do this") rather than a disconnected highlight
    -- e.g. showing the single longest homer by anyone all year, which
    would look impressive but say nothing about today's board."""
    entries = hr_data.get("entries") or []
    if not entries:
        return None
    top_pick = max(entries, key=lambda e: e.get("heuristicProb") or 0)
    player_id = top_pick.get("playerId")
    if not player_id:
        return None
    home_runs = fetch_player_home_runs(player_id, year)
    if not home_runs:
        # A real, honest outcome -- e.g. a rookie with 0 HRs yet this
        # season, or the per-event fetch came back empty/failed. No
        # spotlight today rather than a fabricated one.
        return None
    hardest = max(home_runs, key=lambda hr: hr["exitVelo"])
    return {
        "name": top_pick.get("name"), "team": top_pick.get("team"),
        "heuristicProb": top_pick.get("heuristicProb"),
        "exitVelo": hardest["exitVelo"], "launchAngle": hardest["launchAngle"],
        "distance": hardest["distance"], "hrDate": hardest["date"],
        "seasonHRCount": len(home_runs),
    }


def build_top_matchup(hr_data, ko_data, teams_data):
    """Today's featured pitcher-vs-batter matchup.

    Primary pick: the highest-edge, liquidity-verified K entry (see
    edgeEligible in fetch_kalshi.py -- same reasoning applies here:
    edge only means something against a real, two-sided market), paired
    with the opposing batter with the most notable history against him
    (highest OPS among the pitcher's own matchup table, which is already
    sample-size-floored at 3+ career at-bats in fetch_vs_pitcher -- not
    literally "most plate appearances", the single most threatening
    qualifying matchup).

    Fallback, for days with no edgeEligible K entries at all (e.g.
    Kalshi has no liquid strikeout markets today): highest projected-K
    pitcher, paired with the opposing team's best HR threat from today's
    HR board instead of a specific historical matchup -- still a real,
    defensible pairing, just a different kind of "top" when there's no
    trustworthy edge to rank by."""
    ko_entries = (ko_data or {}).get("entries") or []
    if not ko_entries:
        return None

    eligible = [e for e in ko_entries if e.get("edge") is not None and e.get("edgeEligible")]
    used_fallback = not eligible
    pitcher = max(eligible, key=lambda e: e["edge"]) if eligible \
        else max(ko_entries, key=lambda e: e.get("projectedK") or 0)

    matchup = {
        "pitcherName": pitcher.get("name"), "pitcherTeam": pitcher.get("team"),
        "opp": pitcher.get("opp"), "projectedK": pitcher.get("projectedK"),
        "edge": pitcher.get("edge") if not used_fallback else None,
        "usedFallback": used_fallback,
    }

    batter = None
    if not used_fallback and teams_data:
        for m in (teams_data.get("matchups") or []):
            if m.get("pitcherId") == pitcher.get("playerId"):
                table = m.get("matchupTable") or []
                if table:
                    batter = table[0]  # already OPS-sorted, already 3+ AB floored
                break

    if batter:
        matchup["batter"] = {
            "name": batter.get("name"), "atBats": batter.get("atBats"),
            "hits": batter.get("hits"), "homeRuns": batter.get("homeRuns"),
            "ops": batter.get("ops"),
        }
        matchup["batterKind"] = "history"
    else:
        # No specific historical matchup found (or intentionally using
        # the fallback path) -- feature the opposing team's best current
        # HR threat instead.
        hr_entries = [e for e in ((hr_data or {}).get("entries") or []) if e.get("team") == pitcher.get("opp")]
        best_threat = max(hr_entries, key=lambda e: e.get("heuristicProb") or 0) if hr_entries else None
        if best_threat:
            matchup["batter"] = {
                "name": best_threat.get("name"),
                "heuristicProb": best_threat.get("heuristicProb"),
            }
            matchup["batterKind"] = "hrThreat"
        else:
            matchup["batter"] = None
            matchup["batterKind"] = None

    return matchup


def build(hr_data, ko_data, teams_data, year):
    return {
        "statcastSpotlight": build_statcast_spotlight(hr_data, year),
        "topMatchup": build_top_matchup(hr_data, ko_data, teams_data),
    }
