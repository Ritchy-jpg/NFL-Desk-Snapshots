"""Validate a Polymarket NCAA quote export before sharp-market comparison.

Flags illiquid, wide, or mutually inconsistent order books. Does not infer EV.
"""
import argparse
import csv
from collections import defaultdict

def number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def audit(rows, min_size=20, max_spread=0.08):
    by_market = defaultdict(list)
    for row in rows:
        by_market[row["market_id"]].append(row)
    output = []
    for market_rows in by_market.values():
        for row in market_rows:
            ask = number(row["best_ask"])
            bid = number(row["best_bid"])
            size = number(row["ask_size"])
            reasons = []
            if ask is None or not 0 < ask < 1:
                reasons.append("NO_VALID_ASK")
            if bid is None or not 0 <= bid < 1:
                reasons.append("NO_VALID_BID")
            if ask is not None and bid is not None:
                if bid > ask:
                    reasons.append("CROSSED_BOOK")
                if ask - bid > max_spread:
                    reasons.append("WIDE_SPREAD")
            if size is None or size < min_size:
                reasons.append("LOW_ASK_DEPTH")
            # Binary market: simultaneous buying of both outcomes below $1
            # would be a suspicious crossed/inconsistent snapshot.
            others = [x for x in market_rows if x["token_id"] != row["token_id"]]
            if len(others) == 1 and ask is not None:
                other_ask = number(others[0]["best_ask"])
                if other_ask is not None and ask + other_ask < 0.995:
                    reasons.append("INCONSISTENT_BINARY_ASKS")
            result = dict(row)
            result["audit_status"] = "REVIEW" if reasons else "QUOTE_OK"
            result["audit_reasons"] = "|".join(reasons)
            # Market equivalence, settlement terms, sharp price and fee model
            # are not validated by this audit.
            result["eligible_for_ev"] = "False"
            output.append(result)
    return output

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="polymarket_ncaa_quotes.csv")
    p.add_argument("--output", default="polymarket_ncaa_audit.csv")
    args = p.parse_args()
    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    checked = audit(rows)
    fields = list(checked[0]) if checked else list(rows[0]) + ["audit_status", "audit_reasons"] if rows else []
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        if fields:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(checked)
    print({"total": len(checked), "quote_ok": sum(r["audit_status"] == "QUOTE_OK" for r in checked),
           "review": sum(r["audit_status"] == "REVIEW" for r in checked),
           "eligible_for_ev": 0})

if __name__ == "__main__":
    main()
