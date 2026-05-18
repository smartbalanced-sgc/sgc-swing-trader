"""Measurement-driven tier classifier — advisory layer.

See docs/V1_SPEC.md §4.2. Each nightly run, for every watchlist ticker,
this module computes four observable behavioral properties from the raw
daily prices/volumes and scores each property into tier A, B, or C
against the bands in `config.TIER_CLASSIFIER`. The ticker's measured
tier is the *most conservative* (toward C) of the four — the
"worst-of" rule. Bias is toward more warnings, not fewer.

Plain-English meanings (no jargon without explanation):

  - Realized volatility (annualized): the typical year-over-year price
    swing as a fraction of price. Computed as the standard deviation of
    daily log-returns over the trailing 90 trading days, then scaled by
    √252 (≈ trading days per year) to express it in annualized terms.
    Example: 0.50 means the stock's annual price swing is roughly half
    its own price.

  - Volatility-of-volatility (vol-of-vol): how much the daily move-size
    itself swings around. Computed as the standard deviation of the
    rolling 20-day vol estimate over the same 90-day window. High
    vol-of-vol means the calm/wild oscillation is itself unpredictable.

  - Average daily dollar volume (ADV): the dollar value traded per day
    over the trailing 20 sessions = mean(close × volume). High ADV
    means deep liquidity (big trades don't move price much); low ADV
    means thin tape (a single retail-sized order can move it 1-2%).

  - Days of clean history: how many clean daily bars survive sanity
    checks. Names with <1 year of clean data lack a regime cycle to
    calibrate against — fundamentally more uncertain.

The classifier is ADVISORY in v1.0: the watchlist's human-set `tier:`
field still drives the actual statistical treatment in the pipeline.
The dashboard surfaces watchlist-vs-measured agreement so humans can
re-anchor when warranted. See §4.2 for the v1.x flip-to-behavior-wins
policy.

CLI smoke test (after running data fetch first):
    python -m src.pipeline.data NVDA
    python -m src.tier_classifier NVDA
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

from src import config
from src.pipeline import data as data_module

# Ordering used by the worst-of rule: smaller index = more conservative.
# So the per-property tier we keep is the MAX index across properties.
_TIER_ORDER = ("A", "B", "C")
_TIER_INDEX = {t: i for i, t in enumerate(_TIER_ORDER)}


# ---------- public API ----------


def classify(price_data: dict) -> dict:
    """Score one ticker against the §4.2 behavioral bands.

    Args:
        price_data: the dict returned by `src.pipeline.data.fetch()` —
            must contain a 'prices' list of bars with 'date', 'close'
            (or 'adj_close'), and 'volume'.

    Returns a dict with shape:
        {
            "ticker": "NVDA",
            "measured_tier": "A" | "B" | "C",
            "properties": {
                "vol_annualized":   {"value": 0.42, "tier": "B"},
                "vol_of_vol":       {"value": 0.11, "tier": "A"},
                "adv_usd":          {"value": 8.2e9, "tier": "A"},
                "history_days":     {"value": 1258, "tier": "A"},
            },
            "decisive_property": "vol_annualized",  # which property
                                                    # bound the measured
                                                    # tier (worst-of)
            "notes": [ ... ],                       # any data-quality
                                                    # caveats
        }

    Caller decides what to do with the measured tier — compare against
    the watchlist anchor, surface on dashboard, etc.
    """
    cfg = config.THRESHOLDS.tier_classifier
    prices = price_data.get("prices") or []
    notes: list[str] = []

    df = _prices_to_df(prices)
    if df.empty:
        raise ValueError(f"no usable price bars for {price_data.get('ticker')}")

    vol_annualized = _realized_vol_annualized(df, cfg.vol_window_days)
    vol_of_vol = _vol_of_vol(df, cfg.vol_window_days, cfg.vol_of_vol_inner_window)
    adv_usd = _avg_daily_dollar_volume(df, cfg.adv_window_days)
    history_days = len(df)

    if history_days < cfg.vol_window_days:
        notes.append(
            f"only {history_days} bars available; vol metrics computed on "
            f"a shorter window than the {cfg.vol_window_days}-day target"
        )

    props = {
        "vol_annualized":  {"value": vol_annualized,  "tier": _score_upper_bound(vol_annualized,  cfg.vol_annualized_bounds)},
        "vol_of_vol":      {"value": vol_of_vol,      "tier": _score_upper_bound(vol_of_vol,      cfg.vol_of_vol_bounds)},
        "adv_usd":         {"value": adv_usd,         "tier": _score_lower_bound(adv_usd,         cfg.adv_usd_lower_bounds)},
        "history_days":    {"value": history_days,    "tier": _score_lower_bound(history_days,    cfg.history_days_lower_bounds)},
    }

    decisive_name, decisive_tier = max(
        props.items(),
        key=lambda kv: _TIER_INDEX[kv[1]["tier"]],
    )

    return {
        "ticker": price_data.get("ticker"),
        "measured_tier": decisive_tier["tier"],
        "properties": props,
        "decisive_property": decisive_name,
        "notes": notes,
    }


def compare_to_anchor(measured: dict, anchor_tier: str | None) -> dict:
    """Compare a measured-tier result against the watchlist anchor.

    Returns a small dict the dashboard can render directly:
        {"anchor": "A", "measured": "B", "agreement": False,
         "direction": "stricter"}  # "stricter" = measured says more risk
                                   # than anchor, "looser" = less.
    """
    measured_tier = measured["measured_tier"]
    if anchor_tier is None:
        return {
            "anchor": None,
            "measured": measured_tier,
            "agreement": False,
            "direction": "no_anchor",
        }
    if anchor_tier == measured_tier:
        direction = "match"
    elif _TIER_INDEX[measured_tier] > _TIER_INDEX[anchor_tier]:
        direction = "stricter"
    else:
        direction = "looser"
    return {
        "anchor": anchor_tier,
        "measured": measured_tier,
        "agreement": (anchor_tier == measured_tier),
        "direction": direction,
    }


# ---------- internals ----------


def _prices_to_df(prices: Iterable[dict]) -> pd.DataFrame:
    """Convert the list-of-dicts payload to a clean DataFrame, dropping
    rows with missing close or volume. Uses adj_close when available so
    splits/dividends don't pollute the return series."""
    rows = []
    for p in prices:
        close = p.get("adj_close") or p.get("close")
        vol = p.get("volume")
        if close is None or vol is None or close <= 0:
            continue
        rows.append({"date": p["date"], "close": float(close), "volume": float(vol)})
    if not rows:
        return pd.DataFrame(columns=["date", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _realized_vol_annualized(df: pd.DataFrame, window: int) -> float:
    """Std-dev of daily log-returns over the trailing `window` bars,
    scaled by √252. Returns 0.0 if too few bars to estimate."""
    if len(df) < 2:
        return 0.0
    tail = df.tail(window + 1)  # +1 because we need N log-returns from N+1 prices
    log_returns = (tail["close"].apply(math.log)).diff().dropna()
    if log_returns.empty:
        return 0.0
    daily_std = log_returns.std(ddof=1)
    if pd.isna(daily_std):
        return 0.0
    return float(daily_std * math.sqrt(252))


def _vol_of_vol(df: pd.DataFrame, outer_window: int, inner_window: int) -> float:
    """Std-dev of the rolling `inner_window`-day annualized vol estimate,
    computed over the trailing `outer_window` bars. Captures how much
    the calm/wild oscillation itself swings around. Returns 0.0 if too
    few bars."""
    if len(df) < inner_window + 2:
        return 0.0
    tail = df.tail(outer_window + inner_window)
    log_returns = (tail["close"].apply(math.log)).diff().dropna()
    if len(log_returns) < inner_window:
        return 0.0
    rolling_vol = log_returns.rolling(inner_window).std(ddof=1) * math.sqrt(252)
    rolling_vol = rolling_vol.dropna()
    if rolling_vol.empty:
        return 0.0
    vov = rolling_vol.std(ddof=1)
    return 0.0 if pd.isna(vov) else float(vov)


def _avg_daily_dollar_volume(df: pd.DataFrame, window: int) -> float:
    """Mean of close × volume over the trailing `window` bars. Raw USD,
    not millions."""
    tail = df.tail(window)
    if tail.empty:
        return 0.0
    dollar_vol = (tail["close"] * tail["volume"]).mean()
    return 0.0 if pd.isna(dollar_vol) else float(dollar_vol)


def _score_upper_bound(value: float, bounds) -> str:
    """Score a metric where SMALLER values are better (vol, vol-of-vol).
    `bounds` is a SimpleNamespace with .A/.B/.C upper bounds (loaded
    from thresholds.yml). Returns the first tier whose upper bound the
    value clears."""
    for tier in _TIER_ORDER:
        if value <= getattr(bounds, tier):
            return tier
    return "C"


def _score_lower_bound(value: float, bounds) -> str:
    """Score a metric where LARGER values are better (ADV, history).
    `bounds` is a SimpleNamespace with .A/.B/.C lower bounds (loaded
    from thresholds.yml). Returns the first tier whose lower bound the
    value meets."""
    for tier in _TIER_ORDER:
        if value >= getattr(bounds, tier):
            return tier
    return "C"


# ---------- CLI smoke test ----------


def _load_watchlist_tier(ticker: str) -> str | None:
    if not config.WATCHLIST_PATH.exists():
        return None
    with config.WATCHLIST_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    entry = data.get(ticker)
    return entry.get("tier") if isinstance(entry, dict) else None


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_money(n: float) -> str:
    for unit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= unit:
            return f"${n / unit:.2f}{suffix}"
    return f"${n:.0f}"


def _print_summary(result: dict, anchor_tier: str | None) -> None:
    t = result["ticker"]
    props = result["properties"]
    comparison = compare_to_anchor(result, anchor_tier)

    print(
        f"{t}  watchlist_tier={anchor_tier or '?'}  measured_tier={result['measured_tier']}  "
        f"direction={comparison['direction']}"
    )
    print(f"  decisive_property: {result['decisive_property']}")
    print(f"  properties:")
    print(f"    vol_annualized  = {_fmt_pct(props['vol_annualized']['value']):>8}   → tier {props['vol_annualized']['tier']}")
    print(f"    vol_of_vol      = {props['vol_of_vol']['value']:>8.4f}   → tier {props['vol_of_vol']['tier']}")
    print(f"    adv_usd         = {_fmt_money(props['adv_usd']['value']):>8}   → tier {props['adv_usd']['tier']}")
    print(f"    history_days    = {props['history_days']['value']:>8}   → tier {props['history_days']['tier']}")
    if result["notes"]:
        print(f"  notes:")
        for n in result["notes"]:
            print(f"    - {n}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score a ticker against the §4.2 measurement bands."
    )
    parser.add_argument("ticker", help="ticker symbol, e.g. NVDA")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force re-fetch of price data (ignore cache)",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper()
    try:
        price_data = data_module.fetch(ticker, refresh=args.refresh)
    except Exception as e:  # noqa: BLE001 - surface vendor errors to user verbatim
        print(f"ERROR fetching {ticker}: {e}", file=sys.stderr)
        return 2

    result = classify(price_data)
    anchor = _load_watchlist_tier(ticker)
    _print_summary(result, anchor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
