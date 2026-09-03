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
# Must specifically say "pitcher" -- PrizePicks also offers "Hitter Strikeouts"
# (how often a batter strikes out, an unrelated stat), and a bare "strikeout"
# substring match was catching both and letting whichever loaded second
# silently overwrite the correct pitcher line.
K_STAT_MATCHES = ["pitcher strikeout"]
# Best-guess matches for a "Pitching Outs" prop -- worth flagging honestly:
# every stat_types_seen log from today's earlier debugging never showed
# anything outs-related, only the list below. This may mean PrizePicks
# doesn't currently offer this specific prop. The odds_types_seen /
# stat_types_seen diagnostic already prints every real stat type on each
# run, so the very next run will confirm one way or the other.
OUTS_STAT_MATCHES = ["pitching outs", "pitcher outs", "outs recorded"]
# Exact match, not substring -- "Hits" is a genuine substring of both
# "Hits Allowed" (a pitcher stat, wrong side entirely) and "Hits+Runs+RBIs"
# (a combo stat), both real PrizePicks stat types confirmed in earlier
# diagnostic logs. The same class of collision already found and fixed
# once for Strikeouts (Pitcher vs Hitter) -- this one is even more
# dangerous since a substring match here would be silently, completely
# wrong rather than just imprecise.
HITS_STAT_EXACT = "hits"
TB_STAT_EXACT = "total bases"


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
    odds_types_seen = set()
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

        # PrizePicks posts alternate "Goblin" (easier) and "Demon" (harder)
        # lines alongside the standard one for the same stat -- without
        # filtering these out, picking "whichever projection came last in
        # the response" could silently grab a goblin/demon line instead of
        # the real one. Field name guessed defensively since it can't be
        # verified against live data from here; odds_types_seen below will
        # immediately reveal the real values if this guess is wrong.
        odds_type = (attrs.get("odds_type") or attrs.get("type") or "standard")
        odds_type = str(odds_type).strip().lower()
        odds_types_seen.add(odds_type)

        records.append({
            "name": player["name"],
            "team": player.get("team"),
            "statType": stat_type,
            "line": line,
            "oddsType": odds_type,
            "startTime": attrs.get("start_time"),
            "status": attrs.get("status"),
        })

    print(f"  PrizePicks: {len(projections)} raw projection(s), {len(records)} matched to a player")
    print(f"  PrizePicks: stat types seen: {sorted(stat_types_seen)}")
    print(f"  PrizePicks: odds types seen: {sorted(odds_types_seen)}")
    return records


def fetch_mlb_props():
    """Returns (hr_records, k_records, outs_records, hits_records,
    tb_records) -- each a list of {name, team, line, startTime, status}.
    Standard lines only -- Goblin and Demon alternate lines are excluded."""
    leagues = fetch_leagues()
    if not leagues:
        return [], [], [], [], []
    league_id = find_mlb_league_id(leagues)
    if league_id is None:
        print("  [warn] Could not find an MLB league in PrizePicks' /leagues response")
        return [], [], [], [], []

    all_records = fetch_projections(league_id)
    before = len(all_records)
    # "standard" here also covers the case where odds_type couldn't be
    # determined at all -- better to keep an unlabeled line than silently
    # drop everything if the field name guess above turns out wrong.
    all_records = [r for r in all_records if r["oddsType"] in ("standard", "")]
    dropped = before - len(all_records)
    if dropped:
        print(f"  PrizePicks: excluded {dropped} goblin/demon alternate line(s), kept standard only")

    hr_records = [r for r in all_records if any(m in r["statType"].lower() for m in HR_STAT_MATCHES)]
    k_records = [r for r in all_records if any(m in r["statType"].lower() for m in K_STAT_MATCHES)]
    outs_records = [r for r in all_records if any(m in r["statType"].lower() for m in OUTS_STAT_MATCHES)]
    if not outs_records:
        print("  PrizePicks: no Pitching Outs stat type matched -- check odds types seen above for the real name")
    hits_records = [r for r in all_records if r["statType"].strip().lower() == HITS_STAT_EXACT]
    tb_records = [r for r in all_records if r["statType"].strip().lower() == TB_STAT_EXACT]
    return hr_records, k_records, outs_records, hits_records, tb_records


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


