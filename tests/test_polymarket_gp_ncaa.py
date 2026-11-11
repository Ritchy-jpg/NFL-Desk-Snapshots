import json
import unittest
from decimal import Decimal

from polymarket_gp_ncaa import compare


class GoldenPalaceNCAATests(unittest.TestCase):
    def setUp(self):
        self.gp = {"event_id": "18080735", "odd_id": "4792351748",
                   "match": "Toledo Rockets vs. UMass Minutemen",
                   "start_date": "2026-11-12T00:30:00Z", "market_type": "spread",
                   "market_name": "Écart (prolongation incluse)", "odd_status": "0",
                   "selection": "2 (+14.5)", "line": "-14.5", "price": "1.8334"}
        self.poly = {"league": "ncaa", "event": "cfb-umass-toledo-2026-11-11",
                     "match": "UMass vs. Toledo", "kickoff_utc": "2026-11-12T00:30:00Z",
                     "market_type": "spreads", "outcome": "UMass", "provider_line": "-14.5",
                     "question": "Spread: Toledo (-14.5)",
                     "rules": ('This market will resolve to "Toledo" if Toledo win the game by 15 or more points. '
                               'Otherwise, this market will resolve to "UMass".'),
                     "teams": json.dumps([{"alias": "UMass", "name": "Minutemen"},
                                          {"alias": "Toledo", "name": "Rockets"}]),
                     "status": "OK", "net_at_10_pusd": "0.542455", "asset_id": "11",
                     "book_at_utc": "2026-11-11T17:15:31Z", "observed_at_utc": "2026-11-11T17:15:44Z",
                     "bid": "0.52", "ask": "0.53", "ask_shares": "96425.33", "fee_rate": "0.05"}

    def test_matches_reversed_order_and_actual_selection_sign(self):
        rows, _ = compare([self.poly], [self.gp], None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["line"], Decimal("14.5"))
        self.assertEqual(rows[0]["gp_odd_id"], "4792351748")
        self.assertEqual(rows[0]["timing"], "GP_QUOTE_TIME_MISSING")

    def test_rejects_conflicting_sign_period_and_kickoff(self):
        for changes in ({"line": "14.5"}, {"market_name": "Écart 1ère mi-temps"},
                        {"start_date": "2026-11-12T01:30:00Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(compare([self.poly], [{**self.gp, **changes}], None)[0], [])

    def test_rejects_ambiguous_fixture_or_price(self):
        for changes in ({"event_id": "other"}, {"price": "2.15"}):
            self.assertEqual(compare([self.poly], [self.gp, {**self.gp, **changes}], None)[0], [])

    def test_rejects_rules_with_different_threshold(self):
        row = {**self.poly, "rules": self.poly["rules"].replace("15 or more", "14 or more")}
        self.assertEqual(compare([row], [self.gp], None)[0], [])

    def test_no_net_price_for_rejected_book(self):
        rows, _ = compare([{**self.poly, "status": "STALE_BOOK"}], [self.gp], None)
        self.assertIsNone(rows[0]["poly_decimal_net_at_10_pusd"])

    def test_rejects_swapped_spread_fallback(self):
        row = {**self.poly, "rules": self.poly["rules"].replace('"UMass"', '"Toledo"')}
        self.assertEqual(compare([row], [self.gp], None)[0], [])

    def test_matches_total_and_preserves_quote_time_separately(self):
        poly = {**self.poly, "market_type": "totals", "provider_line": "51.5", "outcome": "Under",
                "question": "UMass vs. Toledo: O/U 51.5",
                "rules": ('Resolve to "Over" if both teams score 52 or more points. '
                          'If less than 52, this market will resolve to "Under".')}
        poly["rules"] = "This market will " + poly["rules"].lower()
        gp = {**self.gp, "market_type": "total", "selection": "Moins de 51.5", "line": "51.5",
              "market_name": "Total (prolongations incluses)", "source_observed_at": "2026-11-11T17:13:00Z"}
        rows, _ = compare([poly], [gp], None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timing"], "WITHIN_5_MIN")
        self.assertEqual(rows[0]["settlement_scope"], "COMPLETED_GAME_ONLY")
