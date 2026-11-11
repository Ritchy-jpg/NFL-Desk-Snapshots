import csv
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket_pinnacle_desk import (
    canonical_events, compare, discover, economics, fee_rate, load_board, market_key, match_fixture, timestamp,
)

START = timestamp("2026-11-11T00:00:00Z")
OBSERVED = START - timedelta(minutes=30)
GAME = "id1402765370899052"
EVENT = {
    "id": "1303967", "gameId": 70899052, "slug": "cfb-kentst-bowlgr-2026-11-10",
    "title": "Kent State vs. Bowling Green", "startTime": START.isoformat(),
    "teams": [{"alias": "Kent State", "name": "Golden Flashes", "league": "cfb"},
              {"alias": "Bowling Green", "name": "Falcons", "league": "cfb"}],
}
MARKET = {
    "id": "1", "question": "Kent State vs. Bowling Green: O/U 41.5", "line": 41.5,
    "outcomes": '["Over","Under"]', "clobTokenIds": '["11","12"]',
    "conditionId": "fixture-condition", "gameStartTime": START.isoformat(),
    "sportsMarketType": "totals", "active": True, "closed": False, "acceptingOrders": True,
    "description": 'This market will resolve to "Over" if Kent State and Bowling Green '
                   'combine to score 42 or more points in this game. If the combined total '
                   'is less than 42, this market will resolve to "Under". '
                   'If the game is canceled it will resolve 50-50.',
    "feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
}
BOARD = Path(__file__).resolve().parents[1] / "output/pinnacle_ncaa_board.csv"


def book(ask, observed=OBSERVED):
    return {"market": MARKET["conditionId"], "observed_at": observed.isoformat(),
            "timestamp": str(int(observed.timestamp() * 1000)),
            "asks": [{"price": str(ask), "size": "200"}],
            "bids": [{"price": str(ask - 0.01), "size": "100"}],
            "tick_size": "0.01", "min_order_size": "5"}


class DeskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures, cls.pairs = load_board(BOARD)

    def test_original_kent_match_reversed_and_total_exact(self):
        game, labels, error = match_fixture(EVENT, self.fixtures, "ncaa")
        self.assertIsNone(error)
        self.assertEqual(game, GAME)
        self.assertEqual(labels["kent state"], "away")
        key, _, error = market_key(MARKET, labels)
        self.assertIsNone(error)
        self.assertEqual(self.pairs[game, key]["odds"]["over"], 1.735)

    def test_away_handicap_orientation_and_push(self):
        _, labels, _ = match_fixture(EVENT, self.fixtures, "ncaa")
        spread = {**MARKET, "sportsMarketType": "spreads", "line": -3.5,
                  "outcomes": ["Bowling Green", "Kent State"],
                  "question": "Spread: Kent State (-3.5)"}
        self.assertEqual(market_key(spread, labels)[:2],
                         (("spreads", "result", "", 3.5), ["home", "away"]))
        self.assertEqual(market_key({**spread, "line": -3}, labels)[2],
                         "LIGNE_AVEC_PUSH_OU_INVALIDE")

    def test_stale_real_board_pair_never_produces_live_ev(self):
        cards, rows, _ = compare("ncaa", [{**EVENT, "markets": [MARKET]}],
                                 self.fixtures, self.pairs, {"11": book(0.48), "12": book(0.53)},
                                 OBSERVED)
        self.assertEqual(cards[0]["exact_markets"], 1)
        self.assertEqual(cards[0]["priced_markets"], 1)
        self.assertTrue(all("ev" not in row and "historique_ev" in row for row in rows))
        self.assertAlmostEqual(sum(row["pin_fair"] for row in rows), 1)

    def test_synchronous_ev_and_liquidity(self):
        pairs = {(game, key): {**pair, "seen": OBSERVED}
                 for (game, key), pair in self.pairs.items()}
        illiquid = book(0.53)
        illiquid["asks"][0]["size"] = "6"
        _, rows, _ = compare("ncaa", [{**EVENT, "markets": [MARKET]}], self.fixtures, pairs,
                             {"11": book(0.48), "12": illiquid}, OBSERVED)
        self.assertEqual(rows[0]["status"], "REVIEW_EV_POSITIVE")
        self.assertGreater(rows[0]["ev"], 0.02)
        self.assertEqual(rows[1]["status"], "LIQUIDITE_INSUFFISANTE")
        self.assertNotIn("ev", rows[1])

    def test_fee_and_buy_price_tick(self):
        self.assertEqual(fee_rate(MARKET), 0.05)
        self.assertIsNone(fee_rate({**MARKET, "feeSchedule": {"rate": 0.05}}))
        result = economics(0.50, 0.05, 0.55, 0.01)
        self.assertAlmostEqual(result["cout_net"], 0.5125)
        self.assertEqual(result["buy_price_max"], 0.52)
        self.assertGreaterEqual(economics(0.52, 0.05, 0.55, 0.01)["ev"], 0.02)
        self.assertLess(economics(0.53, 0.05, 0.55, 0.01)["ev"], 0.02)

    def test_incomplete_pair_does_not_use_unrelated_side(self):
        with BOARD.open(newline="") as source:
            reader = csv.DictReader(source)
            candidates = [row for row in reader if row["fixture_id"] == GAME
                          and row["market_type"] == "totals" and row["period"] == "result"
                          and row["handicap"] == "41.5"]
        self.assertEqual(len(candidates), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.csv"
            with path.open("w", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
                writer.writeheader()
                writer.writerow(candidates[0])
                writer.writerow({**candidates[1], "market_id": "different"})
            _, pairs = load_board(path)
        self.assertIsNone(pairs[GAME, ("totals", "result", "", 41.5)]["odds"])

    def test_same_day_team_match_requires_both_names_and_kickoff(self):
        wrong_start = {**EVENT, "startTime": (START + timedelta(hours=2)).isoformat()}
        self.assertIsNone(match_fixture(wrong_start, self.fixtures, "ncaa")[0])
        duplicate = {**self.fixtures, "duplicate": self.fixtures[GAME]}
        self.assertEqual(match_fixture(EVENT, duplicate, "ncaa")[2], "MATCH_AMBIGU")

    def test_sibling_markets_count_as_one_fixture(self):
        parent = {**EVENT, "markets": [MARKET]}
        child = {**EVENT, "slug": EVENT["slug"] + "-exact-margin",
                 "markets": [MARKET, {**MARKET, "id": "2"}]}
        self.assertEqual(canonical_events([child, parent]),
                         [{**EVENT, "markets": [MARKET, {**MARKET, "id": "2"}]}])

    def test_keyset_pagination(self):
        with patch("polymarket_pinnacle_desk.fetch",
                   side_effect=[{"events": [], "next_cursor": "cursor"}, {"events": [EVENT]}]):
            self.assertEqual(discover("ncaa", "12756", OBSERVED, 1), [{**EVENT, "markets": []}])


if __name__ == "__main__":
    unittest.main()
