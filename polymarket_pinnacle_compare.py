"""Compare Polymarket ask to *verified* Pinnacle two-sided decimal quotes.

Input Pinnacle CSV columns: event_slug,market_id,question,outcome,
pinnacle_outcome_odds,pinnacle_opposite_odds,verified_at_utc.
A missing sharp quote is always PASS_NO_SHARP_PRICE. No guessed prices.
"""
import argparse
import csv
from datetime import datetime, timezone

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def compare(poly, sharp):
    by_key = {(x["event_slug"], x["market_id"], x["outcome"]): x for x in sharp}
    out = []
    for p in poly:
        r = dict(p)
        s = by_key.get((p["event_slug"], p["market_id"], p["outcome"]))
        r.update({"pinnacle_outcome_odds": "", "pinnacle_opposite_odds": "",
                  "pinnacle_verified_at_utc": "", "sharp_fair_probability": "",
                  "polymarket_ev_before_fees": "", "buy_price_max_before_fees": "",
                  "decision": "PASS_NO_SHARP_PRICE"})
        if not s:
            out.append(r)
            continue
        r["pinnacle_outcome_odds"] = s["pinnacle_outcome_odds"]
        r["pinnacle_opposite_odds"] = s["pinnacle_opposite_odds"]
        r["pinnacle_verified_at_utc"] = s["verified_at_utc"]
        # Reject historical or non-synchronous comparisons as actionable signals.
        try:
            poly_time = datetime.fromisoformat(str(p.get("captured_at_utc", "")).replace("Z", "+00:00"))
            sharp_time = datetime.fromisoformat(str(s["verified_at_utc"]).replace("Z", "+00:00"))
            age_seconds = abs((poly_time - sharp_time).total_seconds())
        except (ValueError, TypeError):
            age_seconds = float("inf")
        a, b = num(s["pinnacle_outcome_odds"]), num(s["pinnacle_opposite_odds"])
        ask = num(p.get("best_ask"))
        if age_seconds > 300:
            r["decision"] = "PASS_STALE_SHARP_SNAPSHOT"
        elif not a or not b or a <= 1 or b <= 1:
            r["decision"] = "PASS_INVALID_SHARP"
        elif not ask or not 0 < ask < 1:
            r["decision"] = "PASS_NO_ASK"
        elif p.get("audit_status") != "QUOTE_OK":
            r["decision"] = "PASS_BOOK_REVIEW"
        else:
            fair = (1/a) / ((1/a)+(1/b))
            r["sharp_fair_probability"] = round(fair, 6)
            r["polymarket_ev_before_fees"] = round(fair / ask - 1, 6)
            r["buy_price_max_before_fees"] = round(fair, 6)
            r["decision"] = "REVIEW_POSITIVE_EV_FEES_AND_RULES" if fair > ask else "PASS_NEGATIVE_EV"
        out.append(r)
    return out

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--polymarket", default="polymarket_ncaa_audit.csv")
    p.add_argument("--pinnacle", default="pinnacle_verified_quotes.csv")
    p.add_argument("--output", default="polymarket_pinnacle_comparison.csv")
    args = p.parse_args()
    with open(args.polymarket, newline="", encoding="utf-8") as f:
        poly = list(csv.DictReader(f))
    try:
        with open(args.pinnacle, newline="", encoding="utf-8") as f:
            sharp = list(csv.DictReader(f))
    except FileNotFoundError:
        sharp = []
    result = compare(poly, sharp)
    fields = list(result[0]) if result else []
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        if fields:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result)
    from collections import Counter
    print({"rows": len(result), "sharp_rows": len(sharp),
           "decisions": dict(Counter(x["decision"] for x in result))})

if __name__ == "__main__":
    main()
