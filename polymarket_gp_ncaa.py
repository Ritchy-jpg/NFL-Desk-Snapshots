"""Compare NCAA full-game prices from public Polymarket books and a published GP export."""
import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from polymarket_scanner import PARIS, timestamp, write_csv

TYPES = {"moneyline": "moneyline", "spreads": "spread", "totals": "total"}
TEAM_ALIASES = {"central mich": "central michigan", "massachusetts": "umass"}
MONEYLINE, SPREAD, TOTAL = ("moneyline", "spread", "total")
MAX_SKEW = timedelta(minutes=5)


def normalize(text):
    text = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", text)
    value = " ".join("state" if word == "st" else word for word in words)
    for old, new in TEAM_ALIASES.items():
        if value == old or value.startswith(old + " "):
            return new + value[len(old):]
    return value


def decimal(value):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def half_point(value):
    line = decimal(value)
    return line if line is not None and abs(line) % 1 == Decimal("0.5") else None


def gp_contract(row):
    kind = row["market_type"]
    label = normalize(row["market_name"])
    if row["odd_status"] != "0" or label not in {
        MONEYLINE: {"vainqueur prolongations incluses"},
        SPREAD: {"ecart prolongation incluse", "ecart prolongations incluses"},
        TOTAL: {"total prolongations incluses", "total prolongation incluse"},
    }.get(kind, set()):
        return None
    selection = row["selection"]
    if kind == MONEYLINE and selection in ("1", "2"):
        return kind, int(selection) - 1, None
    if kind == SPREAD:
        match = re.fullmatch(r"([12]) \(([+-]\d+\.5)\)", selection)
        if match:
            side, line = int(match[1]) - 1, half_point(match[2])
            if decimal(row["line"]) == (line if side == 0 else -line):
                return kind, side, line
    if kind == TOTAL:
        match = re.fullmatch(r"(plus|moins) de (\d+\.5)", selection, re.I)
        if match and (line := half_point(match[2])) == decimal(row["line"]):
            return kind, "over" if match[1].lower() == "plus" else "under", line
    return None


def team_labels(row):
    teams = json.loads(row["teams"])
    if len(teams) != 2 or any(not team.get("alias") for team in teams):
        return ()
    return tuple({normalize(team["alias"]), normalize(f"{team['alias']} {team.get('name') or ''}")}
                 for team in teams)


def poly_contract(row, labels):
    kind = TYPES.get(row["market_type"])
    outcome = row["outcome"]
    side = normalize(outcome)
    rules = row["rules"]
    if not re.search(r"will resolve to", rules, re.I):
        return None
    if kind == MONEYLINE:
        if not (re.search(r"overtime is included", rules, re.I)
                and re.search(rf'resolve to "{re.escape(outcome)}" if {re.escape(outcome)} win[s]? the game',
                              rules, re.I)):
            return None
        options = [i for i, variants in enumerate(labels) if side in variants]
        return (kind, options[0], None) if len(options) == 1 else None
    line = half_point(row["provider_line"])
    if line is None:
        return None
    if kind == TOTAL:
        match = re.fullmatch(r".+: O/U (\d+\.5)", row["question"])
        threshold = int(line + Decimal("0.5"))
        if (match and decimal(match[1]) == line and side in {"over", "under"}
                and re.search(rf'resolve to "Over" if [^.]*score {threshold} or more points\b',
                              rules, re.I)
                and re.search(rf'less than {threshold}\b[^.]*resolve to "Under"', rules, re.I)):
            return kind, side, line
    if kind == SPREAD:
        match = re.fullmatch(r"Spread: (.+) \((-\d+\.5)\)", row["question"])
        options = [i for i, variants in enumerate(labels) if side in variants]
        if match and decimal(match[2]) == line and len(options) == 1:
            favorite = [i for i, variants in enumerate(labels) if normalize(match[1]) in variants]
            threshold = int(-line + Decimal("0.5"))
            fallback = re.search(r'otherwise, this market will resolve to "([^"]+)"', rules, re.I)
            if (len(favorite) == 1 and fallback and normalize(fallback[1]) in labels[1 - favorite[0]]
                    and re.search(
                    rf'resolve to "{re.escape(match[1])}" if .+? win[s]? the game by {threshold} or more points\b',
                    rules, re.I)):
                return kind, options[0], line if options == favorite else -line
    return None


def read_gp(rows):
    fixtures = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        kickoff = timestamp(row["start_date"])
        names = tuple(normalize(name) for name in re.split(r"\s+vs\.?\s+", row["match"], flags=re.I))
        key = gp_contract(row)
        price = decimal(row["price"])
        if kickoff and len(names) == 2 and key and price is not None and price > 1:
            fixture = fixtures[kickoff][(row["event_id"], names)]
            if key in fixture and fixture[key] and fixture[key]["price"] != row["price"]:
                fixture[key] = None
            else:
                fixture.setdefault(key, row)
    return fixtures


