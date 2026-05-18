"""Snapshot persistence — JSON read/write to data/snapshots/{TICKER}/.

See docs/V1_SPEC.md §7. Each nightly run writes one JSON snapshot per
ticker at `data/snapshots/{TICKER}/{YYYY-MM-DD}.json` containing every
numeric output of the pipeline. The next run loads recent snapshots to
compute Bayesian-smoothed conviction trajectories.

This module is pure I/O — no smoothing or analytics yet (that arrives
when there are several days of accumulated history to smooth across).

Snapshot shape (excerpt; full schema grows as pipeline steps come online):

    {
      "ticker": "NVDA",
      "run_date": "2026-05-18",
      "as_of": "2026-05-16",          # last price bar date
      "tier_anchor": "A",             # tier from watchlist.yml
      "data": {"status": "ok"|"fail", ...},
      "tier_classifier": {"status": "ok", ...},
      "regime": {"status": "pending"|"ok"|"fail", ...},
      "catalyst": {"status": "pending", ...},
      ...
      "verdict": {"aidy": {...}, "jesse": {...}}
    }

CLI smoke test:
    python -m src.snapshot NVDA           # list snapshots
    python -m src.snapshot NVDA latest    # print latest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src import config


def write(payload: dict) -> Path:
    """Write a snapshot to disk. Returns the path written.

    Required keys in payload: 'ticker', 'run_date' (YYYY-MM-DD string).
    Overwrites any existing file for that (ticker, run_date) pair — by
    design, since a same-day re-run should reflect the latest state, not
    accumulate duplicates.
    """
    ticker = payload["ticker"]
    run_date = payload["run_date"]
    ticker_dir = config.SNAPSHOTS_DIR / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    path = ticker_dir / f"{run_date}.json"
    with path.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path


def load(ticker: str, run_date: str) -> dict | None:
    """Load one specific snapshot, or None if it doesn't exist."""
    path = config.SNAPSHOTS_DIR / ticker / f"{run_date}.json"
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


def load_latest(ticker: str) -> dict | None:
    """Load the most recent snapshot for this ticker, or None if none
    exist yet. 'Most recent' is by filename (lexical = chronological for
    YYYY-MM-DD)."""
    ticker_dir = config.SNAPSHOTS_DIR / ticker
    if not ticker_dir.exists():
        return None
    files = sorted(ticker_dir.glob("*.json"))
    if not files:
        return None
    with files[-1].open() as f:
        return json.load(f)


def load_recent(ticker: str, n_days: int) -> list[dict]:
    """Load up to the last N snapshots for this ticker, oldest first.
    Returns empty list if no snapshots exist yet."""
    ticker_dir = config.SNAPSHOTS_DIR / ticker
    if not ticker_dir.exists():
        return []
    files = sorted(ticker_dir.glob("*.json"))[-n_days:]
    out: list[dict] = []
    for path in files:
        with path.open() as f:
            out.append(json.load(f))
    return out


def list_dates(ticker: str) -> list[str]:
    """List all snapshot dates on disk for this ticker, oldest first."""
    ticker_dir = config.SNAPSHOTS_DIR / ticker
    if not ticker_dir.exists():
        return []
    return [p.stem for p in sorted(ticker_dir.glob("*.json"))]


# ---------- CLI smoke test ----------


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect ticker snapshots on disk.")
    parser.add_argument("ticker", help="ticker symbol, e.g. NVDA")
    parser.add_argument(
        "mode",
        nargs="?",
        default="list",
        choices=("list", "latest"),
        help="'list' = print available dates; 'latest' = print the most recent snapshot as JSON",
    )
    args = parser.parse_args()
    ticker = args.ticker.upper()

    if args.mode == "list":
        dates = list_dates(ticker)
        if not dates:
            print(f"no snapshots on disk for {ticker}")
            return 0
        print(f"{ticker}: {len(dates)} snapshot(s)")
        for d in dates:
            print(f"  {d}")
        return 0

    latest = load_latest(ticker)
    if latest is None:
        print(f"no snapshots on disk for {ticker}", file=sys.stderr)
        return 1
    print(json.dumps(latest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
