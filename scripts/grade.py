"""
Walk every saved day's HR/K board and grade any ungraded entries against
real box scores. Safe to run repeatedly -- already-graded entries are
left alone, and games that haven't finished yet are simply skipped
(they'll get picked up on a future run).
"""

import glob
import json
import os

from common import API, get


def fetch_boxscore(game_pk):
    return get(f"{API}/game/{game_pk}/boxscore")


def grade_hr_file(path):
    with open(path) as f:
        data = json.load(f)
    pending = [e for e in data["entries"] if not e.get("graded")]
    if not pending:
        return 0
    game_pks = sorted(set(e["gamePk"] for e in pending))
    boxscores = {}
    for pk in game_pks:
        try:
            boxscores[pk] = fetch_boxscore(pk)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] boxscore fetch failed for game {pk}: {e}")
            boxscores[pk] = None

    graded_count = 0
    for e in data["entries"]:
        if e.get("graded"):
            continue
        box = boxscores.get(e["gamePk"])
        if not box:
            continue
        all_players = {**(box["teams"]["away"].get("players") or {}), **(box["teams"]["home"].get("players") or {})}
        p = all_players.get(f"ID{e['playerId']}")
        if p and p.get("stats", {}).get("batting") is not None:
            # game is final if the boxscore has a batting line recorded with atBats present
            if "atBats" in p["stats"]["batting"] or box.get("teams", {}).get("away", {}).get("teamStats"):
                e["graded"] = True
                e["hr"] = (p["stats"]["batting"].get("homeRuns") or 0) > 0
                graded_count += 1

    if graded_count:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    return graded_count


def grade_ko_file(path):
    with open(path) as f:
        data = json.load(f)
    pending = [e for e in data["entries"] if not e.get("graded")]
    if not pending:
        return 0
    game_pks = sorted(set(e["gamePk"] for e in pending if e.get("gamePk")))
    boxscores = {}
    for pk in game_pks:
        try:
            boxscores[pk] = fetch_boxscore(pk)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] boxscore fetch failed for game {pk}: {e}")
            boxscores[pk] = None

    graded_count = 0
    for e in data["entries"]:
        if e.get("graded"):
            continue
        box = boxscores.get(e.get("gamePk"))
        if not box:
            continue
        all_players = {**(box["teams"]["away"].get("players") or {}), **(box["teams"]["home"].get("players") or {})}
        p = all_players.get(f"ID{e['playerId']}")
        if p and p.get("stats", {}).get("pitching") is not None:
            pitching = p["stats"]["pitching"]
            if "strikeOuts" in pitching:
                e["graded"] = True
                e["actualK"] = pitching.get("strikeOuts")
                if e.get("marketThreshold") is not None and e["actualK"] is not None:
                    e["hit"] = e["actualK"] >= e["marketThreshold"]
                graded_count += 1

    if graded_count:
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
