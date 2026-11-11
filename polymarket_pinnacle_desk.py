"""Read-only pregame comparison of public CLOB asks to existing Pinnacle boards."""
import argparse
import csv
import json
import math
import re
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from polymarket_probe import GAMMA, CLOB, HEADERS, arr, fetch

LEAGUES = {"nfl": "nfl", "ncaa": "cfb"}
PERIODS = {
    "": ("result", ""), "first_half_": ("p1+p2", "1H"),
    "second_half_": ("p3+p4", "2H"), "q1_": ("p1", "1Q"),
    "q2_": ("p2", "2Q"), "q3_": ("p3", "3Q"), "q4_": ("p4", "4Q"),
}
MARKET_TYPES = {prefix + kind: (kind, *period)
                for prefix, period in PERIODS.items()
                for kind in ("moneyline", "spreads", "totals", "team_totals")}
MAX_SKEW = timedelta(minutes=5)
MAX_BOOK_AGE = timedelta(minutes=2)
MIN_DEPTH = 10  # collateral at the best ask
MAX_SPREAD = 0.10
MIN_EV = 0.02
GAME_SLUG = re.compile(r"(?:nfl|cfb)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}")
GAME_SPLIT = re.compile(r"\s+vs\.?\s+", re.I)
PARIS = ZoneInfo("Europe/Paris")


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


def name(value):
    words = re.findall(r"[a-z0-9]+", (value or "").lower().replace("&", " and "))
    return " ".join({"st": "state", "univ": "university"}.get(word, word) for word in words)


def team_names(team, league):
    alias, label = name(team.get("alias")), name(team.get("name"))
    full = f"{alias} {label}".strip() if league == "ncaa" else label
    variants = {alias, full}
    # Preserve the mascot when using an alias that can denote two schools.
    if league == "ncaa" and alias in {"miami", "ohio", "usc"}:
        variants.discard(alias)
    return variants - {""}


def load_board(path):
    fixtures, groups = {}, defaultdict(list)
    required = {"fixture_id", "match", "start_time", "bookmaker", "period",
                "market_type", "market_id", "market_name", "handicap",
                "bookmaker_outcome_id", "pin_snapshot_at", "observed_at",
                "mapping_status", "price_status", "is_player_selection", "price"}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f"Schéma Pinnacle incomplet : {path.name}")
        for row in reader:
            game = row["fixture_id"]
            start, seen = timestamp(row["start_time"]), timestamp(row["observed_at"])
            teams = [name(part) for part in GAME_SPLIT.split(row["match"])]
            if not game or not start or len(teams) != 2 or row["bookmaker"] != "pinnacle":
                continue
            fixture = (row["match"], teams, start)
            if game in fixtures and fixtures[game] != fixture:
                raise ValueError(f"Identité contradictoire pour {game}")
            fixtures[game] = fixture
            if (not seen or seen >= start or seen != timestamp(row["pin_snapshot_at"])
                    or row["mapping_status"] != "MAPPED"
                    or row["is_player_selection"] != "False"):
                continue
            kind, period = row["market_type"], row["period"]
            team = {"teamtotals-team1": "home", "teamtotals-team2": "away"}.get(kind, "")
            if team:
                kind = "team_totals"
            if kind not in {"moneyline", "spreads", "totals", "team_totals"}:
                continue
            line = number(row["handicap"]) if kind != "moneyline" else None
            if kind != "moneyline" and line is None:
                continue
            # The exported spread handicap is home-oriented on both sides.
            # For totals the source ID ends in over/under, for spreads in home/away.
            side = row["bookmaker_outcome_id"].split("/")[-1]
            odds = number(row["price"]) if row["price_status"] == "VALID" else None
            group = game, (kind, period, team, line), seen, row["market_id"]
            groups[group].append((side, odds, row["market_name"]))

    pairs = {}
    for (game, key, seen, market_id), rows in groups.items():
        expected = {"home", "away"} if key[0] in {"moneyline", "spreads"} else {"over", "under"}
        latest = pairs.get((game, key))
        if latest and latest["seen"] > seen:
            continue
        odds = {side: price for side, price, _ in rows}
        valid = (len(rows) == 2 and odds.keys() == expected
                 and all(price is not None and price > 1 for price in odds.values())
                 and len({row[2] for row in rows}) == 1)
        if latest and latest["seen"] == seen:
            valid = False
        pairs[game, key] = {"seen": seen, "odds": odds if valid else None,
                            "market_name": rows[0][2], "id": market_id}
    return fixtures, pairs


