"""Step 1 — Data fetch & sanity.

See docs/V1_SPEC.md §2. Pulls 5 years of daily price history, volume, and
basic profile from FMP (Financial Modeling Prep — our data vendor),
caches the result to disk at data/cache/{TICKER}.json, and runs four
sanity checks.

Sanity check severities:
  - freshness     HARD FAIL if last bar > 5 trading days old.
                  Running on a frozen tape is worse than not running.
  - completeness  HARD FAIL if > 5% of expected trading days are missing;
                  WARN if there's any multi-day gap.
  - split_div     WARN if any close-to-close return exceeds 30% in
                  absolute value. No auto-fix — humans review the flagged
                  dates because real catastrophes (earnings disasters,
                  M&A) can also produce moves that large.
  - volume        WARN if zero-volume bars exceed 5% of the window, or
                  if ANY negative-volume bars are present (yes, these
                  appear in vendor feeds sometimes).

CLI smoke test (run from repo root with FMP_API_KEY exported):
    python -m src.pipeline.data NVDA
    python -m src.pipeline.data NVDA --refresh
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

import pandas_market_calendars as mcal
import requests
import yaml

from src import config
from src.data_sources import fmp

# NYSE calendar — same holiday set as NASDAQ for our purposes, and it
# tracks ad-hoc closures (e.g. Hurricane Sandy, Jan 9 2025 for President
# Carter). Loaded once at import time.
NYSE_CAL = mcal.get_calendar("XNYS")

CACHE_DIR = config.DATA_DIR / "cache"

# Thresholds — all sourced from config/thresholds.yml so we can calibrate
# without code changes (see src/config.py).
FETCH_TIMEOUT_SEC = config.THRESHOLDS.engine.fetch_timeout_sec
HISTORY_YEARS = config.THRESHOLDS.engine.history_years
FRESHNESS_HARD_FAIL_DAYS = config.THRESHOLDS.data_sanity.freshness_hard_fail_days
# Cache TTL: cache entries older than this force a fresh fetch. See
# the thresholds.yml comment for the why behind 18 hours.
CACHE_TTL_HOURS = getattr(config.THRESHOLDS.data_sanity, "cache_ttl_hours", 18)
COMPLETENESS_HARD_FAIL_RATIO = config.THRESHOLDS.data_sanity.completeness_hard_fail_ratio
SPLIT_DIV_WARN_RETURN = config.THRESHOLDS.data_sanity.split_div_warn_return
VOLUME_WARN_RATIO = config.THRESHOLDS.data_sanity.volume_warn_ratio

_SEVERITY_RANK = {"ok": 0, "warn": 1, "fail": 2}


# ---------- public API ----------


def fetch(ticker: str, refresh: bool = False) -> dict:
    """Fetch ticker data from FMP (or cache), run sanity checks, return payload.

    Returns a dict with keys: ticker, as_of, profile, prices, sanity,
    source ("fmp" | "cache:fresh"), fetched_at. Schema documented at
    the top of this module.

    Cache behavior: if a cache file exists AND was written less than
    CACHE_TTL_HOURS hours ago, the cached payload is returned with
    source="cache:fresh". Otherwise, a fresh FMP fetch happens. This
    is critical: without the TTL, the nightly cron would serve the
    previous day's prices forever because the cache file always
    exists after the first run.

    Pass refresh=True to force a fresh fetch regardless of cache age
    (used by smoke tests and manual probes).
    """
    cache_path = CACHE_DIR / f"{ticker}.json"

    if not refresh and cache_path.exists():
        cached = _load_cache(cache_path)
        if cached is not None and _cache_is_fresh(cached):
            cached["source"] = "cache:fresh"
            return cached
        # Cache exists but is stale (> TTL old) — fall through to
        # fresh fetch. Old cache file gets overwritten below.

    profile = _fetch_profile(ticker)
    prices = _fetch_prices(ticker)
    sanity = _run_sanity(prices)

    result = {
        "ticker": ticker,
        "as_of": prices[-1]["date"],
        "profile": profile,
        "prices": prices,
        "sanity": sanity,
        "source": "fmp",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w") as f:
        json.dump(result, f, indent=2)

    return result


def _load_cache(cache_path) -> dict | None:
    try:
        with cache_path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _cache_is_fresh(cached: dict) -> bool:
    """True iff the cache's fetched_at timestamp is within the TTL.
    Stale or missing timestamp -> False (forces fresh fetch)."""
    ts = cached.get("fetched_at")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - when
        return age < timedelta(hours=CACHE_TTL_HOURS)
    except ValueError:
        return False


# ---------- FMP fetchers (HTTP layer lives in src.data_sources.fmp) ----------


def _fetch_profile(ticker: str) -> dict:
    body = fmp.get("profile", ticker)
    if not body:
        raise RuntimeError(f"FMP returned empty profile for {ticker}")
    p = body[0] if isinstance(body, list) else body
    market_cap = p.get("marketCap") or p.get("mktCap")
    price = p.get("price")
    shares = p.get("sharesOutstanding")
    if not shares and market_cap and price:
        shares = market_cap / price
    return {
        "company_name": p.get("companyName"),
        "sector": p.get("sector"),
        "industry": p.get("industry"),
        "exchange": p.get("exchangeShortName") or p.get("exchange"),
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "currency": p.get("currency"),
        "is_etf": p.get("isEtf"),
        "is_active": p.get("isActivelyTrading"),
    }


def _fetch_prices(ticker: str) -> list[dict]:
    from_date = (date.today() - timedelta(days=int(HISTORY_YEARS * 365.25))).isoformat()
    # Stable endpoint name based on FMP's current convention. If this
    # returns 404, tools/probe_fmp.py will tell us the correct name.
    body = fmp.get("historical-price-eod/full", ticker, from_=from_date)
    # Stable can return either a bare list or {"historical": [...]} —
    # handle both shapes defensively.
    if isinstance(body, list):
        historical = body
    elif isinstance(body, dict):
        historical = body.get("historical") or body.get("data") or []
    else:
        historical = []
    if not historical:
        raise RuntimeError(f"FMP returned no historical prices for {ticker}")
    # FMP returns newest-first; reverse to chronological.
    return [
        {
            "date": row["date"],
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "adj_close": row.get("adjClose") or row.get("close"),
            "volume": row.get("volume"),
        }
        for row in reversed(historical)
    ]


# ---------- sanity checks ----------


def _trading_days_in_range(start: date, end: date) -> int:
    """Count actual NYSE trading days in [start, end] inclusive. Returns 0
    if start > end. Holidays and ad-hoc closures are correctly excluded."""
    if start > end:
        return 0
    sched = NYSE_CAL.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return len(sched)


def _run_sanity(prices: list[dict]) -> dict:
    freshness = _check_freshness(prices)
    completeness = _check_completeness(prices)
    split_div = _check_split_div(prices)
    volume = _check_volume(prices)
    overall = max(
        (freshness, completeness, split_div, volume),
        key=lambda r: _SEVERITY_RANK[r["status"]],
    )["status"]
    return {
        "freshness": freshness,
        "completeness": completeness,
        "split_div": split_div,
        "volume": volume,
        "overall": overall,
    }


def _check_freshness(prices: list[dict]) -> dict:
    last = date.fromisoformat(prices[-1]["date"])
    today = date.today()
    # Count trading days strictly between last bar and today.
    gap = _trading_days_in_range(last + timedelta(days=1), today - timedelta(days=1))
    status = "fail" if gap > FRESHNESS_HARD_FAIL_DAYS else "ok"
    return {
        "status": status,
        "trading_days_old": gap,
        "last_bar": prices[-1]["date"],
        "message": f"last bar {gap} trading day(s) old (threshold {FRESHNESS_HARD_FAIL_DAYS})",
    }


def _check_completeness(prices: list[dict]) -> dict:
    first = date.fromisoformat(prices[0]["date"])
    last = date.fromisoformat(prices[-1]["date"])
    expected = _trading_days_in_range(first, last)
    actual = len(prices)
    missing = max(expected - actual, 0)

    gaps: list[dict] = []
    for i in range(1, len(prices)):
        a = date.fromisoformat(prices[i - 1]["date"])
        b = date.fromisoformat(prices[i]["date"])
        between = _trading_days_in_range(a + timedelta(days=1), b - timedelta(days=1))
        if between > 0:
            gaps.append(
                {
                    "from": a.isoformat(),
                    "to": b.isoformat(),
                    "missing_trading_days": between,
                }
            )

    if expected > 0 and missing / expected > COMPLETENESS_HARD_FAIL_RATIO:
        status = "fail"
    elif gaps:
        status = "warn"
    else:
        status = "ok"

    return {
        "status": status,
        "expected_bars": expected,
        "actual_bars": actual,
        "missing": missing,
        "gap_count": len(gaps),
        "gaps": gaps[:10],
        "message": (
            f"{actual} bars, {missing} missing of {expected} expected trading days, "
            f"{len(gaps)} gap(s) > 0 trading days"
        ),
    }


def _check_split_div(prices: list[dict]) -> dict:
    flagged: list[dict] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1].get("adj_close") or prices[i - 1].get("close")
        cur = prices[i].get("adj_close") or prices[i].get("close")
        if not prev or not cur or prev <= 0:
            continue
        ret = (cur - prev) / prev
        if abs(ret) > SPLIT_DIV_WARN_RETURN:
            flagged.append(
                {
                    "date": prices[i]["date"],
                    "prev_close": prev,
                    "close": cur,
                    "return_pct": round(ret * 100, 2),
                }
            )

    status = "warn" if flagged else "ok"
    return {
        "status": status,
        "flagged_count": len(flagged),
        "flagged": flagged[:10],
        "message": (
            f"{len(flagged)} bar(s) with |return| > {int(SPLIT_DIV_WARN_RETURN*100)}% "
            "— review for unadjusted corporate action vs real move"
            if flagged
            else "no implausibly large single-day moves"
        ),
    }


def _check_volume(prices: list[dict]) -> dict:
    zero_vol_count = 0
    negative_vol_dates: list[str] = []
    for p in prices:
        v = p.get("volume")
        if v is None or v == 0:
            zero_vol_count += 1
        elif v < 0:
            negative_vol_dates.append(p["date"])
            zero_vol_count += 1

    ratio = zero_vol_count / len(prices) if prices else 0
    status = "warn" if (negative_vol_dates or ratio > VOLUME_WARN_RATIO) else "ok"
    return {
        "status": status,
        "zero_vol_count": zero_vol_count,
        "zero_vol_ratio": round(ratio, 4),
        "negative_vol_dates": negative_vol_dates[:10],
        "message": (
            f"{zero_vol_count} zero/missing-volume bar(s) of {len(prices)} "
            f"({ratio*100:.2f}%); {len(negative_vol_dates)} negative-volume"
        ),
    }


# ---------- CLI smoke test ----------


_SEV_LABEL = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}


def _load_tier(ticker: str) -> str | None:
    if not config.WATCHLIST_PATH.exists():
        return None
    with config.WATCHLIST_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    entry = data.get(ticker)
    return entry.get("tier") if isinstance(entry, dict) else None


def _format_money(n) -> str:
    if n is None:
        return "?"
    for unit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= unit:
            return f"${n / unit:.2f}{suffix}"
    return f"${n:.0f}"


def _print_summary(result: dict) -> None:
    t = result["ticker"]
    p = result["profile"]
    prices = result["prices"]
    s = result["sanity"]
    tier = _load_tier(t)

    print(
        f"{t}  tier={tier or '?'}  sector={p.get('sector') or '?'}  "
        f"market_cap={_format_money(p.get('market_cap'))}"
    )
    print(
        f"  exchange={p.get('exchange') or '?'}  active={p.get('is_active')}  "
        f"name={p.get('company_name')}"
    )
    print(f"  prices: {len(prices)} bars  {prices[0]['date']} → {prices[-1]['date']}")
    print(f"  sanity:")
    for check_name in ("freshness", "completeness", "split_div", "volume"):
        c = s[check_name]
        print(f"    {_SEV_LABEL[c['status']]}  {check_name:<13} {c['message']}")
    print(f"  overall: {_SEV_LABEL[s['overall']]}")
    cache_path = CACHE_DIR / f"{t}.json"
    print(f"  cache:   {cache_path.relative_to(config.REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="FMP fetch + sanity for one ticker.")
    parser.add_argument("ticker", help="ticker symbol, e.g. NVDA")
    parser.add_argument(
        "--refresh", action="store_true", help="force re-fetch (ignore cache)"
    )
    args = parser.parse_args()

    try:
        result = fetch(args.ticker.upper(), refresh=args.refresh)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"ERROR: network/HTTP error fetching {args.ticker}: {e}", file=sys.stderr)
        return 2

    _print_summary(result)
    return 0 if result["sanity"]["overall"] != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
