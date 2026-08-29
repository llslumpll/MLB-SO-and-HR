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


def refine_ko_with_prizepicks(ko_data, kalshi_threshold_map):
    """For any entry with a PrizePicks line, make marketThreshold/modelProb/
    marketProb/edge all correctly correspond to PrizePicks' actual line --
    not whatever threshold Kalshi's own 'closest to 50%' pick happened to
    land on, which could be a different number entirely and would make the
    displayed edge silently answer the wrong question.

    Entries with no PrizePicks line are left exactly as Kalshi's own merge
    already set them.
    """
    import fetch_kalshi

    refined = 0
    no_kalshi_at_that_line = 0
    for e in ko_data["entries"]:
        pp_line = e.get("prizePicksKLine")
        if pp_line is None:
            continue

        # PrizePicks lines are "over X.5" style -> clearing them means
        # actual_K >= floor(X.5) + 1. Handles integer lines the same way
        # (strictly more than the line).
        implied_threshold = int(pp_line // 1) + 1

        model_prob = fetch_kalshi.poisson_prob_at_least(implied_threshold, e["projectedK"])
        e["marketThreshold"] = implied_threshold
        e["modelProb"] = model_prob
        e["prizePicksCall"] = "OVER" if model_prob >= 0.5 else "UNDER"

        kalshi_price = (kalshi_threshold_map.get(norm_name(e["name"])) or {}).get(implied_threshold)
        if kalshi_price is not None:
            if e.get("openingProb") is None:
                e["openingProb"] = kalshi_price
            e["priceDelta"] = kalshi_price - e["openingProb"]
            e["marketProb"] = kalshi_price
            e["edge"] = model_prob - kalshi_price
        else:
            # Kalshi doesn't have a market at this exact threshold -- showing
            # no edge is more honest than showing one computed against a
            # different number.
            e["marketProb"] = None
            e["edge"] = None
            e["openingProb"] = None
            e["priceDelta"] = None
            no_kalshi_at_that_line += 1
        refined += 1

    print(f"  PrizePicks/Kalshi threshold match: refined {refined} entries "
          f"({no_kalshi_at_that_line} had no Kalshi price at PrizePicks' exact line)")
    if refined:
        print("  DEBUG: sample refined K entries (name, projectedK, PP line, implied threshold, modelProb, call):")
        shown = 0
        for e in ko_data["entries"]:
            if e.get("prizePicksKLine") is not None and shown < 15:
                print(f"    {e['name']!r}: projectedK={e['projectedK']:.2f}, ppLine={e['prizePicksKLine']}, "
                      f"threshold={e['marketThreshold']}, modelProb={e['modelProb']:.3f}, call={e.get('prizePicksCall')}")
                shown += 1
    return ko_data
