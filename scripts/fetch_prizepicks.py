"""
Fetch PrizePicks MLB player-prop projections via their public (undocumented)
partner-api endpoint. No API key, no login -- this is the same endpoint used
by numerous public hobbyist projects (see e.g. github.com/mada949/PrizePicks-API).

Important caveats, stated plainly:
  - This is NOT an official developer API. PrizePicks could change the
    response shape, rate-limit, or block this endpoint at any time without
    notice. If this script starts failing, that's the most likely reason.
  - League IDs are looked up dynamically by name (not hardcoded), since
    PrizePicks is known to renumber leagues over time.
  - Exact stat_type strings ("Home Runs" vs "Homers" etc.) can't be verified
    live from this environment, so matching is case-insensitive/substring
    based, and every run logs the full set of stat types actually seen --
    if matching comes up empty, check that log first.
"""

from common import get, norm_name, to_num

PP_BASE = "https://partner-api.prizepicks.com"

# Substrings we look for in PrizePicks' stat_type field (case-insensitive).
# Update these if the debug log shows PrizePicks phrasing it differently.
HR_STAT_MATCHES = ["home run"]
K_STAT_MATCHES = ["strikeout"]


def fetch_leagues():
    try:
        data = get(f"{PP_BASE}/leagues")
        return data.get("data") or []
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] PrizePicks /leagues fetch failed: {e}")
        return []


def find_mlb_league_id(leagues):
    for league in leagues:
        name = (league.get("attributes") or {}).get("name", "")
        if name.strip().upper() == "MLB":
            return league["id"]
    # fallback: substring match, in case of a naming variant like "MLB (Live)"
    for league in leagues:
        name = (league.get("attributes") or {}).get("name", "")
        if "mlb" in name.lower():
            print(f"  [info] using MLB-ish league match: {name!r} (id={league['id']})")
            return league["id"]
    return None


def fetch_projections(league_id):
    try:
        data = get(f"{PP_BASE}/projections", params={"league_id": league_id, "per_page": 1000})
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] PrizePicks /projections fetch failed: {e}")
        return []

    projections = data.get("data") or []
    included = data.get("included") or []

    players = {}
    for item in included:
        if item.get("type") == "new_player":
            attrs = item.get("attributes") or {}
            name = attrs.get("name") or attrs.get("display_name")
            team = attrs.get("team") or attrs.get("team_name")
            if name:
                players[item["id"]] = {"name": name, "team": team}

    records = []
    stat_types_seen = set()
    for p in projections:
        attrs = p.get("attributes") or {}
        stat_type = attrs.get("stat_type") or ""
        stat_types_seen.add(stat_type)

        rel = p.get("relationships") or {}
        player_ref = ((rel.get("new_player") or {}).get("data") or {})
        player_id = player_ref.get("id")
        player = players.get(player_id)
        if not player:
            continue

        line = to_num(attrs.get("line_score"))
        if line is None:
            continue

        records.append({
            "name": player["name"],
            "team": player.get("team"),
            "statType": stat_type,
            "line": line,
            "startTime": attrs.get("start_time"),
            "status": attrs.get("status"),
        })

    print(f"  PrizePicks: {len(projections)} raw projection(s), {len(records)} matched to a player")
    print(f"  PrizePicks: stat types seen: {sorted(stat_types_seen)}")
    return records


def fetch_mlb_props():
    """Returns (hr_records, k_records) -- each a list of
    {name, team, line, startTime, status}."""
    leagues = fetch_leagues()
    if not leagues:
        return [], []
    league_id = find_mlb_league_id(leagues)
    if league_id is None:
        print("  [warn] Could not find an MLB league in PrizePicks' /leagues response")
        return [], []

    all_records = fetch_projections(league_id)
    hr_records = [r for r in all_records if any(m in r["statType"].lower() for m in HR_STAT_MATCHES)]
    k_records = [r for r in all_records if any(m in r["statType"].lower() for m in K_STAT_MATCHES)]
    return hr_records, k_records


def merge_hr(hr_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in hr_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            e["prizePicksHRLine"] = hit["line"]
            matched += 1
    print(f"  PrizePicks HR: matched {matched}/{len(hr_data['entries'])} entries")
    return hr_data


def merge_ko(ko_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in ko_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            e["prizePicksKLine"] = hit["line"]
            matched += 1
    print(f"  PrizePicks K: matched {matched}/{len(ko_data['entries'])} entries")
    return ko_data
