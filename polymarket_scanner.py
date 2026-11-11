"""Archive public pregame Polymarket books for NFL and NCAA."""
import argparse
import csv
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LEAGUES = {"nfl": "nfl", "ncaa": "cfb"}
GAME_SLUG = re.compile(r"(?:nfl|cfb)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}")
PARIS = ZoneInfo("Europe/Paris")
MAX_BOOK_AGE = timedelta(minutes=2)
MAX_PAIR_SKEW = timedelta(seconds=30)
LEVELS = 10
DEPTH_BUDGET = 10.0  # pUSD, including fees

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


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def fee_rate(market):
    schedule = market.get("feeSchedule") or {}
    rate = number(schedule.get("rate"))
    if market.get("feesEnabled") is False and rate in (None, 0):
        return 0.0
    if (market.get("feesEnabled") is True and schedule.get("exponent") == 1
            and schedule.get("takerOnly") is True and rate is not None and 0 <= rate <= 1):
        return rate
    return None


def discover(league, series, now, days):
    params = {"series_id": series, "closed": "false", "limit": 50,
              "start_time_min": now.isoformat(),
              "start_time_max": (now + timedelta(days=days)).isoformat()}
    events, cursors = {}, set()
    while True:
        page = fetch(GAMMA, "/events/keyset", params)
        if not isinstance(page.get("events"), list):
            raise ValueError("Réponse Gamma sans liste d’événements")
        for event in page["events"]:
            start = timestamp(event.get("startTime"))
            if ((event.get("gameId") or event.get("parentEventId"))
                    and event.get("slug", "").startswith(LEAGUES[league] + "-")
                    and start and now < start <= now + timedelta(days=days)
                    and not event.get("live") and not event.get("ended")):
                events[event["id"]] = event
        cursor = page.get("next_cursor")
        if not cursor:
            return canonical_events(events.values())
        if cursor in cursors:
            raise ValueError(f"Curseur Gamma répété pour {league}")
        cursors.add(cursor)
        params["after_cursor"] = cursor


def canonical_events(events):
    """Merge prop events by parent ID, including children without a game ID."""
    parents, children, siblings = {}, defaultdict(list), defaultdict(list)
    for event in events:
        parent_id = event.get("parentEventId")
        if event.get("gameId") and GAME_SLUG.fullmatch(event["slug"]):
            parents[str(event["id"])] = event
        elif parent_id:
            children[str(parent_id)].append(event)
        elif event.get("gameId"):
            siblings[event["gameId"]].append(event)
    games = Counter(parent["gameId"] for parent in parents.values())
    result = []
    for parent_id, parent in parents.items():
        markets = {market["id"]: market for market in parent.get("markets") or []}
        candidates = list(children[parent_id])
        if games[parent["gameId"]] == 1:
            candidates.extend(siblings[parent["gameId"]])
        for child in candidates:
            if (child["slug"].startswith(parent["slug"] + "-")
                    and timestamp(child.get("startTime")) == timestamp(parent["startTime"])):
                for market in child.get("markets") or []:
                    markets.setdefault(market["id"], market)
        result.append({**parent, "markets": list(markets.values())})
    return result


def fetch_books(tokens):
    # POST /books is a public batch read. No trading endpoints or credentials.
    request = urllib.request.Request(CLOB + "/books", headers={**HEADERS, "Content-Type": "application/json"},
                                     data=json.dumps([{"token_id": token} for token in tokens]).encode())
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            books = json.load(response)
        if not isinstance(books, list):
            raise ValueError("Réponse CLOB sans liste de carnets")
        observed = utcnow().isoformat()
        return {str(book["asset_id"]): {**book, "observed_at": observed} for book in books}, None
    except (OSError, ValueError, KeyError) as error:
        return {}, str(error)


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as target:
        if fields:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def contracts(events):
    for event in events:
        kickoff = timestamp(event.get("startTime"))
        for market in event.get("markets") or []:
            if (market.get("active") is not True or market.get("closed") is not False
                    or market.get("acceptingOrders") is not True):
                continue
            if not kickoff or timestamp(market.get("gameStartTime")) != kickoff:
                continue
            outcomes = arr(market.get("outcomes"))
            ids = arr(market.get("positionIds" if market.get("version") == "v2" else "clobTokenIds"))
            if (len(outcomes) != 2 or not all(isinstance(x, str) and x.strip() for x in outcomes)
                    or outcomes[0] == outcomes[1] or len(ids) != 2
                    or not all(isinstance(x, str) and x.isdigit() for x in ids)
                    or ids[0] == ids[1] or not market.get("id") or not market.get("conditionId")
                    or not market.get("description") or not market.get("sportsMarketType")):
                continue
            yield event, market, outcomes, ids


