"""Read-only Polymarket market discovery + indicative CLOB quotes.

No trading endpoints, credentials, or bookmaker API calls.
Run: python polymarket_readonly.py --limit 20 --output polymarket_snapshot.json
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "NFL-Desk-Polymarket-Readonly/0.1", "Accept": "application/json"}


def get_json(base, path, params=None, retries=3):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS, method="GET")
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == retries - 1:
                raise
            wait = exc.headers.get("Retry-After", "2")
            try:
                seconds = min(max(float(wait), 0), 30)
            except ValueError:
                seconds = 2
            time.sleep(seconds)
        except (TimeoutError, urllib.error.URLError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def parse_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def quote(token_id):
    # CLOB order-book asks represent prices available to buy YES/NO shares.
    book = get_json(CLOB, "/book", {"token_id": token_id})
    asks = [float(x["price"]) for x in book.get("asks", [])
            if float(x.get("price", 0)) > 0 and float(x.get("size", 0)) > 0]
    bids = [float(x["price"]) for x in book.get("bids", [])
            if float(x.get("price", 0)) > 0 and float(x.get("size", 0)) > 0]
    return {"best_ask": min(asks) if asks else None,
            "best_bid": max(bids) if bids else None}


def collect(limit):
    events = get_json(GAMMA, "/events", {"active": "true", "closed": "false", "limit": limit})
    result = []
    for event in events:
        title = str(event.get("title", ""))
        # Broad discovery only: classification is intentionally not an automatic match.
        if not any(term in (title + " " + str(event.get("slug", ""))).lower()
                   for term in ("nfl", "ncaa", "football", "super bowl")):
            continue
        for market in event.get("markets", []):
            tokens = parse_array(market.get("clobTokenIds"))
            outcomes = parse_array(market.get("outcomes"))
            sides = []
            for i, token in enumerate(tokens):
                try:
                    prices = quote(str(token))
                    sides.append({"outcome": outcomes[i] if i < len(outcomes) else None,
                                  "token_id": str(token), **prices})
                except (urllib.error.URLError, ValueError, KeyError) as exc:
                    sides.append({"token_id": str(token), "error": str(exc)})
            result.append({"event_id": event.get("id"), "event": title,
                           "event_slug": event.get("slug"), "market_id": market.get("id"),
                           "condition_id": market.get("conditionId"),
                           "question": market.get("question"),
                           "end_date": market.get("endDate"), "sides": sides,
                           "match_status": "UNMATCHED"})
    return {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "Polymarket Gamma + CLOB public GET", "read_only": True,
            "events_scanned": len(events), "markets": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", default="polymarket_snapshot.json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    snapshot = collect(args.limit)
    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(snapshot, file, indent=2, ensure_ascii=False)
    print(f"Scanned {snapshot['events_scanned']} events; captured {len(snapshot['markets'])} candidate markets")


if __name__ == "__main__":
    main()
