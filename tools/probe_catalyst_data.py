"""Diagnostic dumper for FMP catalyst-related endpoints.

When catalyst.py produces surprising output (a ticker missing earnings,
analyst grades all classified as "held", etc.), the root cause is
almost always that FMP's actual response shape differs from what our
parser expected. This tool dumps the raw responses for a ticker so we
can see exactly what FMP returns and fix parsing assumptions.

Usage:
    export FMP_API_KEY=...
    python tools/probe_catalyst_data.py NVDA
    python tools/probe_catalyst_data.py NVDA AMAT IONQ

For each ticker, dumps:
  1. earnings-calendar response — focused on entries matching the
     ticker (and the first 3 raw entries so we can see symbol format)
  2. grades response — first 5 entries with all fields
  3. price-target-consensus response

Output goes to stdout as JSON. Pipe through `| head -200` or `| less`
if it's noisy.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running as `python tools/probe_catalyst_data.py` from repo root
# (without this, Python's sys.path starts at tools/ and can't see src/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_sources import fmp  # noqa: E402
from src.data_sources.fmp import redact  # noqa: E402


def probe_earnings_calendar(ticker: str) -> None:
    """Dump earnings-calendar entries matching this ticker plus a few
    raw samples to show the symbol-field format FMP actually uses."""
    print(f"\n=== {ticker} :: earnings-calendar ===")
    from_date = (date.today() - timedelta(days=360)).isoformat()
    to_date = (date.today() + timedelta(days=90)).isoformat()
    print(f"  query window: from={from_date}  to={to_date}")
    try:
        body = fmp.get("earnings-calendar", symbol=None, from_=from_date, to=to_date)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {redact(e)}")
        return

    if not isinstance(body, list):
        print(f"  unexpected response shape: {type(body).__name__}")
        print(f"  raw: {json.dumps(body, indent=2)[:500]}")
        return

    print(f"  total events in window: {len(body)}")
    # Show a few raw samples to see field names + symbol format
    print(f"\n  first 3 raw entries (any ticker — shows shape):")
    for e in body[:3]:
        print(f"    {json.dumps(e, indent=4)}")

    # Find entries that look like they might be for this ticker
    candidates = [
        e for e in body
        if ticker.upper() in (e.get("symbol") or "").upper()
    ]
    print(f"\n  entries with '{ticker}' as substring of symbol field: {len(candidates)}")
    for e in candidates[:5]:
        print(f"    {json.dumps(e, indent=4)}")


def probe_grades(ticker: str) -> None:
    """Dump the first 5 grade-change entries with all fields, so we
    can see what action-field name (if any) FMP uses and what
    previousGrade/newGrade values look like."""
    print(f"\n=== {ticker} :: grades ===")
    try:
        body = fmp.get("grades", ticker)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {redact(e)}")
        return

    if not isinstance(body, list):
        print(f"  unexpected response shape: {type(body).__name__}")
        print(f"  raw: {json.dumps(body, indent=2)[:500]}")
        return

    print(f"  total entries: {len(body)}")
    print(f"\n  first 5 raw entries (FULL fields — shows action field name):")
    for g in body[:5]:
        print(f"    {json.dumps(g, indent=4)}")

    # Summarize all distinct keys present across entries
    all_keys: set[str] = set()
    for g in body:
        all_keys.update(g.keys())
    print(f"\n  all distinct keys across {len(body)} entries: {sorted(all_keys)}")

    # Tally how often each action-like key has a non-empty value
    for key in ("action", "gradeAction", "gradeChange"):
        nonempty = sum(1 for g in body if g.get(key))
        if nonempty > 0:
            sample_values = sorted({str(g.get(key)) for g in body[:20] if g.get(key)})
            print(f"  '{key}' present in {nonempty}/{len(body)} entries — sample values: {sample_values}")


def probe_price_target_consensus(ticker: str) -> None:
    print(f"\n=== {ticker} :: price-target-consensus ===")
    try:
        body = fmp.get("price-target-consensus", ticker)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {redact(e)}")
        return
    print(f"  raw: {json.dumps(body, indent=2)}")


# ---- ALTERNATIVE ENDPOINTS (probing for replacements) ----
#
# earnings-calendar is paginated at 4000 events and clips off events
# before the latest 4000. grades only returns "maintain" entries on
# Starter. We need replacements. Probe candidate endpoints.


_ALTERNATIVE_ENDPOINTS = [
    # ---- earnings replacements ----
    # FMP commonly has a per-symbol earnings endpoint that bypasses
    # the calendar's pagination cap entirely.
    ("earnings", {}),
    ("earnings", {"limit": 20}),
    ("historical-earning-calendar", {"limit": 20}),
    ("earnings-surprises", {}),
    ("earnings-transcript-latest", {}),
    # ---- analyst-rating-change replacements ----
    # The reiterations-only result on /grades suggests upgrades and
    # downgrades live on a different endpoint name.
    ("upgrades-downgrades", {}),
    ("upgrades-downgrades-consensus", {}),
    ("grades-historical", {}),
    ("grades-latest-news", {}),
    ("stock-grade-news", {}),
    ("analyst-stock-recommendations", {}),
    ("rating-news", {}),
]


def probe_alternative_endpoints(ticker: str) -> None:
    """For each candidate endpoint, attempt the call and report:
      - HTTP success vs error (with status code on 4xx)
      - response shape (list/dict, length)
      - first entry's fields and a sample value
    Failures are non-fatal — we want to discover what works on Starter."""
    print(f"\n=== {ticker} :: ALTERNATIVE ENDPOINT PROBES ===")
    for endpoint, extra in _ALTERNATIVE_ENDPOINTS:
        label = endpoint + (f"?{','.join(f'{k}={v}' for k, v in extra.items())}" if extra else "")
        print(f"\n  --- /{label} ---")
        try:
            body = fmp.get(endpoint, ticker, **extra)
        except Exception as e:  # noqa: BLE001
            msg = redact(str(e))
            # Truncate the verbose RuntimeError text so the report stays scannable
            if len(msg) > 200:
                msg = msg[:200] + "..."
            print(f"    ERROR: {msg}")
            continue
        if isinstance(body, list):
            print(f"    OK — list of {len(body)} entries")
            if body:
                print(f"    keys in first entry: {sorted(body[0].keys())}")
                print(f"    first entry: {json.dumps(body[0], indent=6)}")
        elif isinstance(body, dict):
            print(f"    OK — dict with keys: {sorted(body.keys())}")
            print(f"    raw: {json.dumps(body, indent=6)[:500]}")
        else:
            print(f"    OK — unexpected type {type(body).__name__}: {body!r:.200}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="ticker symbols to probe")
    args = parser.parse_args()
    for t in args.tickers:
        t = t.upper()
        probe_earnings_calendar(t)
        probe_grades(t)
        probe_price_target_consensus(t)
        probe_alternative_endpoints(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
