"""Inline forward-tracking backtest.

This is NOT walk-forward replay. It does not re-run the engine on
historical dates. Instead it follows the dip-engine pattern
(sgc-dip-engine `signal_archiver.py` + `backtest.py`):

  1. Every nightly run writes a snapshot to data/snapshots/{TICKER}/.
     That snapshot already contains every prediction the engine made
     that night — predicted dip price, predicted rally price, per-user
     target, per-user stop, verdict label, the lot. So the snapshot
     archive IS the signal-history archive; no separate CSV needed.

  2. This module reads accumulated snapshots and, for each snapshot
     old enough that its horizon has elapsed (e.g. a 30d-horizon
     snapshot from 30 trading days ago), compares the predictions to
     what actually happened. Two questions per (ticker, snapshot, horizon):

       a) DID THE DIP HAPPEN? — did the daily LOW ever touch the
          predicted `price_levels.horizons[h].dip.price` within the
          horizon window?
       b) DID THE RALLY HAPPEN? — did the daily HIGH ever touch the
          predicted `price_levels.horizons[h].rally.price` within the
          horizon window?

     And per-user (using the verdict's target/stop):

       c) DID THE TARGET HIT? — daily HIGH >= target_price ever.
       d) DID THE STOP HIT? — daily LOW <= stop_price ever.
       e) FIRST-PASSAGE — which happened first, target or stop or
          neither?

     Intraday extremes (high/low) are used because Trading 212
     limit/stop orders fire intraday, not on the close.

  3. Per-verdict aggregates: target-hit rate, stop-hit rate, mean
     realized return per verdict label per horizon. Plus the
     dip/rally-prediction calibration that's verdict-independent.

Cold start: snapshots accumulate over time. The first 14 trading
days after going live there isn't enough data — the panel shows
"X of 14 days of history accumulated." After day 14 the first
30d-horizon predictions start aging into testability; 60d follows
later. This is honest cold-start behavior, not a bug.

This module is called inline by main.py after the per-ticker pipeline
loop completes — milliseconds of compute, no extra API calls (price
data is reused from the live run's data.fetch results).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

# Trading days a snapshot must be old before its predictions are
# testable. Same as dip engine's default (14). Below this, no
# meaningful sample of outcomes has elapsed.
MIN_HISTORY_TRADING_DAYS = 14

# Per-horizon: a snapshot's predictions are evaluable only after this
# many trading days have passed since the snapshot date (so we have
# the full forward window of bars to check against). At 30, a 30d
# horizon prediction has its full window of evidence; same for 60.
def _required_future_bars(horizon_days: int) -> int:
    return horizon_days


# ---------- public API ----------


def run(price_data_by_ticker: dict[str, dict]) -> dict:
    """Read accumulated snapshots, score predictions whose horizon has
    elapsed, return the aggregate payload for the dashboard.

    Args:
        price_data_by_ticker: {ticker: data.fetch() result}. Used as
            the source of "what actually happened" — the price bars
            after each snapshot's date. Passed in (not re-fetched)
            so this module is free of network I/O.

    Returns dashboard-shaped payload:
        {
            "status": "ok" | "insufficient_data",
            "days_of_history": int,
            "trades_evaluated": int,
            "by_horizon": {
                30: {
                    "n": int,
                    "target_hit_rate": float,    # 0..1
                    "stop_hit_rate": float,
                    "dip_hit_rate": float,       # MC dip prediction
                    "rally_hit_rate": float,     # MC rally prediction
                    "by_verdict": {label: {n, target_hit_rate, stop_hit_rate}},
                },
                60: {...},
            },
            "summary": str,
        }
    """
    snapshot_dates = _accumulated_snapshot_dates()
    days_of_history = len(snapshot_dates)

    if days_of_history < MIN_HISTORY_TRADING_DAYS:
        return {
            "status": "insufficient_data",
            "days_of_history": days_of_history,
            "days_needed": MIN_HISTORY_TRADING_DAYS,
            "summary": (
                f"Backtest accumulates as snapshots accrue. "
                f"{days_of_history} of {MIN_HISTORY_TRADING_DAYS} days "
                f"of history available — predictions become testable as "
                f"the horizon elapses on each accumulated snapshot."
            ),
        }

    horizons = (
        config.THRESHOLDS.horizons.primary_days,
        config.THRESHOLDS.horizons.secondary_days,
    )

    # Trades: one row per (ticker, snapshot_date, horizon) that is old
    # enough to evaluate. The same row carries the dip/rally check
    # plus per-user target/stop checks.
    all_trades: list[dict] = []

    for ticker in price_data_by_ticker.keys():
        prices = (price_data_by_ticker[ticker] or {}).get("prices") or []
        if not prices:
            continue
        snaps_for_ticker = _load_ticker_snapshots(ticker)
        for snap in snaps_for_ticker:
            snap_date = snap.get("run_date")
            if not snap_date:
                continue
            for h in horizons:
                row = _evaluate_snapshot(snap, prices, horizon_days=h)
                if row is not None:
                    all_trades.append(row)

    metrics = _aggregate(all_trades, horizons)
    if metrics["trades_evaluated"] == 0:
        return {
            "status": "insufficient_data",
            "days_of_history": days_of_history,
            "days_needed": MIN_HISTORY_TRADING_DAYS,
            "summary": (
                f"{days_of_history} days of snapshots on disk, but no "
                f"snapshots are old enough yet for their horizons to "
                f"have elapsed. The 30d hit-rate populates first, "
                f"the 60d follows."
            ),
        }

    return {
        "status": "ok",
        "days_of_history": days_of_history,
        "trades_evaluated": metrics["trades_evaluated"],
        "by_horizon": metrics["by_horizon"],
        "summary": (
            f"Predictions scored from {days_of_history} nights of "
            f"accumulated snapshots. Each (ticker, horizon) pair is "
            f"evaluated once the horizon's trading-day window has elapsed; "
            f"intraday highs and lows determine whether the predicted "
            f"rally and dip levels were touched."
        ),
    }


# ---------- per-snapshot evaluation ----------


def _evaluate_snapshot(snap: dict, all_prices: list[dict], horizon_days: int) -> dict | None:
    """Score one (ticker, snapshot, horizon). Returns None if the
    snapshot isn't yet old enough to evaluate (not enough future bars)."""
    snap_date = snap.get("run_date")
    if not snap_date:
        return None

    # Future bars: bars STRICTLY AFTER snap_date. The snapshot was made
    # using snap_date's closing data, so the testable window starts the
    # very next trading day.
    future_bars = [b for b in all_prices if (b.get("date") or "") > snap_date]
    needed = _required_future_bars(horizon_days)
    if len(future_bars) < needed:
        # Horizon hasn't elapsed yet — wait for more bars.
        return None
    window = future_bars[:needed]

    price_levels = (snap.get("price_levels") or {}).get("horizons") or {}
    # JSON keys are strings — try both forms.
    horizon_block = price_levels.get(horizon_days) or price_levels.get(str(horizon_days))
    if not horizon_block:
        return None

    dip_price = (horizon_block.get("dip") or {}).get("price")
    rally_price = (horizon_block.get("rally") or {}).get("price")

    # Dip hit: did the daily LOW ever touch the predicted dip level?
    # Rally hit: did the daily HIGH ever touch the predicted rally level?
    dip_hit = _touched_below(window, dip_price) if dip_price else None
    rally_hit = _touched_above(window, rally_price) if rally_price else None

    # Per-user verdicts + target/stop hits.
    users_out: dict[str, dict] = {}
    conviction_horizons = (snap.get("conviction") or {}).get("horizons") or {}
    user_block_map = conviction_horizons.get(horizon_days) or conviction_horizons.get(str(horizon_days)) or {}
    for user_name, user_block in user_block_map.items():
        targets = user_block.get("targets") or {}
        breakdown = user_block.get("breakdown") or {}
        target_price = targets.get("target")
        stop_price = targets.get("stop")
        if not target_price or not stop_price:
            continue
        first_passage = _first_passage(window, target_price=target_price, stop_price=stop_price)
        users_out[user_name] = {
            "verdict_label": breakdown.get("verdict_label", "—"),
            "target_price": target_price,
            "stop_price": stop_price,
            "target_hit": first_passage["target_hit"],
            "stop_hit": first_passage["stop_hit"],
            "first_passage": first_passage["outcome"],
            "first_passage_day": first_passage["first_passage_day"],
        }

    return {
        "ticker": snap.get("ticker"),
        "snapshot_date": snap_date,
        "horizon_days": horizon_days,
        "current_price": (snap.get("targets") or {}).get("current_price"),
        "predicted_dip_price": dip_price,
        "predicted_rally_price": rally_price,
        "dip_hit": dip_hit,
        "rally_hit": rally_hit,
        "actual_low": _window_min_low(window),
        "actual_high": _window_max_high(window),
        "users": users_out,
    }


