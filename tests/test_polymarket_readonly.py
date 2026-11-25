"""Offline checks: python -m unittest discover -s tests -p 'test_polymarket_readonly.py'."""
import unittest
from decimal import Decimal
from polymarket_readonly import plain, sports_candidate


class CollectorTests(unittest.TestCase):
    def test_decimal_is_not_float(self):
        self.assertEqual(plain({"ask": Decimal("0.590000")}), {"ask": "0.590000"})

    def test_candidate_is_only_hint(self):
        self.assertTrue(sports_candidate({"question": "NFL champion?"}))
        self.assertFalse(sports_candidate({"question": "Will it rain tomorrow?"}))


if __name__ == "__main__":
    unittest.main()
