import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import polymarket_scanner as scanner


class PolymarketScannerTests(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2026, 11, 13, 1, 15, tzinfo=timezone.utc)
        self.observed = self.kickoff - timedelta(minutes=2)
        self.market = {
            "id": "1", "conditionId": "fixture", "active": True, "closed": False,
            "acceptingOrders": True, "gameStartTime": self.kickoff.isoformat(),
            "outcomes": '["Over","Under"]', "clobTokenIds": '["11","12"]',
            "description": "Fixture rules", "sportsMarketType": "receiving_yards",
            "line": 49.5, "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.03, "takerOnly": True},
        }
        self.book = {
            "asset_id": "11", "market": "fixture", "observed_at": self.observed.isoformat(),
            "timestamp": int((self.observed - timedelta(seconds=1)).timestamp() * 1000),
            "asks": [{"price": "0.53", "size": "50"}, {"price": "0.50", "size": "2"}],
            "bids": [{"price": "0.47", "size": "50"}],
            "min_order_size": "5", "tick_size": "0.01",
        }

    def test_quote_uses_multiple_levels_and_fees(self):
        quote = scanner.quote(self.book, self.market, "11", self.kickoff)
        first_price = 0.50 + 0.03 * 0.50 * 0.50
        second_price = 0.53 + 0.03 * 0.53 * 0.47
        shares = 2 + (10 - 2 * first_price) / second_price
        self.assertEqual(quote["status"], "OK")
        self.assertEqual(quote["ask"], 0.50)
        self.assertAlmostEqual(quote["shares_at_10_pusd"], shares)
        self.assertAlmostEqual(quote["net_at_10_pusd"], 10 / shares)

    def test_rejects_wrong_token_stale_shallow_and_live_books(self):
        cases = [
            ({"asset_id": "12"}, "NO_VERIFIED_BOOK"),
            ({"timestamp": int((self.observed - timedelta(minutes=3)).timestamp() * 1000)}, "STALE_BOOK"),
            ({"asks": [{"price": "0.50", "size": "2"}]}, "THIN_BOOK"),
            ({"observed_at": self.kickoff.isoformat(),
              "timestamp": int(self.kickoff.timestamp() * 1000)}, "AFTER_KICKOFF"),
        ]
        for changes, status in cases:
            with self.subTest(status=status):
                self.assertEqual(scanner.quote({**self.book, **changes}, self.market, "11",
                                               self.kickoff)["status"], status)

    def test_unknown_fees_prevent_net_price(self):
        market = {**self.market, "feeSchedule": {}}
        quote = scanner.quote(self.book, market, "11", self.kickoff)
        self.assertEqual(quote["status"], "MISSING_EXECUTION_PARAMETERS")
        self.assertNotIn("net_at_10_pusd", quote)

    def test_keeps_props_and_uses_correct_ids(self):
        v2 = {**self.market, "version": "v2", "positionIds": ["21", "22"]}
        event = {"startTime": self.kickoff.isoformat(), "markets": [self.market, v2]}
        self.assertEqual([row[-1] for row in scanner.contracts([event])],
                         [["11", "12"], ["21", "22"]])

    def test_discovery_follows_pagination_and_merges_sibling_props(self):
        parent = {"id": "1", "gameId": 1, "slug": "nfl-was-nyg-2026-11-13",
                  "startTime": self.kickoff.isoformat(), "markets": []}
        sibling = {**parent, "id": "2", "slug": parent["slug"] + "-more-markets",
                   "markets": [self.market]}
        pages = [{"events": [parent], "next_cursor": "next"}, {"events": [sibling]}]
        with patch.object(scanner, "fetch", side_effect=pages) as fetch:
            events = scanner.discover("nfl", "1", self.observed, 7)
        self.assertEqual(events[0]["markets"], [self.market])
        self.assertEqual(fetch.call_args.args[2]["after_cursor"], "next")

    def test_reference_requires_synchronous_consistent_books(self):
        first = scanner.quote(self.book, self.market, "11", self.kickoff)
        second = {**first, "bid": 0.50, "ask": 0.54}
        band, status = scanner.reference([first, second])
        self.assertEqual(status, "OK")
        self.assertAlmostEqual(band[0][0], 0.47)
        self.assertAlmostEqual(band[0][1], 0.50)
        second["book_at_utc"] -= timedelta(seconds=31)
        self.assertEqual(scanner.reference([first, second])[1], "BOOKS_OUT_OF_SYNC")
