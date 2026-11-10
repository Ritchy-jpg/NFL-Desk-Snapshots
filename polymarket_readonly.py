"""Polymarket public SDK collector (read-only, no orders or credentials).

Install: pip install polymarket-client
Run: python polymarket_readonly.py --max-markets 200 --output polymarket_snapshot.json

Candidate discovery is deliberately conservative; unmatched markets are never
treated as executable bookmaker comparisons.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from polymarket import AsyncPublicClient, PolymarketError, RateLimitError


def plain(value):
    """Serialize typed SDK models without converting Decimal prices to float."""
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(x) for x in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def sports_candidate(market):
    """Discovery hint, NOT a verified NFL/NCAA classification."""
    data = json.dumps(market, ensure_ascii=False, default=str).lower()
    return any(term in data for term in (
        "nfl", "ncaa", "college football", "super bowl", "touchdown",
        "quarterback", "football"
    ))


async def collect(max_markets, page_size):
    checked = 0
    candidates = []
    async with AsyncPublicClient() as client:
        pages = client.list_markets(closed=False, page_size=page_size)
        async for page in pages:
            for market in page.items:
                checked += 1
                record = plain(market)
                if sports_candidate(record):
                    candidates.append({
                        "market": record,
                        "match_status": "UNMATCHED",
                        "quote_status": "NOT_FETCHED",
                        "eligible_for_ev": False,
                    })
                if checked >= max_markets:
                    break
            if checked >= max_markets:
                break
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Polymarket official Python SDK / public client",
        "read_only": True,
        "markets_scanned": checked,
        "scan_complete": False,
        "note": "Bounded sample; candidate discovery is not exhaustive. No CLOB quote or EV is asserted.",
        "candidates": candidates,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-markets", type=int, default=200)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--output", default="polymarket_snapshot.json")
    args = parser.parse_args()
    if not 1 <= args.max_markets <= 10000 or not 1 <= args.page_size <= 100:
        parser.error("max-markets must be 1..10000 and page-size 1..100")
    try:
        result = asyncio.run(collect(args.max_markets, args.page_size))
    except RateLimitError as exc:
        parser.exit(2, f"Polymarket rate limit: {exc}\n")
    except PolymarketError as exc:
        parser.exit(2, f"Polymarket SDK error: {exc}\n")
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scanned {result['markets_scanned']} markets; {len(result['candidates'])} candidates; no EV calculated")


if __name__ == "__main__":
    main()
