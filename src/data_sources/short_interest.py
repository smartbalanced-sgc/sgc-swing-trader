"""Short interest data fetcher with investor-grade failover.

Short interest (open short position as % of float) is one of the
catalyst-panel risk signals — see the v17 dip engine's
"Short interest: 1.47% of float (stale 2026-04-15)" pattern. FMP
Starter doesn't provide this data at all (confirmed via FMP support
2026-05-18 — see docs/FMP_ENDPOINTS.md). Finnhub's free tier excludes
it ($12+/mo for paid). Other providers (Polygon, Quiver) are similar
or pricier.

Yahoo Finance (via the unofficial `yfinance` library) DOES provide it
for free, but yfinance can be rate-limited / blocked on shared CI
infrastructure (GitHub Actions, AWS) because Yahoo's anti-bot detection
sees too much traffic from those IP ranges. This module mitigates that
with a hardened HTTP session + cache + retry + multi-layer failover.

Failover chain when the primary call fails:

  1. yfinance (primary) with:
       - curl_cffi TLS-fingerprint impersonation (browser-realistic)
       - 24-hour local disk cache
       - Retry with exponential backoff + jitter
       - Inter-ticker delay to avoid burst patterns
  2. Stale cache up to 14 days old. Short interest publishes biweekly,
     so a 7-day-stale value is still informative. Dashboard renders
     with explicit "stale, N days old" marker.
  3. FMP float-size proxy — qualitative "low / medium / high float"
     label per FMP support's documented workaround. Loses precise %
     but preserves the squeeze-risk qualitative signal. Requires
     `current_float_shares` arg to be passed in.
  4. Marked unavailable with the failure reason logged. Dashboard
     renders "short interest unavailable today (reason)".

Every successful result carries a `source` field for honest data
provenance — the dashboard can show "yfinance, fetched 2 hours ago"
vs "stale (12 days), yfinance unavailable" vs "FMP float-proxy
fallback".

Public API:
  fetch(ticker, current_float_shares=None) -> dict
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

# Cache config — short interest publishes biweekly so 24-hour cache is
# generous. Stale-but-usable window is 14 days (covers one publish cycle).
CACHE_DIR = config.DATA_DIR / "cache" / "short_interest"
CACHE_FRESH_HOURS = 24
CACHE_STALE_DAYS = 14

# yfinance retry config — 3 attempts at 10s/30s/90s with jitter.
RETRY_DELAYS_SEC = (10, 30, 90)
INTER_TICKER_DELAY_SEC = 5  # space requests when fetching for multiple tickers


# ---------- public API ----------


def fetch(ticker: str, current_float_shares: float | None = None) -> dict:
    """Get short-interest data for one ticker via the failover chain.

    Args:
        ticker: ticker symbol (US-listed equity).
        current_float_shares: optional, used for the Layer-3 float-proxy
            fallback. If not provided, fallback skips to Layer 4.

    Returns a dict with consistent shape regardless of which layer
    answered:

        {
            "ticker": "NVDA",
            "source": str,                 # see _SOURCE_* constants
            "fetched_at": ISO timestamp,
            "short_percent_of_float": float | None,
            "short_ratio_days_to_cover": float | None,
            "shares_short": int | None,
            "shares_short_prior_month": int | None,
            "as_of_date": "YYYY-MM-DD" | None,
            "trend_month_over_month_pct": float | None,
            "stale_days": int | None,      # how old the data is in days
            "float_proxy_label": str | None,   # only set for Layer 3
            "failure_reason": str | None,  # only set when source == unavailable
        }

    Caller does NOT need to handle exceptions — fetch always returns a
    well-formed dict, with `source` indicating provenance. Treat
    source == "unavailable" as "no data this run, show pending in UI."
    """
    # Layer 1 — fresh cache hit (avoid Yahoo entirely if recent)
    cached = _load_cache(ticker)
    if cached and _cache_is_fresh(cached):
        cached["source"] = _SOURCE_CACHE_FRESH
        cached["stale_days"] = _cache_age_days(cached)
        return cached

    # Layer 1 — try yfinance fresh fetch
    yf_result = _try_yfinance(ticker)
    if yf_result is not None:
        _save_cache(ticker, yf_result)
        return yf_result

    # Layer 2 — stale cache (acceptable if under CACHE_STALE_DAYS old)
    if cached and _cache_age_days(cached) <= CACHE_STALE_DAYS:
        cached["source"] = _SOURCE_CACHE_STALE
        cached["stale_days"] = _cache_age_days(cached)
        return cached

    # Layer 3 — FMP float-proxy qualitative fallback
    if current_float_shares is not None:
        return _float_proxy(ticker, current_float_shares)

    # Layer 4 — unavailable
    return _unavailable(
        ticker,
        reason="yfinance fetch failed and no stale cache + no float data for fallback",
    )


# ---------- source constants (string-stable for downstream consumers) ----------

_SOURCE_YFINANCE_FRESH = "yfinance:fresh"
_SOURCE_CACHE_FRESH = "cache:fresh"
_SOURCE_CACHE_STALE = "cache:stale"
_SOURCE_FMP_FLOAT_PROXY = "fmp:float-proxy"
_SOURCE_UNAVAILABLE = "unavailable"


# ---------- yfinance primary path ----------


def _try_yfinance(ticker: str) -> dict | None:
    """Attempt a fresh fetch via yfinance with all hardening applied.

    Returns the structured result dict on success, or None on terminal
    failure (caller proceeds to next failover layer).
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — short interest fetcher unavailable")
        return None

    session = _make_hardened_session()

    last_error: Exception | None = None
    for attempt, base_delay in enumerate(RETRY_DELAYS_SEC, start=1):
        try:
            yticker = yf.Ticker(ticker, session=session) if session else yf.Ticker(ticker)
            info = yticker.info

            if not info or "symbol" not in info:
                raise RuntimeError(f"yfinance returned empty info for {ticker} (likely rate-limited or blocked)")

            short_pct = info.get("shortPercentOfFloat")
            if short_pct is None:
                # Some small-caps legitimately have no short interest data.
                # Distinguish this from rate-limit: if other fields populated, it's a real "no data."
                if info.get("regularMarketPrice") is not None:
                    logger.info(f"{ticker}: yfinance returned profile but no short interest field — likely no shorts on this name")
                    return _build_result(ticker, info, source=_SOURCE_YFINANCE_FRESH, short_pct=None)
                raise RuntimeError(f"yfinance returned no short interest fields for {ticker}")

            return _build_result(ticker, info, source=_SOURCE_YFINANCE_FRESH, short_pct=short_pct)

        except Exception as e:  # noqa: BLE001 — broad catch is the point of a failover layer
            last_error = e
            if attempt < len(RETRY_DELAYS_SEC):
                # Jitter: ±30% of base delay, so two parallel retries don't sync up
                delay = base_delay * random.uniform(0.7, 1.3)
                logger.warning(
                    f"{ticker}: yfinance attempt {attempt} failed ({e}); retrying in {delay:.1f}s"
                )
                time.sleep(delay)
            else:
                logger.warning(f"{ticker}: yfinance attempts exhausted, falling through to failover ({e})")

    return None