def match_fixture(event, fixtures, league):
    teams = event.get("teams") or []
    start = timestamp(event.get("startTime"))
    if (not start or len(teams) != 2
            or any(team.get("league") != LEAGUES[league] for team in teams)):
        return None, {}, "IDENTITE_GAMMA_INCOMPLETE"
    names = [team_names(team, league) for team in teams]
    matches = []
    for game, (_, pin_teams, pin_start) in fixtures.items():
        if abs(pin_start - start) > timedelta(minutes=60):
            continue
        for home, away in ((0, 1), (1, 0)):
            if pin_teams[0] in names[home] and pin_teams[1] in names[away]:
                matches.append((game, home, away))
    if len(matches) != 1:
        return None, {}, "MATCH_AMBIGU" if matches else "ABSENT_OU_NOM_HORAIRE_DIFFERENT"
    game, home, away = matches[0]
    labels = defaultdict(set)
    for index, side in ((home, "home"), (away, "away")):
        for value in names[index] | {name(teams[index].get("alias")), name(teams[index].get("name"))}:
            if value:
                labels[value].add(side)
    return game, {label: next(iter(sides)) for label, sides in labels.items() if len(sides) == 1}, None


def market_key(market, labels):
    spec = MARKET_TYPES.get(market.get("sportsMarketType"))
    if not spec:
        return None, [], "TYPE_HORS_PERIMETRE"
    kind, period, marker = spec
    question = (market.get("question") or "").strip()
    outcomes = arr(market.get("outcomes"))
    if len(outcomes) != 2 or any(not isinstance(x, str) for x in outcomes):
        return None, [], "ISSUES_INVALIDES"
    side_map = labels if kind in {"moneyline", "spreads"} else {"over": "over", "under": "under"}
    sides = [side_map.get(name(outcome)) for outcome in outcomes]
    if None in sides or len(set(sides)) != 2:
        return None, [], "ISSUES_INVALIDES"
    line = number(market.get("line")) if kind != "moneyline" else None
    team = ""
    if kind != "moneyline":
        if line is None or abs(line) % 1 != 0.5:
            return None, [], "LIGNE_AVEC_PUSH_OU_INVALIDE"
        if kind == "spreads":
            pattern = (f"{marker} " if marker else "") + r"Spread: (.+) \(([+-]?\d+\.5)\)"
        elif kind == "team_totals":
            pattern = r"(.+?) " + (f"{marker} " if marker else "") + r"Team Total: O/U (\d+\.5)"
        else:
            pattern = r".+: " + (f"{marker} " if marker else "") + r"O/U (\d+\.5)"
        parsed = re.fullmatch(pattern, question, flags=re.I)
        if not parsed or number(parsed.groups()[-1]) != line:
            return None, [], "LIGNE_OU_PERIODE_CONTRADICTOIRE"
        if kind in {"spreads", "team_totals"}:
            team = labels.get(name(parsed[1]))
            if not team:
                return None, [], "EQUIPE_DE_LIGNE_INCONNUE"
            if kind == "spreads":
                line = -line if team == "away" else line
                team = ""
    return (kind, period, team, line), sides, None


