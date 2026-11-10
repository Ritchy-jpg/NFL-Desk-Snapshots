"""Public Polymarket connectivity and exact-name discovery probe.

Does not claim an executable quote, place orders, or use credentials.
"""
import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://gamma-api.polymarket.com"


def fetch(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "NFLDeskPublicProbe/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def run():
    report = {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": "Gamma public REST", "read_only": True,
              "checks": {}, "matching_markets": [], "status": "NO_VERIFIED_PRICE"}
    markets = fetch("/markets", {"closed": "false", "limit": 5})
    report["checks"]["gamma_markets"] = {"ok": isinstance(markets, list), "count": len(markets)}
    # Scan a bounded sample only; cannot assert nonexistence from a sample.
    events = fetch("/events", {"closed": "false", "limit": 100, "offset": 0})
    report["checks"]["gamma_events"] = {"ok": isinstance(events, list), "count": len(events),
                                         "complete": False}
    for event in events:
        title = str(event.get("title", ""))
        if "kent state" in title.lower() and "bowling green" in title.lower():
            report["matching_markets"].append({
                "event_id": event.get("id"), "title": title,
                "slug": event.get("slug"),
                "markets": [{"id": m.get("id"), "question": m.get("question"),
                             "clobTokenIds": m.get("clobTokenIds")}
                            for m in event.get("markets", [])]})
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="polymarket_probe.json")
    args = p.parse_args()
    report = run()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
