# Polymarket public-data prototype

This isolated branch does **not** modify existing NFL/NCAA jobs or submit orders.

## Run locally

```bash
python -m pip install -r requirements-polymarket.txt
python polymarket_readonly.py --max-markets 200 --output polymarket_snapshot.json
python -m unittest discover -s tests -p 'test_polymarket_readonly.py'
```

The collector uses the official Python `AsyncPublicClient`, paginates active
markets, and writes a bounded sample. The text-based candidate filter is **not
exhaustive** and does not establish league identity, event matching, or market
equivalence. All candidates remain UNMATCHED and ineligible for EV.

## Production gates (not implemented)

1. Discover NFL and NCAA events via official sports metadata, not keyword search.
2. Normalize teams, event start times, outcomes, settlement terms and line values.
3. Retrieve CLOB best ask/bid and size, reject stale/illiquid books, and account for fees.
4. Compare only exactly equivalent markets against timestamped GP/Pinnacle prices.
5. Add fixture-based tests, network smoke tests, bounded retries and rate-limit handling.
6. Schedule independently after passing gates; preserve existing paid-API quotas.

No credentials, secure client, trading or geographic restrictions bypass.