def _make_hardened_session():
    """Build a curl_cffi session that impersonates Chrome's TLS
    fingerprint. Yahoo's anti-bot looks at the JA3 hash — if it matches
    a real browser, we look like a real browser. Returns None if
    curl_cffi isn't installed (yfinance falls back to default requests
    session)."""
    try:
        from curl_cffi import requests as curl_requests
        return curl_requests.Session(impersonate="chrome131")
    except ImportError:
        logger.warning(
            "curl_cffi not installed — yfinance will use default requests session, "
            "more likely to be rate-limited on shared CI infra"
        )
        return None


def _build_result(ticker: str, info: dict, source: str, short_pct: float | None) -> dict:
    """Construct the canonical result dict from yfinance .info."""
    cur = info.get("sharesShort")
    prev = info.get("sharesShortPriorMonth")
    trend = None
    if cur is not None and prev:
        trend = (cur - prev) / prev * 100

    as_of = _format_yf_timestamp(info.get("dateShortInterest"))

    return {
        "ticker": ticker,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "short_percent_of_float": short_pct,
        "short_ratio_days_to_cover": info.get("shortRatio"),
        "shares_short": cur,
        "shares_short_prior_month": prev,
        "as_of_date": as_of,
        "trend_month_over_month_pct": trend,
        "stale_days": 0,
        "float_proxy_label": None,
        "failure_reason": None,
    }


def _format_yf_timestamp(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


# ---------- cache layer ----------


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}.json"


def _load_cache(ticker: str) -> dict | None:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(ticker: str, result: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with _cache_path(ticker).open("w") as f:
            json.dump(result, f, indent=2)
    except OSError as e:
        logger.warning(f"{ticker}: failed to write short-interest cache: {e}")


def _cache_age_days(cached: dict) -> int:
    fetched_at = cached.get("fetched_at")
    if not fetched_at:
        return 999
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).days
    except ValueError:
        return 999


def _cache_is_fresh(cached: dict) -> bool:
    fetched_at = cached.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=CACHE_FRESH_HOURS)
    except ValueError:
        return False


