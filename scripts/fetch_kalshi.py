"""
Pull live Kalshi HR/strikeout market prices and merge them into today's
saved board JSON (marketProb/edge fields), matching by normalized name.
No API key needed -- this runs server-side (GitHub Actions), so it isn't
subject to the browser CORS restriction the live site hits.
"""

import json
import re
import sys
import time
from datetime import datetime

from common import KALSHI, get, norm_name, to_num, clip


def fetch_events(series_ticker):
    events, cursor = [], None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/events", params=params)
        batch = data.get("events") or []
        events.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return events


def fetch_markets_for_event(event_ticker):
    data = get(f"{KALSHI}/markets", params={"event_ticker": event_ticker, "status": "open"})
    return data.get("markets") or []


def ticker_threshold(ticker):
    m = re.search(r"-(\d+)$", ticker or "")
    return int(m.group(1)) if m else None


def strip_display_name(sub):
    m = re.match(r"^(.*?):\s*\d+\+\s*$", sub or "")
    return m.group(1).strip() if m else (sub or "").strip()


def pull_series(series_ticker, label):
    print(f"Fetching {series_ticker} ({label})...")
    try:
        events = fetch_events(series_ticker)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] couldn't reach Kalshi: {e}")
        return []
    if not events:
        print("  no open events right now")
        return []
    print(f"  {len(events)} game(s)")

    records = []
    for ev in events:
        ticker = ev.get("event_ticker")
        if not ticker:
            continue
        try:
            markets = fetch_markets_for_event(ticker)
        except Exception:  # noqa: BLE001
            continue
        for m in markets:
            sub = (m.get("yes_sub_title") or m.get("title") or "").strip()
            mkt_ticker = m.get("ticker", "")
            threshold = ticker_threshold(mkt_ticker)
            if threshold is None or not sub:
                continue
            name = strip_display_name(sub)
            price_raw = m.get("yes_ask_dollars") or m.get("yes_bid_dollars")
            price = to_num(price_raw)
            if not name or price is None or price <= 0:
                continue
            records.append({"name": name, "threshold": threshold, "price": price})
        time.sleep(0.1)
    return records


def merge_hr(hr_data, records):
    by_name = {}
    for r in records:
        if r["threshold"] == 1:
            by_name[norm_name(r["name"])] = r
    matched = 0
    now = datetime.utcnow().isoformat()
    for e in hr_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            price = hit["price"]
            if e.get("openingProb") is None:
                e["openingProb"] = price
            e["priceDelta"] = price - e["openingProb"]
            e["marketProb"] = price
            e["edge"] = e["heuristicProb"] - price
            e["oddsUpdatedAt"] = now
            matched += 1
    hr_data["oddsRefreshedAt"] = now
    print(f"  HR: matched {matched}/{len(hr_data['entries'])} entries to Kalshi prices")
    return hr_data


def poisson_prob_at_least(threshold, lam):
    import math
    if lam <= 0:
        return 0.0 if threshold > 0 else 1.0
    cum = 0.0
    for k in range(threshold):
        cum += math.exp(-lam + k * math.log(lam) - sum(math.log(i) for i in range(2, k + 1)))
    return clip(1 - cum, 0.001, 0.999)


def k_threshold_map(records):
    """Raw {norm_name: {threshold: price}} -- every threshold Kalshi has a
    market for, unfiltered, so a specific threshold can be looked up on
    demand (e.g. to match a PrizePicks line) rather than only Kalshi's own
    'closest to 50%' pick."""
    out = {}
    for r in records:
        key = norm_name(r["name"])
        out.setdefault(key, {})[r["threshold"]] = r["price"]
    return out


def merge_ko(ko_data, records):
    by_name = {}
    for r in records:
        key = norm_name(r["name"])
        prev = by_name.get(key)
        if prev is None or abs(r["price"] - 0.5) < abs(prev["price"] - 0.5):
            by_name[key] = r
    matched = 0
    now = datetime.utcnow().isoformat()
    for e in ko_data["entries"]:
        hit = by_name.get(norm_name(e["name"]))
        if hit:
            # Once a PrizePicks-based prediction has frozen a threshold,
            # THAT threshold is the real one -- this function must not
            # keep overwriting it with whatever Kalshi's own unrelated
            # "closest to 50%" market happens to be. refine_ko_with_
            # prizepicks already does its own Kalshi price lookup AT the
            # frozen threshold, so these fields are correctly maintained
            # without this function's help once a PrizePicks call exists.
            # Without this guard, every 10-minute cycle would silently
            # replace the real predicted line with Kalshi's own number,
            # which is how multiple unrelated pitchers ended up sharing
            # an identical, wrong displayed line.
            if e.get("prizePicksCall") is not None:
                matched += 1
                continue
            price = hit["price"]
            threshold = hit["threshold"]
            # If we're now tracking a DIFFERENT threshold than whatever
            # openingProb was recorded under, that old opening price
            # belongs to a different question entirely -- treat this as a
            # fresh first sighting rather than comparing prices from two
            # different thresholds as if they were the same market moving.
            if e.get("openingProb") is None or e.get("openingThreshold") != threshold:
                e["openingProb"] = price
                e["openingThreshold"] = threshold
            e["priceDelta"] = price - e["openingProb"]
            e["marketThreshold"] = threshold
            e["marketProb"] = price
            e["modelProb"] = poisson_prob_at_least(threshold, e["projectedK"])
            e["edge"] = e["modelProb"] - price
            e["oddsUpdatedAt"] = now
            matched += 1
    ko_data["oddsRefreshedAt"] = now
    print(f"  K: matched {matched}/{len(ko_data['entries'])} entries to Kalshi prices")
    return ko_data


if __name__ == "__main__":
    date = sys.argv[1]
    hr_records = pull_series("KXMLBHR", "home runs")
    ko_records = pull_series("KXMLBKS", "strikeouts")

    try:
        with open(f"data/hr/{date}.json") as f:
            hr_data = json.load(f)
        hr_data = merge_hr(hr_data, hr_records)
        hr_data["entries"].sort(key=lambda e: -e["heuristicProb"])
        with open(f"data/hr/{date}.json", "w") as f:
            json.dump(hr_data, f, indent=2, default=str)
    except FileNotFoundError:
        print(f"  [warn] no data/hr/{date}.json to merge into")

    try:
        with open(f"data/ko/{date}.json") as f:
            ko_data = json.load(f)
        ko_data = merge_ko(ko_data, ko_records)
        ko_data["entries"].sort(key=lambda e: -e["projectedK"])
        with open(f"data/ko/{date}.json", "w") as f:
            json.dump(ko_data, f, indent=2, default=str)
    except FileNotFoundError:
        print(f"  [warn] no data/ko/{date}.json to merge into")
