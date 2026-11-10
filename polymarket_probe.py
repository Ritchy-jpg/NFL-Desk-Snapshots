"""Read-only Gamma/CLOB NCAA match probe. No credentials or orders."""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "NFLDeskPublicProbe/1.1", "Accept": "application/json"}


def fetch(base, path, params):
    url = base + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 503) or attempt == 2:
                raise
            time.sleep(min(float(error.headers.get("Retry-After", "2")), 15))


def arr(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except ValueError:
            return []
    return []


def book(token):
    data = fetch(CLOB, "/book", {"token_id": token})
    asks = [(float(x["price"]), float(x["size"])) for x in data.get("asks", [])
            if float(x.get("size", 0)) > 0]
    bids = [(float(x["price"]), float(x["size"])) for x in data.get("bids", [])
            if float(x.get("size", 0)) > 0]
    return {"best_ask": min(asks)[0] if asks else None,
            "best_ask_size": min(asks)[1] if asks else None,
            "best_bid": max(bids)[0] if bids else None,
            "best_bid_size": max(bids)[1] if bids else None}


def run(max_pages, page_size):
    report = {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "read_only": True, "events_scanned": 0, "scan_complete": False,
              "matches": [], "status": "NO_VERIFIED_PRICE"}
    for offset in range(0, max_pages * page_size, page_size):
        events = fetch(GAMMA, "/events",
                       {"closed": "false", "limit": page_size, "offset": offset})
        report["events_scanned"] += len(events)
        for event in events:
            text = (str(event.get("title", "")) + " " + str(event.get("slug", ""))).lower()
            if not (("kent state" in text or "kent-state" in text)
                    and ("bowling green" in text or "bowling-green" in text)):
                continue
            match = {"event_id": event.get("id"), "title": event.get("title"),
                     "slug": event.get("slug"), "markets": []}
            for market in event.get("markets", []):
                tokens = arr(market.get("clobTokenIds"))
                outcomes = arr(market.get("outcomes"))
                row = {"market_id": market.get("id"),
                       "question": market.get("question"), "outcomes": []}
                for index, token in enumerate(tokens):
                    side = {"outcome": outcomes[index] if index < len(outcomes) else None,
                            "token_id": str(token)}
                    try:
                        side.update(book(str(token)))
                    except (urllib.error.URLError, ValueError, KeyError) as error:
                        side["error"] = str(error)
                    row["outcomes"].append(side)
                match["markets"].append(row)
            report["matches"].append(match)
        if len(events) < page_size:
            report["scan_complete"] = True
            break
    if report["matches"]:
        report["status"] = "BOOKS_CAPTURED_REVIEW_MARKET_RULES"
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--output", default="polymarket_probe.json")
    args = p.parse_args()
    if not 1 <= args.max_pages <= 100 or not 1 <= args.page_size <= 100:
        p.error("max-pages and page-size must be 1..100")
    report = run(args.max_pages, args.page_size)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({"status": report["status"], "events_scanned": report["events_scanned"],
                      "matches": len(report["matches"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
