"""Convert public Polymarket order-book snapshot to exact-match quote rows."""
import argparse
import csv
import json
from datetime import datetime, timezone

TARGET = "cfb-kentst-bowlgr-2026-11-10"
FIELDS = ["captured_at_utc", "event_slug", "market_id", "question", "outcome",
          "token_id", "best_bid", "best_ask", "ask_size", "decimal_buy",
          "price_status", "fees_included", "eligible_for_ev"]


def extract(report, target=TARGET):
    rows = []
    seen = set()
    for event in report.get("matches", []):
        if event.get("slug") != target:
            continue
        for market in event.get("markets", []):
            for side in market.get("outcomes", []):
                token = str(side.get("token_id") or "")
                key = (str(market.get("market_id")), token)
                if not token or key in seen:
                    continue
                seen.add(key)
                ask = side.get("best_ask")
                valid = isinstance(ask, (float, int)) and 0 < ask <= 1
                rows.append({
                    "captured_at_utc": report.get("captured_at_utc"),
                    "event_slug": target,
                    "market_id": market.get("market_id"),
                    "question": market.get("question"),
                    "outcome": side.get("outcome"),
                    "token_id": token,
                    "best_bid": side.get("best_bid"),
                    "best_ask": ask,
                    "ask_size": side.get("best_ask_size"),
                    "decimal_buy": round(1 / ask, 4) if valid else None,
                    "price_status": "SNAPSHOT_ASK" if valid else "NO_ASK",
                    "fees_included": False,
                    "eligible_for_ev": False,
                })
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="polymarket_probe.json")
    p.add_argument("--output", default="polymarket_ncaa_quotes.csv")
    args = p.parse_args()
    with open(args.input, encoding="utf-8") as f:
        report = json.load(f)
    rows = extract(report)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"event_slug": TARGET, "rows": len(rows),
                      "with_ask": sum(r["price_status"] == "SNAPSHOT_ASK" for r in rows),
                      "eligible_for_ev": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
