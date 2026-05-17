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
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests
import yaml

from src import config

FMP_BASE = "https://financialmodelingprep.com/api/v3"
FETCH_TIMEOUT_SEC = 30
HISTORY_YEARS = 5

CACHE_DIR = config.DATA_DIR / "cache"

FRESHNESS_HARD_FAIL_DAYS = 5
COMPLETENESS_HARD_FAIL_RATIO = 0.05
SPLIT_DIV_WARN_RETURN = 0.30
VOLUME_WARN_RATIO = 0.05

_SEVERITY_RANK = {"ok": 0, "warn": 1, "fail": 2}


# ---------- public API ----------


def fetch(ticker: str, refresh: bool = False) -> dict:
    """Fetch ticker data from FMP (or cache), run sanity checks, return payload.

    Returns a dict with keys: ticker, as_of, profile, prices, sanity,
    source, fetched_at. Schema documented at the top of this module.
    """
    cache_path = CACHE_DIR / f"{ticker}.json"

    if not refresh and cache_path.exists():
        with cache_path.open() as f:
            return json.load(f)

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FMP_API_KEY not set in environment. Export it locally "
            "(`export FMP_API_KEY=...`) or set it as a GitHub Actions "
            "secret for cron runs."
        )

    profile = _fetch_profile(ticker, api_key)
    prices = _fetch_prices(ticker, api_key)
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


# ---------- FMP HTTP ----------


def _fetch_profile(ticker: str, api_key: str) -> dict:
    url = f"{FMP_BASE}/profile/{ticker}"
    resp = requests.get(url, params={"apikey": api_key}, timeout=FETCH_TIMEOUT_SEC)
    resp.raise_for_status()
    body = resp.json()
    if not body:
        raise RuntimeError(f"FMP returned empty profile for {ticker}")
    p = body[0]
    market_cap = p.get("mktCap")
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


def _fetch_prices(ticker: str, api_key: str) -> list[dict]:
    from_date = (date.today() - timedelta(days=int(HISTORY_YEARS * 365.25))).isoformat()
    url = f"{FMP_BASE}/historical-price-full/{ticker}"
    resp = requests.get(
        url,
        params={"apikey": api_key, "from": from_date},
        timeout=FETCH_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    body = resp.json()
    historical = body.get("historical", []) if isinstance(body, dict) else []
    if not historical:
        raise RuntimeError(f"FMP returned no historical prices for {ticker}")
    return [
        {
            "date": row["date"],
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "adj_close": row.get("adjClose"),
            "volume": row.get("volume"),
        }
        for row in reversed(historical)  # FMP returns newest-first; reverse to chronological
    ]


# ---------- sanity checks ----------


def _count_weekdays(d1: date, d2: date) -> int:
    """Count Mon-Fri days in [d1, d2] inclusive. Holidays not subtracted (v1)."""
    if d1 > d2:
        return 0
    n = 0
    cur = d1
    while cur <= d2:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


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
    # Trading days strictly between last bar and today (exclusive both ends).
    gap = _count_weekdays(last + timedelta(days=1), today - timedelta(days=1))
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
    expected = _count_weekdays(first, last)
    actual = len(prices)
    missing = max(expected - actual, 0)

    gaps: list[dict] = []
    for i in range(1, len(prices)):
        a = date.fromisoformat(prices[i - 1]["date"])
        b = date.fromisoformat(prices[i]["date"])
        if (b - a).days > 4:  # weekend = 3 days; > 4 means at least one missing trading day
            gaps.append(
                {"from": a.isoformat(), "to": b.isoformat(), "calendar_days": (b - a).days}
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
            f"{actual} bars, {missing} missing of {expected} expected weekdays, "
            f"{len(gaps)} gap(s) > 1 trading day"
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