def order_levels(entries, reverse=False):
    sizes = defaultdict(float)
    for entry in entries:
        price, size = number(entry.get("price")), number(entry.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            sizes[price] += size
    return sorted(sizes.items(), reverse=reverse)[:LEVELS]


def cost_for_budget(asks, rate, minimum, budget=DEPTH_BUDGET):
    remaining, shares = budget, 0.0
    for price, size in asks:
        net = price + rate * price * (1 - price)
        spent = min(remaining, size * net)
        shares += spent / net
        remaining -= spent
        if remaining <= 1e-9:
            return (shares, budget / shares) if shares >= minimum else (None, None)
    return None, None


def quote(book, market, asset_id, kickoff):
    if not book or str(book.get("asset_id")) != asset_id or book.get("market") != market["conditionId"]:
        return {"status": "NO_VERIFIED_BOOK"}
    observed = timestamp(book.get("observed_at"))
    stamp = number(book.get("timestamp"))
    book_at = datetime.fromtimestamp(stamp / 1000, timezone.utc) if stamp and 0 < stamp < 4e12 else None
    asks = order_levels(book.get("asks") or [])
    bids = order_levels(book.get("bids") or [], reverse=True)
    ask, ask_size = asks[0] if asks else (None, None)
    bid, bid_size = bids[0] if bids else (None, None)
    minimum = number(book.get("min_order_size"))
    tick = number(book.get("tick_size"))
    rate = fee_rate(market)
    row = {"observed_at_utc": observed, "book_at_utc": book_at, "bid": bid, "ask": ask,
           "bid_shares": bid_size, "ask_shares": ask_size, "fee_rate": rate, "tick": tick,
           "minimum_shares": minimum, "asks": json.dumps(asks), "bids": json.dumps(bids)}
    if not observed or not book_at or not timedelta(seconds=-5) <= observed - book_at <= MAX_BOOK_AGE:
        return {**row, "status": "STALE_BOOK"}
    if max(observed, book_at) >= kickoff:
        return {**row, "status": "AFTER_KICKOFF"}
    if bid is None or ask is None or not 0 <= ask - bid <= 0.10 + 1e-12:
        return {**row, "status": "WIDE_OR_INCOMPLETE_BOOK"}
    if rate is None or not minimum or not tick:
        return {**row, "status": "MISSING_EXECUTION_PARAMETERS"}
    shares, net = cost_for_budget(asks, rate, minimum)
    if shares is None:
        return {**row, "status": "THIN_BOOK"}
    return {**row, "status": "OK", "shares_at_10_pusd": shares, "net_at_10_pusd": net,
            "decimal_at_10_pusd": 1 / net}


def reference(quotes):
    first, second = quotes
    if any(q["status"] != "OK" for q in quotes):
        return None, "BOOK_FILTER"
    if abs(first["book_at_utc"] - second["book_at_utc"]) > MAX_PAIR_SKEW:
        return None, "BOOKS_OUT_OF_SYNC"
    low = max(first["bid"], 1 - second["ask"])
    high = min(first["ask"], 1 - second["bid"])
    if low > high + 1e-12:
        return None, "CONFLICTING_BOOKS"
    high = max(high, low)
    return ((low, high), (1 - high, 1 - low)), "OK"


def analyze(league, catalog, books):
    for event, market, outcomes, ids in catalog:
        kickoff = timestamp(event["startTime"])
        quotes = [quote(books.get(asset_id), market, asset_id, kickoff) for asset_id in ids]
        band, status = reference(quotes)
        for index, (outcome, asset_id, book) in enumerate(zip(outcomes, ids, quotes)):
            row = {"league": league, "event": event["slug"], "match": event["title"],
                   "kickoff_utc": kickoff, "market_type": market["sportsMarketType"],
                   "market_id": market["id"], "condition_id": market["conditionId"],
                   "asset_id": asset_id, "question": market.get("question"), "outcome": outcome,
                   "provider_line": market.get("line"), "rules": market["description"],
                   "reference_status": status, **book}
            if band:
                row["reference_low"], row["reference_high"] = band[index]
            yield row


def read_books(catalog):
    ids = sorted({asset_id for contract in catalog for asset_id in contract[-1]})
    batches = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    books, errors = {}, []
    with ThreadPoolExecutor(max_workers=6) as executor:
        for batch, (result, error) in zip(batches, executor.map(fetch_books, batches)):
            books.update(result)
            if error:
                errors.append(f"Lot de {len(batch)} issues · {error}")
    return books, errors


def report(rows, diagnostics, now):
    lines = ["# Polymarket NFL et NCAA", "",
             f"{now.astimezone(PARIS):%d/%m/%Y %H:%M %Z} · Pré-match · Lecture seule", "",
             "Les coûts nets utilisent un budget de référence de 10 pUSD par issue.", ""]
    for league, counts in diagnostics.items():
        lines += [f"## {league.upper()}", "",
                  f"{counts['games']} matchs · {counts['markets']} marchés · "
                  f"{counts['quoted_outcomes']} issues avec coût net · "
                  f"{counts['reference_markets']} fourchettes de référence", ""]
        for kind in ("moneyline", "spreads", "totals"):
            sample = next((r for r in rows if r["league"] == league and r["market_type"] == kind
                           and r["reference_status"] == "OK"), None)
            if sample:
                price = sample["net_at_10_pusd"]
                observed = sample["observed_at_utc"].astimezone(PARIS)
                lines.append(f"- {sample['question']} · {sample['outcome']} · ask {sample['ask']:.3f}"
                             f" · net {price:.4f} · {observed:%H:%M:%S %Z}")
        lines.extend(f"- Collecte · {error}" for error in counts["errors"][:5])
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--days", type=int, choices=range(1, 15), default=7)
    args = parser.parse_args()
    now = utcnow()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sports = fetch(GAMMA, "/sports", {})
    series = {entry["sport"]: entry["series"] for entry in sports if entry.get("series")}
    rows, diagnostics = [], {}
    for league, sport in LEAGUES.items():
        events, catalog = [], []
        try:
            events = discover(league, series[sport], now, args.days)
            catalog = list(contracts(events))
            books, errors = read_books(catalog)
            league_rows = list(analyze(league, catalog, books))
        except (OSError, ValueError, KeyError) as error:
            league_rows, errors = [], [str(error)]
        statuses = Counter(row["status"] for row in league_rows)
        if events and not statuses["OK"]:
            errors.append("Matchs détectés, sans coût net vérifié.")
        diagnostics[league] = {
            "games": len(events), "markets": len(catalog),
            "games_with_quotes": len({r["event"] for r in league_rows if r["status"] == "OK"}),
            "quoted_outcomes": statuses["OK"],
            "reference_markets": sum(r["reference_status"] == "OK" for r in league_rows) // 2,
            "quote_status": dict(statuses),
            "market_types": dict(Counter(m["sportsMarketType"] for _, m, _, _ in catalog)),
            "errors": errors,
        }
        rows.extend(league_rows)
    write_csv(args.output_dir / "polymarket_quotes.csv", rows)
    (args.output_dir / "diagnostics.json").write_text(
        json.dumps({"started_at_utc": now.isoformat(), "leagues": diagnostics}, indent=2),
        encoding="utf-8")
    (args.output_dir / "report.md").write_text(report(rows, diagnostics, now), encoding="utf-8")
    return int(any(counts["errors"] for counts in diagnostics.values()))


if __name__ == "__main__":
    raise SystemExit(main())