def _touched_below(window: list[dict], price: float) -> bool:
    """True if any daily low (or close if low missing) touched or
    fell below `price` in the window."""
    for b in window:
        low = b.get("low")
        if low is None:
            low = b.get("adj_close") or b.get("close")
        if low is not None and low <= price:
            return True
    return False


def _touched_above(window: list[dict], price: float) -> bool:
    """True if any daily high (or close if high missing) touched or
    exceeded `price` in the window."""
    for b in window:
        high = b.get("high")
        if high is None:
            high = b.get("adj_close") or b.get("close")
        if high is not None and high >= price:
            return True
    return False


def _first_passage(window: list[dict], target_price: float, stop_price: float) -> dict:
    """Determine which of target / stop was touched first, intraday.

    Returns {outcome: 'target'|'stop'|'both'|'neither', target_hit, stop_hit,
    first_passage_day}.
    'both' = same trading session touched both — daily OHLC can't tell
    us which limit order would have filled first; the trader's realized
    P&L depends on intraday sequence we don't observe.
    """
    target_day = None
    stop_day = None
    for i, b in enumerate(window, start=1):
        high = b.get("high") or b.get("adj_close") or b.get("close")
        low = b.get("low") or b.get("adj_close") or b.get("close")
        if high is not None and high >= target_price and target_day is None:
            target_day = i
        if low is not None and low <= stop_price and stop_day is None:
            stop_day = i
        if target_day is not None and stop_day is not None:
            break

    target_hit = target_day is not None
    stop_hit = stop_day is not None
    if not target_hit and not stop_hit:
        outcome = "neither"
        fp_day = len(window)
    elif target_hit and not stop_hit:
        outcome = "target"
        fp_day = target_day
    elif stop_hit and not target_hit:
        outcome = "stop"
        fp_day = stop_day
    elif target_day < stop_day:
        outcome = "target"
        fp_day = target_day
    elif stop_day < target_day:
        outcome = "stop"
        fp_day = stop_day
    else:
        outcome = "both"
        fp_day = target_day
    return {
        "outcome": outcome,
        "target_hit": target_hit,
        "stop_hit": stop_hit,
        "first_passage_day": fp_day,
    }


