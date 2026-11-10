"""Join Polymarket NCAA totals to the existing Desk Pinnacle snapshot.

Read-only. Exact event, total line, and Over/Under only. No invented quotes.
Historical comparisons are never executable.
"""
import argparse
import csv
import re
from datetime import datetime

EVENT = "cfb-kentst-bowlgr-2026-11-10"
GAME = "Bowling Green Falcons vs Kent State Golden Flashes"
PATTERN = re.compile(r"^(?:Total|Total Points|Over/Under):?\\s*(\\d+(?:\\.\\d+)?)$", re.I)

def timestamp(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

def key_for_poly(row):
    question = row.get("question", "").strip()
    outcome = row.get("outcome", "").strip().lower()
    match = PATTERN.fullmatch(question)
    if match and outcome in ("over", "under"):
        return (float(match.group(1)), outcome)
    # Polymarket also uses "Total Points: 43.5" and "Over"/"Under".
    return None

def load_pinnacle(rows):
    out = {}
    for row in rows:
        if row.get("match_pin") != GAME or row.get("market") != "total":
            continue
        if row.get("pricing_status") not in ("EXACT_MAIN", "EXACT_ALT"):
            continue
        sel = row.get("selection", "")
        side = "over" if sel.startswith("Plus de ") else "under" if sel.startswith("Moins de ") else None
        try:
            line = float(row["pin_exact_line"])
            fair = float(row["pin_exact_fair_prob"])
            price = float(row["pin_exact_price"])
        except (ValueError, TypeError):
            continue
        if side and 0 < fair < 1 and price > 1:
            out[(line, side)] = {"fair": fair, "price": price,
                                  "time": row.get("pin_snapshot_at", "")}
    return out

def compare(poly, sharp):
    pin = load_pinnacle(sharp)
    results = []
    for row in poly:
        if row.get("event_slug") != EVENT:
            continue
        k = key_for_poly(row)
        if k is None or k not in pin:
            continue
        p = pin[k]
        ask = float(row["best_ask"]) if row.get("best_ask") else None
        pt, st = timestamp(row.get("captured_at_utc")), timestamp(p["time"])
        age = abs((pt-st).total_seconds()) if pt and st else None
        ev = p["fair"]/ask-1 if ask and 0 < ask < 1 else None
        results.append({
            "event_slug": EVENT, "market": "total", "line": k[0], "side": k[1],
            "polymarket_ask": ask, "polymarket_decimal": round(1/ask, 4) if ask else "",
            "pinnacle_decimal": p["price"], "pinnacle_fair": round(p["fair"], 6),
            "ev_before_fees": round(ev, 6) if ev is not None else "",
            "poly_captured_at": row.get("captured_at_utc", ""),
            "pin_captured_at": p["time"], "age_seconds": round(age) if age is not None else "",
            "status": "HISTORICAL_ONLY" if age is None or age > 300 else "REVIEW_FEES_AND_RULES",
            "bet": False
        })
    return results

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--poly", default="polymarket_ncaa_audit.csv")
    p.add_argument("--pin", default="output/gp_pin_main_ncaa_latest.csv")
    p.add_argument("--output", default="polymarket_pinnacle_historical.csv")
    a = p.parse_args()
    with open(a.poly, newline="", encoding="utf-8") as f:
        poly = list(csv.DictReader(f))
    with open(a.pin, newline="", encoding="utf-8") as f:
        sharp = list(csv.DictReader(f))
    result = compare(poly, sharp)
    fields = ["event_slug","market","line","side","polymarket_ask","polymarket_decimal",
              "pinnacle_decimal","pinnacle_fair","ev_before_fees","poly_captured_at",
              "pin_captured_at","age_seconds","status","bet"]
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(result)
    print({"exact_total_matches":len(result),"positive_historical_ev":sum(isinstance(x["ev_before_fees"],float) and x["ev_before_fees"]>0 for x in result),"bets":0})

if __name__ == "__main__":
    main()