def merge_outs(ko_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in ko_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            line = hit["line"]
            if e.get("prizePicksOutsOpeningLine") is None:
                e["prizePicksOutsOpeningLine"] = line
            e["prizePicksOutsLineDelta"] = line - e["prizePicksOutsOpeningLine"]
            e["prizePicksOutsLine"] = line
            matched += 1
    print(f"  PrizePicks Outs: matched {matched}/{len(ko_data['entries'])} entries")
    ko_data["_ppOutsMatched"] = matched
    ko_data["_ppOutsTotal"] = len(ko_data["entries"])
    return ko_data


def refine_outs_with_prizepicks(ko_data):
    """Computes the model's own OVER/UNDER call for pitching outs against
    PrizePicks' line. No Kalshi comparison here -- there's no confirmed
    Kalshi market for this specific stat, so outsEdge/outsMarketProb stay
    None (honest absence) rather than comparing against nothing.

    Frozen the first time it's computed, same reasoning as the K version --
    otherwise the OVER/UNDER call would silently drift every 10 minutes as
    the line moved, instead of representing one stable prediction."""
    import fetch_kalshi

    refined = 0
    for e in ko_data["entries"]:
        pp_line = e.get("prizePicksOutsLine")
        if pp_line is None:
            continue
        if e.get("outsCall") is not None:
            continue
        implied_threshold = int(pp_line // 1) + 1
        model_prob = fetch_kalshi.poisson_prob_at_least(implied_threshold, e["projectedOuts"])
        e["outsMarketThreshold"] = implied_threshold
        e["outsModelProb"] = model_prob
        e["outsCall"] = "OVER" if model_prob >= 0.5 else "UNDER"
        e["predictionOutsLine"] = pp_line
        refined += 1

    if refined:
        print(f"  PrizePicks Outs: computed model call for {refined} entries")
    return ko_data


def merge_hits(hr_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in hr_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            line = hit["line"]
            if e.get("prizePicksHitsOpeningLine") is None:
                e["prizePicksHitsOpeningLine"] = line
            e["prizePicksHitsLineDelta"] = line - e["prizePicksHitsOpeningLine"]
            e["prizePicksHitsLine"] = line
            matched += 1
    print(f"  PrizePicks Hits: matched {matched}/{len(hr_data['entries'])} entries")
    hr_data["_ppHitsMatched"] = matched
    hr_data["_ppHitsTotal"] = len(hr_data["entries"])
    return hr_data


def refine_hits_with_prizepicks(hr_data):
    """Same frozen-prediction pattern as Strikeouts/Outs -- computed once,
    never recalculated on later refresh cycles, so the call doesn't
    silently drift as the line moves throughout the day. No Kalshi
    comparison; there's no confirmed Kalshi market for batter hits props."""
    import fetch_kalshi

    refined = 0
    for e in hr_data["entries"]:
        pp_line = e.get("prizePicksHitsLine")
        if pp_line is None:
            continue
        if e.get("hitsCall") is not None:
            continue
        if e.get("projectedHits") is None:
            # Board was saved by an older build_hr.py before this field
            # existed -- skip rather than crash; the next heavy rebuild
            # will regenerate this entry with the field present.
            continue
        implied_threshold = int(pp_line // 1) + 1
        model_prob = fetch_kalshi.poisson_prob_at_least(implied_threshold, e["projectedHits"])
        e["hitsMarketThreshold"] = implied_threshold
        e["hitsModelProb"] = model_prob
        e["hitsCall"] = "OVER" if model_prob >= 0.5 else "UNDER"
        e["predictionHitsLine"] = pp_line
        refined += 1

    if refined:
        print(f"  PrizePicks Hits: computed model call for {refined} entries")
    return hr_data


def merge_tb(hr_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in hr_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            line = hit["line"]
            if e.get("prizePicksTBOpeningLine") is None:
                e["prizePicksTBOpeningLine"] = line
            e["prizePicksTBLineDelta"] = line - e["prizePicksTBOpeningLine"]
            e["prizePicksTBLine"] = line
            matched += 1
    print(f"  PrizePicks Total Bases: matched {matched}/{len(hr_data['entries'])} entries")
    hr_data["_ppTBMatched"] = matched
    hr_data["_ppTBTotal"] = len(hr_data["entries"])
    return hr_data


def refine_tb_with_prizepicks(hr_data):
    """Same frozen-prediction pattern as the others."""
    import fetch_kalshi

    refined = 0
    for e in hr_data["entries"]:
        pp_line = e.get("prizePicksTBLine")
        if pp_line is None:
            continue
        if e.get("tbCall") is not None:
            continue
        if e.get("projectedTotalBases") is None:
            continue
        implied_threshold = int(pp_line // 1) + 1
        model_prob = fetch_kalshi.poisson_prob_at_least(implied_threshold, e["projectedTotalBases"])
        e["tbMarketThreshold"] = implied_threshold
        e["tbModelProb"] = model_prob
        e["tbCall"] = "OVER" if model_prob >= 0.5 else "UNDER"
        e["predictionTBLine"] = pp_line
        refined += 1

    if refined:
        print(f"  PrizePicks Total Bases: computed model call for {refined} entries")
    return hr_data


def merge_ko(ko_data, records):
    by_name = {norm_name(r["name"]): r for r in records}
    matched = 0
    for e in ko_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            line = hit["line"]
            if e.get("prizePicksOpeningLine") is None:
                e["prizePicksOpeningLine"] = line
            e["prizePicksLineDelta"] = line - e["prizePicksOpeningLine"]
            e["prizePicksKLine"] = line
            matched += 1
    print(f"  PrizePicks K: matched {matched}/{len(ko_data['entries'])} entries")

    if matched == 0 and records and ko_data["entries"]:
        # Zero matches despite real PrizePicks K records existing is
        # suspicious -- same diagnostic pattern that caught the earlier
        # Kalshi date-boundary bug (two legitimate-looking lists with zero
        # real overlap usually means they're actually about different
        # games/days, not a broken matcher).
        pp_names = sorted(set(r["name"] for r in records))
        board_names = sorted(set(e["name"] for e in ko_data["entries"]))
        print(f"  DEBUG: 0 matches -- PrizePicks has {len(pp_names)} pitcher(s) with a K line: {pp_names[:20]}")
        print(f"  DEBUG: today's board has {len(board_names)} pitcher(s): {board_names[:20]}")

    ko_data["_ppKMatched"] = matched
    ko_data["_ppKTotal"] = len(ko_data["entries"])

    return ko_data


def refine_ko_with_prizepicks(ko_data, kalshi_threshold_map):
    """For any entry with a PrizePicks line, make marketThreshold/modelProb/
    marketProb/edge all correctly correspond to PrizePicks' actual line --
    not whatever threshold Kalshi's own 'closest to 50%' pick happened to
    land on, which could be a different number entirely and would make the
    displayed edge silently answer the wrong question.

    The prediction itself (prizePicksCall/modelProb/marketThreshold) is
    frozen the first time it's computed and never recalculated again --
    without that, this function running every 10 minutes would silently
    flip the OVER/UNDER call back and forth as the line moved throughout
    the day, defeating the entire point of tracking a prediction. Kalshi's
    price comparison at that now-fixed threshold can still move live --
    that's real new information about the market's view, not a change to
    our own prediction.

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

        if e.get("prizePicksCall") is None:
            # PrizePicks lines are "over X.5" style -> clearing them means
            # actual_K >= floor(X.5) + 1. Handles integer lines the same way
            # (strictly more than the line).
            implied_threshold = int(pp_line // 1) + 1
            model_prob = fetch_kalshi.poisson_prob_at_least(implied_threshold, e["projectedK"])
            e["marketThreshold"] = implied_threshold
            e["modelProb"] = model_prob
            e["prizePicksCall"] = "OVER" if model_prob >= 0.5 else "UNDER"
            # The exact line this call was made against, frozen forever --
            # prizePicksKLine keeps tracking PrizePicks' current live line
            # (useful for its own movement-tracking purpose), but that can
            # drift away from the number this specific prediction/threshold
            # actually corresponds to. History and grading should always
            # reference THIS frozen value, not the live-drifting one.
            e["predictionLine"] = pp_line

        implied_threshold = e["marketThreshold"]
        kalshi_price = (kalshi_threshold_map.get(norm_name(e["name"])) or {}).get(implied_threshold)
        if kalshi_price is not None:
            if e.get("openingProb") is None or e.get("openingThreshold") != implied_threshold:
                e["openingProb"] = kalshi_price
                e["openingThreshold"] = implied_threshold
            e["priceDelta"] = kalshi_price - e["openingProb"]
            e["marketProb"] = kalshi_price
            e["edge"] = e["modelProb"] - kalshi_price
        else:
            # Kalshi doesn't have a market at this exact threshold -- showing
            # no edge is more honest than showing one computed against a
            # different number.
            e["marketProb"] = None
            e["edge"] = None
            e["openingProb"] = None
            e["openingThreshold"] = None
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