def match_gp(labels, candidates):
    hits = []
    for (_, names), markets in candidates.items():
        for order in ((0, 1), (1, 0)):
            if all(names[order[i]] in labels[i] for i in (0, 1)):
                hits.append((order, markets))
    return hits[0] if len(hits) == 1 else (None, None)


def compare(poly_rows, gp_rows, published):
    gp = read_gp(gp_rows)
    matches, diagnostics, output = {}, Counter(), []
    for row in poly_rows:
        if row["league"] != "ncaa" or row["market_type"] not in TYPES:
            continue
        if row["event"] not in matches:
            labels = team_labels(row)
            candidates = gp.get(timestamp(row["kickoff_utc"]), {})
            matches[row["event"]] = (labels, *match_gp(labels, candidates)) if labels else ((), None, None)
        labels, order, markets = matches[row["event"]]
        if markets is None:
            diagnostics["no_unique_fixture"] += 1
            continue
        key = poly_contract(row, labels)
        if key is None:
            diagnostics["unverified_contract"] += 1
            continue
        kind, selection, line = key
        gp_row = markets.get((kind, order[selection] if isinstance(selection, int) else selection, line))
        if not gp_row:
            diagnostics["no_unique_gp_quote"] += 1
            continue
        net_cost = decimal(row["net_at_10_pusd"]) if row["status"] == "OK" else None
        gp_observed = timestamp(gp_row.get("source_observed_at"))
        poly_book = timestamp(row["book_at_utc"])
        kickoff = timestamp(row["kickoff_utc"])
        comparable_times = (gp_observed and poly_book and gp_observed < kickoff and poly_book < kickoff
                            and abs(gp_observed - poly_book) <= MAX_SKEW)
        timing = ("WITHIN_5_MIN" if comparable_times else
                  "HISTORICAL" if gp_observed else "GP_QUOTE_TIME_MISSING")
        poly_decimal = 1 / net_cost if net_cost and net_cost > 0 else None
        gp_price = decimal(gp_row["price"])
        output.append({"event": row["event"], "match": row["match"], "kickoff_utc": kickoff,
                       "market": kind, "period": "full_game", "selection": row["outcome"], "line": line,
                       "gp_event_id": gp_row["event_id"], "gp_odd_id": gp_row["odd_id"],
                       "gp_api_decimal": gp_price, "gp_observed_at_utc": gp_observed,
                       "gp_published_at_utc": published, "timing": timing,
                       "poly_asset_id": row["asset_id"], "poly_book_at_utc": poly_book,
                       "poly_observed_at_utc": row["observed_at_utc"], "poly_status": row["status"],
                       "poly_bid": row["bid"], "poly_ask": row["ask"], "poly_ask_shares": row["ask_shares"],
                       "poly_fee_rate": row["fee_rate"], "poly_decimal_net_at_10_pusd": poly_decimal,
                       "decimal_difference": poly_decimal - gp_price if poly_decimal is not None else None,
                       "settlement_scope": "COMPLETED_GAME_ONLY",
                       "poly_overtime_explicit": "overtime is included" in row["rules"].casefold(),
                       "poly_rules": row["rules"]})
    return output, diagnostics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gp", type=Path, required=True)
    parser.add_argument("--poly", type=Path, required=True)
    parser.add_argument("--gp-published-at")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    published = timestamp(args.gp_published_at)
    with args.gp.open(encoding="utf-8", newline="") as gp, args.poly.open(encoding="utf-8", newline="") as poly:
        rows, skipped = compare(csv.DictReader(poly), csv.DictReader(gp), published)
    write_csv(args.output_dir / "gp_polymarket_ncaa.csv", rows)
    counts = {"games": len({r["event"] for r in rows}), "paired_outcomes": len(rows),
              "priced_pairs": sum(r["poly_decimal_net_at_10_pusd"] is not None for r in rows),
              "synchronous_pairs": sum(r["timing"] == "WITHIN_5_MIN" and r["poly_decimal_net_at_10_pusd"] is not None
                                       for r in rows), "skipped": dict(skipped)}
    (args.output_dir / "gp_polymarket_ncaa_diagnostics.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    pub = published.astimezone(PARIS).strftime("%d/%m %H:%M %Z") if published else "inconnue"
    report = ["\n## Golden Palace ↔ Polymarket · NCAA", "",
              f"{counts['games']} matchs · {counts['paired_outcomes']} sélections appariées · "
              f"{counts['priced_pairs']} prix nets Polymarket · {counts['synchronous_pairs']} paires synchrones", "",
              f"Publication du fichier GP API · {pub}",
              "Comparaison pour un match mené à terme. Les clauses de report et d’annulation peuvent diverger. "
              "Prix nets Polymarket calculés à 10 pUSD. Le CSV indique la provenance et la fraîcheur de chaque prix.",
              ""]
    (args.output_dir / "gp_polymarket_ncaa_report.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    main()