def _window_min_low(window: list[dict]) -> float | None:
    lows = [b.get("low") for b in window if b.get("low") is not None]
    return min(lows) if lows else None


def _window_max_high(window: list[dict]) -> float | None:
    highs = [b.get("high") for b in window if b.get("high") is not None]
    return max(highs) if highs else None


# ---------- aggregation ----------


def _aggregate(trades: list[dict], horizons: tuple[int, ...]) -> dict:
    if not trades:
        return {"trades_evaluated": 0, "by_horizon": {}}

    by_horizon: dict[int, dict] = {}
    for h in horizons:
        rows = [t for t in trades if t["horizon_days"] == h]
        if not rows:
            by_horizon[h] = {
                "n": 0,
                "dip_hit_rate": None,
                "rally_hit_rate": None,
                "target_hit_rate": None,
                "stop_hit_rate": None,
                "by_verdict": {},
            }
            continue

        dip_rows = [r for r in rows if r["dip_hit"] is not None]
        rally_rows = [r for r in rows if r["rally_hit"] is not None]
        dip_hit_rate = (sum(1 for r in dip_rows if r["dip_hit"]) / len(dip_rows)) if dip_rows else None
        rally_hit_rate = (sum(1 for r in rally_rows if r["rally_hit"]) / len(rally_rows)) if rally_rows else None

        # Per-(verdict, horizon) target/stop hit rates. Sum users
        # across all rows (target/stop is per-user).
        verdict_buckets: dict[str, dict] = defaultdict(lambda: {
            "n": 0, "target_hits": 0, "stop_hits": 0,
            "first_passage": {"target": 0, "stop": 0, "both": 0, "neither": 0},
        })
        target_hits_total = 0
        stop_hits_total = 0
        target_total = 0
        for r in rows:
            for user_name, ub in (r.get("users") or {}).items():
                label = ub.get("verdict_label", "—")
                bucket = verdict_buckets[label]
                bucket["n"] += 1
                target_total += 1
                if ub.get("target_hit"):
                    bucket["target_hits"] += 1
                    target_hits_total += 1
                if ub.get("stop_hit"):
                    bucket["stop_hits"] += 1
                    stop_hits_total += 1
                fp = ub.get("first_passage", "neither")
                bucket["first_passage"][fp] = bucket["first_passage"].get(fp, 0) + 1

        by_verdict_out = {}
        for label, b in verdict_buckets.items():
            n = b["n"]
            by_verdict_out[label] = {
                "n": n,
                "target_hit_rate": b["target_hits"] / n if n else 0.0,
                "stop_hit_rate": b["stop_hits"] / n if n else 0.0,
                "first_passage_target_pct": b["first_passage"]["target"] / n if n else 0.0,
                "first_passage_stop_pct": b["first_passage"]["stop"] / n if n else 0.0,
                "first_passage_neither_pct": b["first_passage"]["neither"] / n if n else 0.0,
            }

        by_horizon[h] = {
            "n": len(rows),
            "n_user_evaluations": target_total,
            "dip_hit_rate": dip_hit_rate,
            "rally_hit_rate": rally_hit_rate,
            "target_hit_rate": (target_hits_total / target_total) if target_total else None,
            "stop_hit_rate": (stop_hits_total / target_total) if target_total else None,
            "by_verdict": by_verdict_out,
        }

    return {
        "trades_evaluated": len(trades),
        "by_horizon": by_horizon,
    }


# ---------- snapshot discovery ----------


def _accumulated_snapshot_dates() -> list[str]:
    """Union of all snapshot dates across all tickers on disk. The size
    of this set is "days of accumulated history" for the cold-start gate.
    """
    root = config.SNAPSHOTS_DIR
    if not root.exists():
        return []
    seen: set[str] = set()
    for ticker_dir in root.iterdir():
        if not ticker_dir.is_dir():
            continue
        for p in ticker_dir.glob("*.json"):
            seen.add(p.stem)
    return sorted(seen)


def _load_ticker_snapshots(ticker: str) -> list[dict]:
    """Load all snapshots on disk for a ticker, oldest first."""
    ticker_dir = config.SNAPSHOTS_DIR / ticker
    if not ticker_dir.exists():
        return []
    out: list[dict] = []
    for p in sorted(ticker_dir.glob("*.json")):
        try:
            with p.open() as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"failed to read snapshot {p}: {e}")
    return out