def settlement(market, key, league, pin_name):
    """Allow normal completed-game scoring only. Exceptional voids differ."""
    kind, period, _, line = key
    rules = (market.get("description") or "").lower()
    scoring = re.split(r"if this market.s outcome was definitively|if the game is postponed|"
                       r"if the game is canceled", rules, maxsplit=1)[0]
    if (kind == "moneyline" and (league == "nfl" or period != "result")):
        return "EGALITE_50_50_VS_PUSH"
    if "will resolve" not in scoring:
        return "REGLEMENT_NON_CONFIRME"
    if period == "result":
        # The game-total and team-total templates use final game points,
        # sometimes without an explicit overtime sentence.
        if ("overtime" not in pin_name.lower()
                or re.search(r"regulation|overtime (?:is )?not|excluding overtime|first half|second half",
                             scoring)
                or ("overtime is included" not in scoring
                    and not (kind in {"totals", "team_totals"} and "in this game" in scoring)
                    and not (kind == "spreads" and league == "ncaa" and "win the game by" in scoring))):
            return "PROLONGATION_NON_CONFIRMEE"
    elif period == "p3+p4":
        if "any overtime count" not in scoring or "overtime" not in pin_name.lower():
            return "PROLONGATION_NON_CONFIRMEE"
    else:
        period_label = {"p1+p2": "first half", "p1": "first quarter", "p2": "second quarter",
                        "p3": "third quarter", "p4": "fourth quarter"}[period]
        if period_label not in scoring or "overtime is not included" not in scoring:
            return "PERIODE_NON_CONFIRMEE"
    if kind in {"totals", "team_totals"}:
        threshold = str(math.ceil(line))
        if (not re.search(r"\b" + threshold + r" or more points", scoring)
                or not re.search(r"less than " + threshold + r"\b", scoring)):
            return "SEUIL_REGLEMENT_NON_CONFIRME"
    if kind == "spreads" and not re.search(
            r"\b" + str(math.ceil(abs(line))) + r" or more points", scoring):
        return "SEUIL_REGLEMENT_NON_CONFIRME"
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


