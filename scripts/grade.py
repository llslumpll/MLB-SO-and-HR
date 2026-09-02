"""
Walk every saved day's HR/K board and grade any ungraded entries against
real box scores. Safe to run repeatedly -- already-graded entries are
left alone, and games that haven't finished yet are simply skipped
(they'll get picked up on a future run).

Important: MLB's boxscore endpoint pre-populates a player's stats object
(with zeros) as soon as a lineup is set, even before the game starts --
so we can't infer "game is over" from whether the stats object exists.
We check the actual game status instead.
"""

import glob
import json

from common import API, get, parse_ip


_status_cache = {}


def is_game_final(game_pk):
    """True only if the game has actually completed."""
    if game_pk in _status_cache:
        return _status_cache[game_pk]
    try:
        data = get(f"{API}/schedule", params={"gamePk": game_pk})
        dates = data.get("dates") or []
        games = dates[0]["games"] if dates else []
        if not games:
            _status_cache[game_pk] = False
            return False
        status = games[0].get("status", {})
        final = status.get("abstractGameState") == "Final"
        _status_cache[game_pk] = final
        return final
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] status check failed for game {game_pk}: {e}")
        _status_cache[game_pk] = False
        return False


def fetch_boxscore(game_pk):
    return get(f"{API}/game/{game_pk}/boxscore")


def grade_hr_file(path):
    with open(path) as f:
        data = json.load(f)

    # Self-heal: entries marked graded=True whose game isn't actually Final
    # are artifacts of a prior bug (grading before the game finished). Reset
    # them so they get correctly re-graded once the game genuinely ends.
    wrongly_graded_pks = {e["gamePk"] for e in data["entries"] if e.get("graded")}
    repaired = 0
    for pk in wrongly_graded_pks:
        if not is_game_final(pk):
            for e in data["entries"]:
                if e["gamePk"] == pk and e.get("graded"):
                    e["graded"] = False
                    e["hr"] = None
                    repaired += 1
    if repaired:
        print(f"    repaired {repaired} entries incorrectly graded before their game finished")

    pending = [e for e in data["entries"] if not e.get("graded")]
    if not pending:
        if repaired:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        return 0

    final_game_pks = {pk for pk in {e["gamePk"] for e in pending} if is_game_final(pk)}
    if not final_game_pks:
        if repaired:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        return 0

    boxscores = {}
    for pk in final_game_pks:
        try:
            boxscores[pk] = fetch_boxscore(pk)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] boxscore fetch failed for game {pk}: {e}")

    graded_count = 0
    for e in data["entries"]:
        if e.get("graded") or e["gamePk"] not in final_game_pks:
            continue
        box = boxscores.get(e["gamePk"])
        if not box:
            continue
        all_players = {**(box["teams"]["away"].get("players") or {}), **(box["teams"]["home"].get("players") or {})}
        p = all_players.get(f"ID{e['playerId']}")
        if p and p.get("stats", {}).get("batting") is not None:
            e["graded"] = True
            e["hr"] = (p["stats"]["batting"].get("homeRuns") or 0) > 0
            graded_count += 1

    if graded_count or repaired:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    return graded_count