# ---------- Layer 3 — FMP float-proxy fallback ----------


# Bands from FMP support's recommendation (see docs/FMP_ENDPOINTS.md gap G).
# These match the "Low / Medium / High" labels they suggested for US stocks.
_FLOAT_BANDS = (
    (25_000_000,  "Low"),       # < 25M shares = low float = elevated squeeze risk
    (100_000_000, "Medium"),    # 25M-100M = medium
    (float("inf"), "High"),     # 100M+ = high float = low squeeze risk
)


def _float_proxy(ticker: str, current_float_shares: float) -> dict:
    """Return a result that uses float-size as a qualitative squeeze-risk
    proxy when actual short interest is unavailable. Per FMP support's
    documented workaround."""
    label = "Unknown"
    for max_shares, band_label in _FLOAT_BANDS:
        if current_float_shares < max_shares:
            label = band_label
            break

    return {
        "ticker": ticker,
        "source": _SOURCE_FMP_FLOAT_PROXY,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "short_percent_of_float": None,
        "short_ratio_days_to_cover": None,
        "shares_short": None,
        "shares_short_prior_month": None,
        "as_of_date": None,
        "trend_month_over_month_pct": None,
        "stale_days": None,
        "float_proxy_label": label,
        "failure_reason": "yfinance unavailable; using FMP float-size as qualitative proxy",
    }


# ---------- Layer 4 — unavailable ----------


def _unavailable(ticker: str, reason: str) -> dict:
    return {
        "ticker": ticker,
        "source": _SOURCE_UNAVAILABLE,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "short_percent_of_float": None,
        "short_ratio_days_to_cover": None,
        "shares_short": None,
        "shares_short_prior_month": None,
        "as_of_date": None,
        "trend_month_over_month_pct": None,
        "stale_days": None,
        "float_proxy_label": None,
        "failure_reason": reason,
    }


# ---------- batch helper (for orchestrator use) ----------


def fetch_batch(tickers: list[str], float_shares_lookup: dict[str, float] | None = None) -> dict[str, dict]:
    """Fetch short interest for multiple tickers with inter-ticker
    spacing to avoid burst patterns that trip Yahoo's rate-limiter.

    Args:
        tickers: list of ticker symbols.
        float_shares_lookup: optional {ticker: float_shares} for the
            Layer-3 fallback. Typically the orchestrator passes
            profile.shares_outstanding or profile.float_shares from FMP.

    Returns {ticker: result_dict}.
    """
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            # Inter-ticker delay (only if we'd actually hit Yahoo —
            # cache hits are free).
            cached = _load_cache(ticker)
            if not (cached and _cache_is_fresh(cached)):
                jitter = INTER_TICKER_DELAY_SEC * random.uniform(0.8, 1.2)
                time.sleep(jitter)
        float_shares = (float_shares_lookup or {}).get(ticker)
        out[ticker] = fetch(ticker, current_float_shares=float_shares)
    return out


# ---------- CLI smoke test ----------


def _cli():
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Fetch short interest with failover chain.")
    parser.add_argument("tickers", nargs="*", default=["NVDA", "AMAT", "IONQ"])
    parser.add_argument("--no-cache", action="store_true", help="Bypass fresh-cache check, force a yfinance call")
    args = parser.parse_args()

    if args.no_cache:
        for t in args.tickers:
            p = _cache_path(t)
            if p.exists():
                p.unlink()
            print(f"cleared cache for {t}")

    results = fetch_batch(args.tickers)
    for ticker, r in results.items():
        print(f"\n{ticker}  source={r['source']}")
        if r["source"] == _SOURCE_UNAVAILABLE:
            print(f"  unavailable: {r['failure_reason']}")
            continue
        if r["float_proxy_label"]:
            print(f"  float proxy: {r['float_proxy_label']} (precise short% unavailable)")
            continue
        sp = r.get("short_percent_of_float")
        if sp is not None:
            print(f"  short % of float:    {sp*100:.2f}%")
        if r.get("short_ratio_days_to_cover") is not None:
            print(f"  days to cover:       {r['short_ratio_days_to_cover']:.2f}")
        if r.get("as_of_date"):
            print(f"  as-of:               {r['as_of_date']}")
        if r.get("trend_month_over_month_pct") is not None:
            direction = "↑" if r["trend_month_over_month_pct"] > 0 else "↓"
            print(f"  MoM trend:           {direction} {r['trend_month_over_month_pct']:+.1f}%")
        if r.get("stale_days") is not None and r["stale_days"] > 0:
            print(f"  stale by:            {r['stale_days']} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
