"""Step 5 - Fair value triangulation.

Three independent valuation methods, triangulated to a per-share fair
value range. The conviction Layer-3 veto reads `premium_sigmas` (how
many standard deviations the current price is above the range mean)
and fires SKIP/TRIM at >= +2 sigma. Below +2 sigma the panel is
informational - "modestly expensive" or "fairly valued" copy with
no veto.

Why three independent methods:

  - Each method has structural blind spots. DCF over-trusts long-run
    growth assumptions. Forward P/E peers anchors on sector
    multiples which can be collectively dislocated. Analyst PT
    consensus is anchored on recent prices via herding.
  - Median across methods is robust to any one method going
    haywire. Range (low to high) honestly surfaces the disagreement
    between methods - a $200 stock with methods at [$150, $200,
    $250] is genuinely uncertain; the SAME stock with all three at
    $200 is pinned. Same `range_mean` = $200 but vastly different
    sigma.
  - Methods FAIL independently (negative FCF, no analyst estimates,
    gated endpoints, no peers). Each failure is isolated; the step
    returns ok as long as min_methods_for_triangulation succeeded.

Method 1: Discounted Cash Flow (DCF)

  Standard 2-stage DCF. Inputs:
  - TTM FCF from cash-flow-statement (operatingCashFlow + capex)
  - Growth rate: median of historical YoY FCF growth, capped at
    growth_rate_cap (default 25%/yr - above this is heroic).
  - Discount rate: literature default 9% (~4% risk-free + ~5% ERP).
  - Terminal growth: 2.5% (Gordon perpetuity rate).

  Math:
    pv_projected = sum_{y=1..5} fcf_ttm * (1+g)^y / (1+r)^y
    fcf_terminal = fcf_ttm * (1+g)^5 * (1+g_terminal)
    pv_terminal = fcf_terminal / (r - g_terminal) / (1+r)^5
    fair_value_per_share = (pv_projected + pv_terminal) / shares_outstanding

  Skips when TTM FCF <= 0 (cash-burning growth names like IONQ -
  DCF is mathematically meaningless when projecting losses forever).

Method 2: Forward P/E peers

  Compare ticker's forward P/E against peer median forward P/E,
  apply peer multiple to ticker's forward EPS:

    forward_eps = analyst-estimates next-FY epsAvg
    peer_pes = [peer_price / peer_forward_eps for peer in stock-peers]
    fair_value_per_share = median(peer_pes) * forward_eps

  Skips when forward EPS is <= 0 (lossmaking ticker) or no peers
  fetchable.

Method 3: Analyst PT consensus

  Use median target price directly from price-target-consensus.
  Simplest method - just plugs into the triangulation as one of
  three anchors.

Triangulation:

  range_low  = min(method values)
  range_mean = median(method values)   - robust to outliers
  range_high = max(method values)
  sigma      = (range_high - range_low) / 4  if >= 2 methods
             = range_mean * 0.15            if only 1 method

  premium_sigmas = (current_price - range_mean) / sigma
  Positive = overvalued. Veto threshold +2 sigma.

Output schema (matches `src/dashboard.py:_render_fair_value`):

    {
        "status": "ok",
        "fetched_at": ISO timestamp,
        "source": "live" | "cache:fresh",
        "current_price": float,
        "range_low": float,
        "range_mean": float,        # median across methods
        "range_high": float,
        "premium_sigmas": float,    # (current - mean) / sigma
        "methods": [str, ...],      # method names (for dashboard list)
        "method_details": [{...}],  # per-method details for diagnostics
        "method_errors": [{...}],   # methods that failed and why
        "narrative": str,
    }

CLI smoke test:

    python -m src.pipeline.fair_value NVDA
    python -m src.pipeline.fair_value NVDA AMAT IONQ
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

from src import config
from src.data_sources import fmp

logger = logging.getLogger(__name__)

_FV = config.THRESHOLDS.fair_value
CACHE_DIR = config.DATA_DIR / "cache" / "fair_value"
CACHE_TTL_HOURS = _FV.cache_ttl_hours
MIN_METHODS = _FV.min_methods_for_triangulation
SINGLE_METHOD_SIGMA_PCT = _FV.single_method_sigma_pct


# ---------- public API ----------


def estimate(ticker: str, price_data: dict, refresh: bool = False) -> dict:
    """Triangulate fair value across DCF + forward P/E peers + analyst PT.

    Args:
        ticker: ticker symbol.
        price_data: output of src.pipeline.data.fetch(ticker). Needs
            `profile` (for shares_outstanding, price) and `prices`
            (for current price fallback).
        refresh: bypass the disk cache and force a fresh fetch.

    Returns the payload described in the module docstring.
    """
    if not refresh:
        cached = _load_cache(ticker)
        if cached and _cache_is_fresh(cached):
            cached["source"] = "cache:fresh"
            return cached

    profile = price_data.get("profile") or {}
    current_price = _extract_current_price(profile, price_data)
    if not current_price or current_price <= 0:
        return _fail("current_price unavailable")

    methods_detail: list[dict] = []
    method_errors: list[dict] = []

    if _FV.dcf.enabled:
        _run_method(
            "DCF", lambda: _dcf(ticker, profile),
            methods_detail, method_errors,
        )

    if _FV.forward_pe_peers.enabled:
        _run_method(
            "Forward P/E peers", lambda: _forward_pe_peers(ticker, current_price),
            methods_detail, method_errors,
        )

    if _FV.analyst_pt.enabled:
        _run_method(
            "Analyst PT consensus", lambda: _analyst_pt(ticker),
            methods_detail, method_errors,
        )

    if len(methods_detail) < MIN_METHODS:
        return _fail(
            f"only {len(methods_detail)} method(s) succeeded; "
            f"need >= {MIN_METHODS}. "
            f"Errors: {', '.join(e['method'] + ': ' + e['reason'] for e in method_errors)}"
        )

    triangulated = _triangulate(methods_detail, current_price)
    method_names = [m["name"] for m in methods_detail]
    narrative = _build_narrative(
        ticker=ticker,
        triangulated=triangulated,
        methods=methods_detail,
        method_errors=method_errors,
        current_price=current_price,
    )

    result = {
        "status": "ok",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "live",
        "current_price": current_price,
        "range_low": triangulated["range_low"],
        "range_mean": triangulated["range_mean"],
        "range_high": triangulated["range_high"],
        "premium_sigmas": triangulated["premium_sigmas"],
        "methods": method_names,
        "method_details": methods_detail,
        "method_errors": method_errors,
        "narrative": narrative,
    }

    _save_cache(ticker, result)
    return result


# ---------- method runners ----------


def _run_method(name: str, fn, methods_out: list, errors_out: list) -> None:
    """Execute one method, append result to methods_out or errors_out."""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - methods isolated per design
        errors_out.append({
            "method": name,
            "reason": str(e),
            "exception_type": type(e).__name__,
        })
        logger.info(f"{name} method skipped: {e}")
        return
    if result is None:
        # Method declined politely (e.g. negative FCF -> DCF returns None)
        errors_out.append({
            "method": name,
            "reason": "method returned None (data did not meet preconditions)",
        })
        return
    methods_out.append(result)


# ---------- Method 1: DCF ----------


def _dcf(ticker: str, profile: dict) -> dict | None:
    """2-stage DCF using TTM FCF and median historical growth."""
    cfg = _FV.dcf
    cfs_body = fmp.get(
        "cash-flow-statement", ticker, period="annual", limit=5
    )
    if not isinstance(cfs_body, list) or not cfs_body:
        raise RuntimeError("no cash-flow-statement data returned by FMP")

    # FMP returns newest-first
    ocf_ttm = float(cfs_body[0].get("operatingCashFlow") or 0.0)
    capex_ttm = float(cfs_body[0].get("capitalExpenditure") or 0.0)
    fcf_ttm = ocf_ttm + capex_ttm  # capex typically negative

    if cfg.skip_when_fcf_negative and fcf_ttm <= 0:
        raise RuntimeError(
            f"TTM FCF is non-positive (${fcf_ttm/1e9:.2f}B); "
            f"DCF mathematically meaningless for cash-burning names"
        )

    # Historical FCF for growth estimate (reverse to chronological)
    fcf_history = []
    for row in reversed(cfs_body):
        ocf_y = float(row.get("operatingCashFlow") or 0.0)
        capex_y = float(row.get("capitalExpenditure") or 0.0)
        fcf_history.append(ocf_y + capex_y)

    growth_rate = _estimate_growth_rate(fcf_history, cap=cfg.growth_rate_cap)

    # 2-stage DCF math
    r = cfg.discount_rate
    g = growth_rate
    g_terminal = cfg.terminal_growth
    n = cfg.projection_years

    pv_projected = 0.0
    for year in range(1, n + 1):
        fcf_year = fcf_ttm * (1.0 + g) ** year
        pv = fcf_year / (1.0 + r) ** year
        pv_projected += pv

    fcf_n_plus_1 = fcf_ttm * (1.0 + g) ** n * (1.0 + g_terminal)
    if r <= g_terminal:
        raise RuntimeError(
            f"discount rate {r:.3f} <= terminal growth {g_terminal:.3f}; "
            f"Gordon growth model diverges"
        )
    terminal_value = fcf_n_plus_1 / (r - g_terminal)
    pv_terminal = terminal_value / (1.0 + r) ** n

    equity_value = pv_projected + pv_terminal

    shares = profile.get("shares_outstanding")
    if not shares or shares <= 0:
        raise RuntimeError(f"shares_outstanding missing or invalid ({shares})")

    fair_value_per_share = equity_value / float(shares)

    return {
        "name": "DCF",
        "value": fair_value_per_share,
        "details": {
            "fcf_ttm": fcf_ttm,
            "fcf_history_billions": [round(v / 1e9, 3) for v in fcf_history],
            "growth_rate_estimated": growth_rate,
            "growth_rate_cap": cfg.growth_rate_cap,
            "discount_rate": r,
            "terminal_growth": g_terminal,
            "projection_years": n,
            "pv_projected_billions": round(pv_projected / 1e9, 3),
            "pv_terminal_billions": round(pv_terminal / 1e9, 3),
            "shares_outstanding": shares,
        },
    }


def _estimate_growth_rate(fcf_history: list[float], cap: float) -> float:
    """Median YoY growth from historical FCF, clipped to [-10%, +cap].

    Median (not mean) is robust to one-year outliers like a pandemic
    dip or a special-charge year. Cap prevents 100%+ recent growth
    from being extrapolated as perpetual.
    """
    if len(fcf_history) < 2:
        return 0.05  # default modest growth when history too short
    growth_rates = []
    for i in range(1, len(fcf_history)):
        prev = fcf_history[i - 1]
        cur = fcf_history[i]
        if prev > 0 and cur > 0:
            growth_rates.append(cur / prev - 1.0)
    if not growth_rates:
        return 0.05
    growth_rates.sort()
    median = growth_rates[len(growth_rates) // 2]
    return max(-0.10, min(median, cap))


# ---------- Method 2: Forward P/E peers ----------


def _forward_pe_peers(ticker: str, current_price: float) -> dict | None:
    """Compare ticker's forward P/E to peer median forward P/E,
    apply peer multiple to ticker's forward EPS."""
    cfg = _FV.forward_pe_peers

    # 1. Ticker's forward EPS
    forward_eps = _fetch_forward_eps(ticker)
    if forward_eps is None or forward_eps <= 0:
        raise RuntimeError(
            f"ticker forward EPS missing or non-positive ({forward_eps})"
        )

    ticker_forward_pe = current_price / forward_eps

    # 2. Peer tickers
    peers_body = fmp.get("stock-peers", ticker)
    peer_tickers = _extract_peer_tickers(peers_body, exclude=ticker)
    if not peer_tickers:
        raise RuntimeError("no peer tickers returned by stock-peers endpoint")
    peer_tickers = peer_tickers[: cfg.max_peers]

    # 3. Per-peer forward P/E
    peer_data = []
    for peer in peer_tickers:
        try:
            peer_eps = _fetch_forward_eps(peer)
            if peer_eps is None or peer_eps <= 0:
                continue
            peer_price = _fetch_quote_price(peer)
            if peer_price is None or peer_price <= 0:
                continue
            peer_pe = peer_price / peer_eps
            peer_data.append({
                "ticker": peer,
                "price": peer_price,
                "forward_eps": peer_eps,
                "forward_pe": peer_pe,
            })
        except Exception as e:  # noqa: BLE001
            logger.debug(f"peer {peer} skipped: {e}")
            continue

    if not peer_data:
        raise RuntimeError("no peer forward P/Es could be computed")

    peer_pes = sorted([p["forward_pe"] for p in peer_data])
    peer_median_pe = peer_pes[len(peer_pes) // 2]
    fair_value_per_share = peer_median_pe * forward_eps

    return {
        "name": "Forward P/E peers",
        "value": fair_value_per_share,
        "details": {
            "ticker_forward_eps": forward_eps,
            "ticker_forward_pe": ticker_forward_pe,
            "peer_median_forward_pe": peer_median_pe,
            "peer_count": len(peer_data),
            "peers": peer_data,
        },
    }


def _fetch_forward_eps(ticker: str) -> float | None:
    """Pull next-FY consensus EPS from analyst-estimates.

    FMP returns multiple yearly records; pick the earliest record with
    a date >= today.
    """
    body = fmp.get("analyst-estimates", ticker, period="annual")
    if not isinstance(body, list) or not body:
        return None
    today = date.today().isoformat()
    future = [
        r for r in body
        if (r.get("date") or "")[:10] >= today and r.get("epsAvg") is not None
    ]
    if not future:
        return None
    future.sort(key=lambda r: (r.get("date") or ""))
    return float(future[0]["epsAvg"])


def _fetch_quote_price(ticker: str) -> float | None:
    """Real-time quote price from /quote."""
    body = fmp.get("quote", ticker)
    if isinstance(body, list) and body:
        row = body[0]
    elif isinstance(body, dict):
        row = body
    else:
        return None
    price = row.get("price")
    return float(price) if price is not None else None


def _extract_peer_tickers(peers_body, exclude: str) -> list[str]:
    """Normalize FMP's varying stock-peers response shapes into a flat
    list of ticker strings."""
    if peers_body is None:
        return []
    out: list[str] = []
    rows = peers_body if isinstance(peers_body, list) else [peers_body]
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Common shape: {"symbol": "NVDA", "peersList": ["AMD", "AVGO", ...]}
        # Other shape: {"symbol": "AMD"} repeated across rows
        peers_field = row.get("peersList") or row.get("peers")
        if isinstance(peers_field, list):
            out.extend(str(s) for s in peers_field if s)
        elif isinstance(peers_field, str):
            out.append(peers_field)
        else:
            sym = row.get("symbol")
            if isinstance(sym, str) and sym.upper() != exclude.upper():
                out.append(sym)
    # Dedupe preserving order, drop self
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        s_up = s.upper()
        if s_up != exclude.upper() and s_up not in seen:
            seen.add(s_up)
            deduped.append(s)
    return deduped


# ---------- Method 3: Analyst PT consensus ----------


def _analyst_pt(ticker: str) -> dict | None:
    body = fmp.get("price-target-consensus", ticker)
    if isinstance(body, list) and body:
        row = body[0]
    elif isinstance(body, dict):
        row = body
    else:
        raise RuntimeError("no price-target-consensus data returned by FMP")

    target_median = row.get("targetMedian")
    if target_median is None or target_median <= 0:
        raise RuntimeError(f"targetMedian missing or invalid ({target_median})")

    return {
        "name": "Analyst PT consensus",
        "value": float(target_median),
        "details": {
            "target_median": target_median,
            "target_high": row.get("targetHigh"),
            "target_low": row.get("targetLow"),
            "target_consensus_mean": row.get("targetConsensus"),
        },
    }


# ---------- triangulation ----------


def _triangulate(methods: list[dict], current_price: float) -> dict:
    """Compute range_low/_mean/_high + sigma + premium_sigmas."""
    values = sorted([m["value"] for m in methods])

    if len(values) >= 2:
        range_low = values[0]
        range_high = values[-1]
        range_mean = values[len(values) // 2]  # median (odd: middle; even: upper-middle)
        sigma = max((range_high - range_low) / 4.0, range_mean * 0.01)
    else:
        # Single method - use a default uncertainty band
        range_mean = values[0]
        sigma = max(range_mean * SINGLE_METHOD_SIGMA_PCT, 0.01)
        range_low = range_mean - 2 * sigma
        range_high = range_mean + 2 * sigma

    premium_sigmas = (current_price - range_mean) / sigma if sigma > 0 else 0.0

    return {
        "range_low": range_low,
        "range_mean": range_mean,
        "range_high": range_high,
        "sigma": sigma,
        "premium_sigmas": premium_sigmas,
    }


# ---------- narrative ----------


def _build_narrative(
    ticker: str,
    triangulated: dict,
    methods: list[dict],
    method_errors: list[dict],
    current_price: float,
) -> str:
    mean = triangulated["range_mean"]
    low = triangulated["range_low"]
    high = triangulated["range_high"]
    sigmas = triangulated["premium_sigmas"]

    method_names = ", ".join(m["name"] for m in methods)
    parts = [
        f"Fair-value range ${low:.2f} - ${mean:.2f} (mean) - ${high:.2f}, "
        f"derived from {len(methods)} method(s): {method_names}."
    ]

    # Position narrative
    if sigmas >= 2.0:
        parts.append(
            f"Current ${current_price:.2f} is {sigmas:+.1f}sigma above the mean - "
            f"Layer-3 fair-value veto FIRES (SKIP for watching, TRIM for entered)."
        )
    elif sigmas >= 1.0:
        parts.append(
            f"Current ${current_price:.2f} is {sigmas:+.1f}sigma above the mean - "
            f"modest premium but no veto."
        )
    elif sigmas >= -1.0:
        parts.append(
            f"Current ${current_price:.2f} is {sigmas:+.1f}sigma vs the mean - "
            f"roughly fair value."
        )
    elif sigmas >= -2.0:
        parts.append(
            f"Current ${current_price:.2f} is {sigmas:+.1f}sigma below the mean - "
            f"meaningfully cheap; supportive for new entries."
        )
    else:
        parts.append(
            f"Current ${current_price:.2f} is {sigmas:+.1f}sigma below the mean - "
            f"deeply discounted; valuation supports ENTER if other layers align."
        )

    # Method-level color
    for m in methods:
        if m["name"] == "DCF":
            d = m["details"]
            parts.append(
                f"DCF $${m['value']:.2f}: TTM FCF ${d['fcf_ttm']/1e9:.2f}B, "
                f"growth {d['growth_rate_estimated']*100:.1f}%/yr "
                f"(capped at {d['growth_rate_cap']*100:.0f}%), "
                f"discount {d['discount_rate']*100:.1f}%, terminal {d['terminal_growth']*100:.1f}%."
            )
        elif m["name"] == "Forward P/E peers":
            d = m["details"]
            parts.append(
                f"Forward P/E peers $${m['value']:.2f}: ticker forward EPS "
                f"${d['ticker_forward_eps']:.2f}, ticker P/E {d['ticker_forward_pe']:.1f}, "
                f"peer median P/E {d['peer_median_forward_pe']:.1f} "
                f"({d['peer_count']} peers used)."
            )
        elif m["name"] == "Analyst PT consensus":
            d = m["details"]
            parts.append(
                f"Analyst PT consensus $${m['value']:.2f}: median target across "
                f"contributing analysts (range ${d.get('target_low', '?')} - "
                f"${d.get('target_high', '?')})."
            )

    if method_errors:
        skipped = ", ".join(f"{e['method']} ({e['reason'][:60]})" for e in method_errors)
        parts.append(f"_Methods skipped: {skipped}._")

    # Fix the double-$$ from f-string escaping above
    return " ".join(parts).replace("$$", "$")


# ---------- helpers ----------


def _extract_current_price(profile: dict, price_data: dict) -> float | None:
    price = profile.get("price")
    if price is not None and price > 0:
        return float(price)
    prices = price_data.get("prices") or []
    if prices:
        last = prices[-1].get("adj_close") or prices[-1].get("close")
        if last:
            return float(last)
    return None


def _fail(reason: str) -> dict:
    return {
        "status": "fail",
        "reason": reason,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- cache ----------


def _cache_path(ticker: str):
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


def _cache_is_fresh(cached: dict) -> bool:
    ts = cached.get("fetched_at")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when) < timedelta(hours=CACHE_TTL_HOURS)
    except ValueError:
        return False


def _save_cache(ticker: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with _cache_path(ticker).open("w") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        logger.warning(f"{ticker}: failed to write fair-value cache: {e}")


# ---------- CLI smoke test ----------


def _print_summary(ticker: str, payload: dict) -> None:
    print(f"\n=== {ticker} ===")
    if payload.get("status") != "ok":
        print(f"  status: {payload.get('status')} - {payload.get('reason')}")
        return
    print(f"  source: {payload['source']}")
    print(f"  current: ${payload['current_price']:.2f}")
    print(f"  range:   ${payload['range_low']:.2f} -> ${payload['range_mean']:.2f} (mean) -> ${payload['range_high']:.2f}")
    print(f"  premium: {payload['premium_sigmas']:+.2f}sigma vs FV mean")
    print(f"  methods ({len(payload['methods'])}): {', '.join(payload['methods'])}")
    if payload.get("method_errors"):
        print(f"  methods skipped ({len(payload['method_errors'])}):")
        for e in payload["method_errors"]:
            print(f"    {e['method']}: {e['reason']}")
    print(f"\n  narrative:")
    print(f"    {payload['narrative']}")


def main() -> int:
    from src.pipeline import data as data_step

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Fair value smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to value")
    parser.add_argument("--refresh", action="store_true", help="bypass disk cache")
    args = parser.parse_args()

    exit_code = 0
    for ticker in args.tickers:
        ticker = ticker.upper()
        try:
            price_data = data_step.fetch(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR fetching {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        try:
            payload = estimate(ticker, price_data, refresh=args.refresh)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR estimating {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