def grade_ko_file(path):
    with open(path) as f:
        data = json.load(f)

    wrongly_graded_pks = {e["gamePk"] for e in data["entries"] if e.get("graded") and e.get("gamePk")}
    repaired = 0
    for pk in wrongly_graded_pks:
        if not is_game_final(pk):
            for e in data["entries"]:
                if e.get("gamePk") == pk and e.get("graded"):
                    e["graded"] = False
                    e["actualK"] = None
                    e["hit"] = None
                    repaired += 1
    if repaired:
        print(f"    repaired {repaired} entries incorrectly graded before their game finished")

    # Backfill predictionLine/predictionOutsLine for ANY already-graded
    # entry missing it. This must run unconditionally, independent of
    # whether there's anything left to grade this run -- a day where
    # every entry is already graded would otherwise return early below
    # and never reach this repair at all, which is exactly how already-
    # graded entries kept showing an inconsistent Line/Result even after
    # the backfill logic existed.
    backfilled = 0
    for e in data["entries"]:
        if e.get("graded"):
            if e.get("predictionLine") is None and e.get("marketThreshold") is not None:
                e["predictionLine"] = e["marketThreshold"] - 0.5
                backfilled += 1
            if e.get("predictionOutsLine") is None and e.get("outsMarketThreshold") is not None:
                e["predictionOutsLine"] = e["outsMarketThreshold"] - 0.5
                backfilled += 1
    if backfilled:
        print(f"    backfilled predictionLine for {backfilled} already-graded entries")

    pending = [e for e in data["entries"] if not e.get("graded")]
    if not pending:
        if repaired or backfilled:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        return 0

    final_game_pks = {pk for pk in {e.get("gamePk") for e in pending if e.get("gamePk")} if is_game_final(pk)}
    if not final_game_pks:
        if repaired or backfilled:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        return 0

    boxscores = {}
    for pk in final_game_pks:
        try:
            boxscores[pk] = fetch_boxscore(pk)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] boxscore fetch failed for game {pk}: {e}")

    graded_count = 0
    for e in data["entries"]:
        if e.get("graded") or e.get("gamePk") not in final_game_pks:
            continue
        box = boxscores.get(e["gamePk"])
        if not box:
            continue
        all_players = {**(box["teams"]["away"].get("players") or {}), **(box["teams"]["home"].get("players") or {})}
        p = all_players.get(f"ID{e['playerId']}")
        if p and p.get("stats", {}).get("pitching") is not None:
            pitching = p["stats"]["pitching"]
            e["graded"] = True
            e["actualK"] = pitching.get("strikeOuts")
            if e.get("marketThreshold") is not None and e["actualK"] is not None:
                e["hit"] = e["actualK"] >= e["marketThreshold"]
            # Backfill for predictions that were frozen before predictionLine
            # existed as a field -- without this, an old entry's displayed
            # line would keep showing whatever prizePicksKLine has since
            # drifted to, while hit/Result above is correctly graded against
            # the OLDER, frozen marketThreshold, producing exactly the same
            # inconsistent-looking Result this fix is meant to close.
            # PrizePicks lines are virtually always half-point (X.5) to
            # avoid ties, so threshold-0.5 recovers the original line for
            # the vast majority of real cases; this is an approximation
            # only for this backward-compatibility path, not for any
            # prediction frozen going forward (those capture the real,
            # exact line directly).
            if e.get("predictionLine") is None and e.get("marketThreshold") is not None:
                e["predictionLine"] = e["marketThreshold"] - 0.5

            # Outs: MLB's "inningsPitched" is thirds-based ("5.2" means 5 and
            # 2/3 innings = 17 outs), not a literal decimal -- parse_ip
            # already handles that conversion correctly.
            ip = parse_ip(pitching.get("inningsPitched"))
            if ip is not None:
                e["actualOuts"] = round(ip * 3)
                if e.get("outsMarketThreshold") is not None:
                    e["outsHit"] = e["actualOuts"] >= e["outsMarketThreshold"]
                if e.get("predictionOutsLine") is None and e.get("outsMarketThreshold") is not None:
                    e["predictionOutsLine"] = e["outsMarketThreshold"] - 0.5
            graded_count += 1

    if graded_count or repaired or backfilled:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    return graded_count


if __name__ == "__main__":
    total = 0
    for path in sorted(glob.glob("data/hr/*.json")):
        n = grade_hr_file(path)
        if n:
            print(f"  {path}: graded {n} entries")
        total += n
    for path in sorted(glob.glob("data/ko/*.json")):
        n = grade_ko_file(path)
        if n:
            print(f"  {path}: graded {n} entries")
        total += n
    print(f"Graded {total} total entries across all days")