def economics(ask, rate, fair, tick):
    cost = ask + rate * ask * (1 - ask)
    lower, upper = 0.0, fair
    for _ in range(55):
        price = (lower + upper) / 2
        if price + rate * price * (1 - price) <= fair / (1 + MIN_EV):
            lower = price
        else:
            upper = price
    step = Decimal(str(tick))
    ticks = (Decimal(str(lower)) / step).to_integral_value(rounding=ROUND_FLOOR)
    buy_price = float(ticks * step)
    return {"cout_net": cost, "decimal_net": 1 / cost, "ev": fair / cost - 1,
            "buy_price_max": buy_price}


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
            if (event.get("gameId") and event.get("slug", "").startswith(LEAGUES[league] + "-")
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
    """Use the dated fixture slug and merge same-game sibling market inventories."""
    games = defaultdict(list)
    for event in events:
        games[event["gameId"]].append(event)
    result = []
    for siblings in games.values():
        parents = [event for event in siblings if GAME_SLUG.fullmatch(event["slug"])]
        # Multiple dated events with one game ID remain visible for review.
        for parent in parents:
            markets = {market["id"]: market for market in parent.get("markets") or []}
            if len(parents) == 1:
                for sibling in siblings:
                    if (sibling["slug"].startswith(parent["slug"] + "-")
                            and timestamp(sibling.get("startTime")) == timestamp(parent["startTime"])):
                        for market in sibling.get("markets") or []:
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


def best_level(levels, best):
    valid = [(number(row.get("price")), number(row.get("size"))) for row in levels]
    valid = [(price, size) for price, size in valid
             if price is not None and 0 < price < 1 and size is not None and size > 0]
    if not valid:
        return None, None
    price = best(p for p, _ in valid)
    return price, sum(size for p, size in valid if p == price)


def evaluate(row, market, book, start, pin, side):
    if not book or book.get("market") != market.get("conditionId"):
        return "CARNET_ABSENT_OU_CONDITION_DIFFERENTE"
    observed = timestamp(book.get("observed_at"))
    stamp = number(book.get("timestamp"))
    book_time = datetime.fromtimestamp(stamp / 1000, timezone.utc) if stamp and 0 < stamp < 4e12 else None
    ask, size = best_level(book.get("asks") or [], min)
    bid, _ = best_level(book.get("bids") or [], max)
    rate, tick = fee_rate(market), number(book.get("tick_size"))
    minimum_size = number(book.get("min_order_size"))
    row.update(poly_observed_at=observed, book_timestamp=book_time, ask=ask, bid=bid,
               ask_size=size, fee_rate=rate, tick=tick, min_order_size=minimum_size)
    if not observed or not book_time or not timedelta(seconds=-5) <= observed - book_time <= MAX_BOOK_AGE:
        return "HORODATAGE_CARNET_INVALIDE"
    if observed >= start or book_time >= start:
        return "COTATION_APRES_COUP_ENVOI"
    if ask is None:
        return "AUCUN_ASK"
    row["ask_verifie"] = True
    row["ask_depth"] = ask * size
    row["ecart_secondes"] = (observed - pin["seen"]).total_seconds()
    if rate is None or tick not in {0.1, 0.01, 0.001, 0.0001}:
        return "FRAIS_OU_TICK_INCONNUS"
    if (bid is None or not 0 <= ask - bid <= MAX_SPREAD + 1e-12
            or ask * size < MIN_DEPTH or not minimum_size or size < minimum_size):
        return "LIQUIDITE_INSUFFISANTE"
    odds = pin["odds"]
    fair = (1 / odds[side]) / sum(1 / price for price in odds.values())
    result = economics(ask, rate, fair, tick)
    if abs(observed - pin["seen"]) > MAX_SKEW:
        row.update({"historique_" + key: value for key, value in result.items()})
        return "PINNACLE_DESYNCHRONISE"
    row.update(result)
    return "REVIEW_EV_POSITIVE" if result["buy_price_max"] >= ask else "PASS_PRIX"


def compare(league, events, fixtures, pairs, books, now):
    coverage, matched, anomalies, rows = [], [], Counter(), []
    joined = [match_fixture(event, fixtures, league) for event in events]
    duplicates = Counter(game for game, _, _ in joined if game)
    for event, (game, labels, error) in zip(events, joined):
        if game and duplicates[game] > 1:
            game, error = None, "PLUSIEURS_EVENEMENTS_POLYMARKET"
        slug = event["slug"]
        start = timestamp(event["startTime"])
        card = {"league": league, "event": slug, "start": start, "fixture_id": game,
                "match": event.get("title") or slug, "anomalie": error,
                "gamma_markets": len(event.get("markets") or []), "exact_markets": 0,
                "priced_markets": 0}
        if game:
            card["pin_kickoff"] = fixtures[game][2]
            card["kickoff_gap_seconds"] = (start - fixtures[game][2]).total_seconds()
        coverage.append(card)
        if error:
            anomalies[error] += 1
            continue
        market_issues = Counter()
        for market in event.get("markets") or []:
            if not (market.get("active") is True and market.get("closed") is False
                    and market.get("acceptingOrders") is True):
                market_issues["MARCHE_NON_OUVERT"] += 1
                continue
            key, sides, error = market_key(market, labels)
            pin = pairs.get((game, key))
            if not error and not pin:
                error = "LIGNE_EXACTE_ABSENTE"
            if not error and not pin["odds"]:
                error = "PAIRE_PINNACLE_INCOMPLETE_OU_AMBIGUE"
            if not error:
                error = settlement(market, key, league, pin["market_name"])
            market_start = timestamp(market.get("gameStartTime"))
            tokens = arr(market.get("clobTokenIds"))
            if not error and (market_start != start or len(tokens) != 2 or tokens[0] == tokens[1]
                              or not all(isinstance(token, str) and token.isdigit() for token in tokens)
                              or not market.get("conditionId")):
                error = "CONTRAT_OU_HORAIRE_NON_CONFIRME"
            if error:
                market_issues[error] += 1
                continue
            card["exact_markets"] += 1
            matched.append((card, event, market, key, pin, sides, tokens))
        anomalies.update(market_issues)
        card["market_anomalies"] = json.dumps(market_issues, ensure_ascii=False)
    if books is None:
        tokens = sorted({token for match in matched for token in match[-1]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            chunks = (tokens[i:i + 100] for i in range(0, len(tokens), 100))
            books = {}
            for batch, error in pool.map(fetch_books, chunks):
                books.update(batch)
                if error:
                    anomalies["ERREUR_LOT_CLOB"] += 1
                    print(f"Erreur lot CLOB {league}: {error}")
    for card, event, market, key, pin, sides, tokens in matched:
        both_verified = []
        for outcome, side, token in zip(arr(market["outcomes"]), sides, tokens):
            odds = pin["odds"]
            fair = (1 / odds[side]) / sum(1 / price for price in odds.values())
            line = -key[3] if key[0] == "spreads" and side == "away" else key[3]
            row = {"league": league, "event": event["slug"], "fixture_id": card["fixture_id"],
                   "kickoff": card["start"], "market_id": market["id"],
                   "pin_market_id": pin["id"], "condition_id": market["conditionId"],
                   "token_id": token, "question": market["question"], "selection": outcome,
                   "market_type": key[0], "period": key[1], "line": line,
                   "pin_observed_at": pin["seen"], "pin_decimal": odds[side],
                   "pin_opposite": next(v for s, v in odds.items() if s != side),
                   "pin_fair": fair, "pin_fair_decimal": 1 / fair, "ask_verifie": False,
                   "settlement_scope": "MATCH_TERMINE_NORMALEMENT"}
            cutoff = min(card["start"], fixtures[card["fixture_id"]][2])
            row["status"] = evaluate(row, market, books.get(token), cutoff, pin, side)
            both_verified.append(row["ask_verifie"])
            rows.append(row)
        card["priced_markets"] += int(all(both_verified))
    covered = {row["fixture_id"] for row in coverage if row["fixture_id"]}
    for game, (title, _, start) in fixtures.items():
        if game not in covered and start > now:
            coverage.append({"league": league, "fixture_id": game, "match": title,
                             "start": start, "anomalie": "ABSENT_COTE_POLYMARKET",
                             "gamma_markets": 0, "exact_markets": 0, "priced_markets": 0})
    return coverage, rows, anomalies


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as target:
        if fields:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def report(coverage, rows, diagnostics, source, now):
    priced = [row for row in rows if row["ask_verifie"]]
    live = [row for row in rows if "ev" in row]
    historical = [row for row in rows if "historique_ev" in row]
    review = [row for row in live if row["status"] == "REVIEW_EV_POSITIVE"]
    lines = ["# Polymarket ↔ Pinnacle",
             f"{now.astimezone(PARIS):%d/%m/%Y %H:%M %Z} · Pré-match · Lecture seule",
             f"Source Pinnacle `{source}` · Horizon {diagnostics['days']} jours",
             "EV conditionnelle au règlement normal. Les clauses d’annulation, report ou "
             "abandon peuvent différer. BUY PRICE = ask maximal pour viser +2 % d’EV après frais.",
             f"**{len(priced)} asks vérifiés · {len(live)} EV synchrones · "
             f"{len(historical)} EV historiques · {len(review)} à revoir**"]
    for league in LEAGUES:
        games = [card for card in coverage if card["league"] == league]
        both = [card for card in games if card.get("event") and card["fixture_id"]]
        lines.extend(["", f"## {league.upper()} · {len(both)} rencontres communes",
                      f"Polymarket {sum(bool(card.get('event')) for card in games)} · "
                      f"Pinnacle {sum(bool(card['fixture_id']) for card in games)} · "
                      f"{sum(card['exact_markets'] for card in games)} marchés exacts · "
                      f"{sum(card['priced_markets'] for card in games)} avec deux asks vérifiés", ""])
        sample = sorted(games, key=lambda card: (not bool(card["exact_markets"]), card["start"]))
        for card in sample[:8]:
            kickoff = card["start"].astimezone(PARIS).strftime("%d/%m %H:%M")
            detail = card["anomalie"] or f"{card['exact_markets']} exacts / {card['priced_markets']} cotés"
            lines.append(f"- {kickoff} · {card['match']} · {detail}")
        if len(sample) > 8:
            lines.append(f"- {len(sample) - 8} autres rencontres dans coverage.csv.")
    examples = sorted(review, key=lambda row: row["ev"], reverse=True)[:10] or live[:4]
    if not examples:
        for league in LEAGUES:
            candidates = [row for row in historical if row["league"] == league]
            examples.extend(sorted(candidates, key=lambda row: abs(row["historique_ev"]))[:2])
    if examples:
        lines.extend(["", "## Prix et EV" if live else "## Exemples historiques · WATCH", ""])
        for row in examples:
            prefix = "" if "ev" in row else "historique_"
            poly_time = row["poly_observed_at"].astimezone(PARIS).strftime("%d/%m %H:%M:%S")
            pin_time = row["pin_observed_at"].astimezone(PARIS).strftime("%d/%m %H:%M:%S")
            lines.append(f"- **{row['question']} · {row['selection']}** · ask {row['ask']:.3f} "
                         f"({poly_time}) · Pinnacle {row['pin_decimal']:.3f} / {row['pin_opposite']:.3f} "
                         f"({pin_time}) · fair {row['pin_fair']:.2%} · EV {row[prefix + 'ev']:+.2%} "
                         f"· BUY PRICE ≤ {row[prefix + 'buy_price_max']:.3f} "
                         f"· frais {row['fee_rate']:.1%} (coefficient) · profondeur {row['ask_depth']:.2f} pUSD")
    issues = Counter(row["status"] for row in rows if "ev" not in row)
    for counts in diagnostics["anomalies"].values():
        issues.update(counts)
    lines.extend(["", "## Anomalies", ""] +
                 [f"- {issue} · {count}" for issue, count in issues.most_common()])
    for league, error in diagnostics["errors"].items():
        lines.append(f"- {league.upper()} · {error}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-dir", type=Path, default=Path("output"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source", required=True, help="commit SHA des boards")
    parser.add_argument("--days", type=int, default=10)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days doit être positif")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    diagnostics = {"days": args.days, "source": args.source, "as_of": now,
                   "anomalies": {}, "errors": {}}
    coverage, rows = [], []
    try:
        sports = fetch(GAMMA, "/sports", {})
        series = {entry["sport"]: entry["series"] for entry in sports
                  if entry.get("sport") in LEAGUES.values() and entry.get("series")}
    except (OSError, ValueError, KeyError) as error:
        series = {}
        diagnostics["errors"]["gamma"] = str(error)
    for league, sport in LEAGUES.items():
        try:
            fixtures, pairs = load_board(args.board_dir / f"pinnacle_{league}_board.csv")
            horizon = now + timedelta(days=args.days)
            fixtures = {game: fixture for game, fixture in fixtures.items() if now < fixture[2] <= horizon}
            events = discover(league, series[sport], now, args.days)
            cards, result, anomalies = compare(league, events, fixtures, pairs, None, now)
            coverage.extend(cards)
            rows.extend(result)
            diagnostics["anomalies"][league] = dict(anomalies)
            if fixtures and (not result or not any(row["ask_verifie"] for row in result)):
                diagnostics["errors"][league] = "Aucun marché exact avec ask vérifié malgré un board Pinnacle."
            if anomalies["ERREUR_LOT_CLOB"]:
                diagnostics["errors"][league] = "Carnets CLOB incomplets."
        except (OSError, ValueError, KeyError) as error:
            diagnostics["errors"][league] = str(error)
    verified = [row for row in rows if row["ask_verifie"]]
    diagnostics.update(
        event_pairs=sum(bool(card.get("event") and card["fixture_id"]) for card in coverage),
        market_pairs=len(rows) // 2, verified_asks=len(verified),
        synchronous_ev=sum("ev" in row for row in rows),
        historical_ev=sum("historique_ev" in row for row in rows),
        reviews=sum(row["status"] == "REVIEW_EV_POSITIVE" for row in rows),
        statuses=dict(Counter(row["status"] for row in rows)))
    write_csv(args.output_dir / "coverage.csv", coverage)
    write_csv(args.output_dir / "comparison.csv", rows)
    (args.output_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, default=str, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.output_dir / "report.md").write_text(
        report(coverage, rows, diagnostics, args.source, now), encoding="utf-8")
    print(json.dumps(diagnostics, default=str, ensure_ascii=False))
    return int(bool(diagnostics["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
