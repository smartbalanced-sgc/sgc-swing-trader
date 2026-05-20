"""Walk-forward backtest harness.

Replays the engine on historical sample dates and compares predicted
verdict outcomes (P(target), P(stop), EV, verdict label) against
realized outcomes from actual subsequent prices. This is what separates
"the math looks right" from "the math worked."

How it works:

  For each (ticker, sample_date) pair:
    1. Build a "historical snapshot" using ONLY data available as of
       sample_date. Most pipeline steps work fine on truncated price
       data; some upstream inputs (analyst grades, fair value, news,
       options-implied move) require current-only FMP endpoints and
       are intentionally omitted (the conviction module's graceful
       degradation handles missing inputs by defaulting to "no veto,
       no haircut" - the backtest's verdicts are therefore CONSERVATIVE
       relative to a production run that has all the inputs).
    2. Run the verdict synthesis to get the verdict label, score, and
       MC-derived P(target) / P(stop) / EV per (user, horizon).
    3. Look at actual prices from sample_date+1 to sample_date+horizon
       trading days; compute the realized first-passage outcome
       (target hit first / stop hit first / neither) and the realized
       terminal return.
    4. Record one row per (sample_date, ticker, horizon) with both
       the predicted and realized values.

  After all snapshots are processed, aggregate:
    - Hit rate per verdict label (do ENTERs actually outperform SKIPs?)
    - Mean realized return per verdict
    - Calibration: predicted P(target) decile vs realized hit rate per
      decile (the engine's probabilities should map linearly to
      realized frequencies if the model is well-calibrated)

What this backtest CAN'T evaluate (intentionally documented):

  - News flow, analyst grades, fair-value premium, options-implied
    move: FMP Starter only exposes CURRENT versions of these. Treating
    them as if they were "as-of" would leak future data. Skipped.
  - Tier classifier sanity check (current vol may not match as-of vol):
    we use truncated-price vol for the as-of measured tier.
  - Multi-night trajectory: requires accumulated snapshot history that
    starts ACCUMULATING the day we ship to production. Defaults to
    "0 direction changes, stable" in the backtest, same as Step 8
    defaults for now.

  All of these default to "neutral" (no veto, no haircut) - so the
  backtest doesn't FALSE-FIRE haircuts that production would also not
  fire. It just LACKS some of the haircut signals production has.

Realized-outcome convention:

  Daily-close first-passage. Same monitoring discretization a real
  Trading 212 user would observe. No Brownian bridge correction on
  realized outcomes (it's not "what could have happened intraday" -
  it's "what was the actual close at the end of each day"). This
  intentionally matches the user's executional reality, not the
  continuous-time mathematical idealization.

Output: JSON file at data/backtest/{YYYY-MM-DD}.json with the full
run config, per-trade detail, and aggregated metrics.

CLI:

  python -m src.backtest --tickers NVDA AMAT IONQ --from 2025-06-01 --to 2026-04-01
  python -m src.backtest --tickers NVDA --from 2025-06-01 --to 2026-04-01 --every 14
  python -m src.backtest --tickers NVDA --as-of 2026-01-15  # single-date dry run

The backtest can be run today on whatever historical data exists, then
re-run periodically as more nights of production accumulate. Eventually
the "predicted vs realized" comparison becomes the most important
diagnostic in the system.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml

from src import config, conviction, tier_classifier
from src.pipeline import (
    data as data_step,
    regime as regime_step,
    volatility as vol_step,
    targets as targets_step,
    monte_carlo as mc_step,
    analytic_verifier as av_step,
    verdict as verdict_step,
)

logger = logging.getLogger(__name__)


# ---------- public API ----------


def run(
    tickers: list[str],
    as_of_start: date,
    as_of_end: date,
    sample_every_days: int = 7,
    horizons: tuple[int, ...] | None = None,
) -> dict:
    """Run the full walk-forward backtest.

    Args:
        tickers: list of ticker symbols to backtest.
        as_of_start: first sample date.
        as_of_end: last sample date. Must be at least max(horizons)
            trading days in the past (or realized outcomes won't exist).
        sample_every_days: gap between consecutive sample dates
            (calendar days). 7 = weekly is a reasonable default;
            smaller = more samples, but also more correlated (samples
            7 days apart see overlapping horizons).
        horizons: which horizons to backtest. Defaults to config.HORIZONS.

    Returns the run payload (also written to disk):
        {
            "run_config": {...},
            "trades": [{...}, ...],
            "metrics": {...},
        }
    """
    horizons = horizons or config.HORIZONS
    watchlist = _load_watchlist()

    all_trades: list[dict] = []
    skipped: list[dict] = []

    for ticker in tickers:
        ticker = ticker.upper()
        entry = watchlist.get(ticker)
        if entry is None:
            logger.warning(f"{ticker}: not in watchlist; using default tier B")
            entry = {"tier": "B", "holders": {"backtest": {"state": "watching"}}}
        else:
            # Backtest evaluates from the "watching" perspective only -
            # entry/exit decisions are what the engine is for. Strip
            # user-specific entered state so the backtest sees a
            # consistent watching-state evaluation.
            entry = {
                "tier": entry.get("tier", "B"),
                "holders": {"backtest": {"state": "watching"}},
            }

        try:
            full_price_data = data_step.fetch(ticker)
        except Exception as e:  # noqa: BLE001
            logger.error(f"{ticker}: data fetch failed: {e}; skipping ticker")
            skipped.append({"ticker": ticker, "reason": f"data fetch failed: {e}"})
            continue

        full_earnings = _fetch_full_earnings_history(ticker)

        sample_dates = _generate_sample_dates(
            full_price_data, as_of_start, as_of_end, sample_every_days
        )
        logger.info(f"{ticker}: {len(sample_dates)} sample dates")

        for as_of in sample_dates:
            try:
                trade_rows = _evaluate_at(
                    ticker=ticker,
                    as_of=as_of,
                    entry=entry,
                    full_price_data=full_price_data,
                    full_earnings=full_earnings,
                    horizons=horizons,
                )
                all_trades.extend(trade_rows)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{ticker} {as_of}: {e}")
                skipped.append({
                    "ticker": ticker, "as_of": as_of.isoformat(),
                    "reason": str(e),
                })

    metrics = _aggregate_metrics(all_trades)

    payload = {
        "run_config": {
            "tickers": tickers,
            "as_of_start": as_of_start.isoformat(),
            "as_of_end": as_of_end.isoformat(),
            "sample_every_days": sample_every_days,
            "horizons": list(horizons),
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "summary": {
            "trades_evaluated": len(all_trades),
            "skipped": len(skipped),
        },
        "metrics": metrics,
        "trades": all_trades,
        "skipped": skipped,
    }

    return payload


# ---------- per-(ticker, as_of) evaluation ----------


def _evaluate_at(
    ticker: str,
    as_of: date,
    entry: dict,
    full_price_data: dict,
    full_earnings: list[dict],
    horizons: tuple[int, ...],
) -> list[dict]:
    """Build a historical snapshot at `as_of`, run verdict, compute
    realized outcomes for each horizon. Returns one row per horizon."""
    truncated_prices = _truncate_prices(full_price_data["prices"], as_of)
    if len(truncated_prices) < 252 + 1:
        raise RuntimeError(
            f"insufficient history (only {len(truncated_prices)} bars before {as_of}); "
            f"need >= 253 for vol + regime"
        )

    snap = _build_historical_snapshot(
        ticker=ticker,
        as_of=as_of,
        truncated_prices=truncated_prices,
        full_profile=full_price_data.get("profile") or {},
        full_earnings=full_earnings,
        entry=entry,
    )

    # Run the pipeline math
    tier = entry.get("tier")
    snap["tier_classifier"] = _safe_classify(snap, tier)
    snap["regime"] = regime_step.detect(ticker, snap["_price_data"], tier=tier)
    snap["volatility"] = vol_step.forecast(ticker, snap["_price_data"], tier=tier)
    snap["targets"] = targets_step.derive(ticker, entry, snap)

    # MC needs a deterministic run_date — use the as_of for reproducibility.
    snap["monte_carlo"] = mc_step.simulate(ticker, snap, run_date=as_of.isoformat())
    if snap["monte_carlo"].get("status") == "ok":
        snap["price_levels"] = snap["monte_carlo"].pop("price_levels", {"status": "pending"})
        snap["daily_path"] = snap["monte_carlo"].pop("daily_path", {"status": "pending"})
    else:
        raise RuntimeError(f"monte_carlo failed: {snap['monte_carlo'].get('reason')}")

    snap["analytic_verifier"] = av_step.verify(ticker, snap)
    snap["fair_value"] = {"status": "pending", "reason": "skipped in backtest (current-only data)"}

    # Verdict
    snap["conviction"] = verdict_step.synthesize(ticker, snap, entry)
    if snap["conviction"].get("status") != "ok":
        raise RuntimeError(f"verdict failed: {snap['conviction'].get('reason')}")

    # Future prices for realized outcomes
    future_prices = _truncate_prices_after(full_price_data["prices"], as_of)

    rows = []
    for h in horizons:
        user_block = (snap["conviction"]["horizons"].get(h) or {}).get("backtest")
        if not user_block:
            continue
        breakdown = user_block["breakdown"]
        targets = user_block["targets"]
        user_mc = ((snap["monte_carlo"]["users"] or {}).get("backtest") or {}).get("horizons", {}).get(h)
        if not user_mc:
            continue

        realized = _compute_realized_outcome(
            future_prices=future_prices,
            target_price=targets["target"],
            stop_price=targets["stop"],
            horizon_days=h,
        )
        if realized is None:
            # Not enough future history for this horizon
            continue

        rows.append({
            "ticker": ticker,
            "as_of": as_of.isoformat(),
            "horizon": h,
            "current_price": snap["targets"]["current_price"],
            "target_price": targets["target"],
            "stop_price": targets["stop"],
            # Predicted (engine output as-of)
            "predicted_verdict": breakdown["verdict_label"],
            "predicted_score": breakdown["final_score"],
            "predicted_p_target": user_mc["p_target"],
            "predicted_p_stop": user_mc["p_stop"],
            "predicted_ev_pct": user_mc["ev_pct"],
            "predicted_ev_normalized": user_mc["ev_normalized"],
            # Realized
            "realized_outcome": realized["outcome"],          # "target"|"stop"|"neither"
            "realized_first_passage_day": realized["first_passage_day"],
            "realized_terminal_pct": realized["terminal_pct"],
            "realized_return_pct": realized["realized_return_pct"],
        })

    return rows


# ---------- historical snapshot construction ----------


def _build_historical_snapshot(
    ticker: str,
    as_of: date,
    truncated_prices: list[dict],
    full_profile: dict,
    full_earnings: list[dict],
    entry: dict,
) -> dict:
    """Construct a snap-like dict with truncated price data + historical
    earnings + filtered reactions. Missing fields (news, analyst, FV)
    are intentionally left empty/pending - graceful degradation
    elsewhere handles them.
    """
    snap: dict = {
        "ticker": ticker,
        "run_date": as_of.isoformat(),
        "tier_anchor": entry.get("tier"),
        "as_of": truncated_prices[-1]["date"] if truncated_prices else None,
        "data": {
            "status": "ok",
            "profile": full_profile,
            "sanity": {"overall": "ok"},
            "bar_count": len(truncated_prices),
        },
        "_price_data": {
            "ticker": ticker,
            "as_of": truncated_prices[-1]["date"] if truncated_prices else None,
            "profile": full_profile,
            "prices": truncated_prices,
            "sanity": {"overall": "ok"},
        },
        "catalyst": _build_catalyst_block(
            full_earnings, truncated_prices, as_of, ticker
        ),
    }
    return snap


def _build_catalyst_block(
    full_earnings: list[dict],
    truncated_prices: list[dict],
    as_of: date,
    ticker: str,
) -> dict:
    """Build a catalyst payload from full earnings history + truncated
    prices, filtered to data available as-of `as_of`. No news, no
    analyst data, no options-implied move - those are current-only
    endpoints and treating them as historical would be data leakage.
    """
    if not full_earnings:
        return {"status": "ok", "next_event": None, "distance_sessions": None,
                "historical_reactions": [], "news_bullets": [],
                "analyst_revisions": {}, "engine_recommendation": "",
                "options_implied_move_pct": None,
                "proximity_haircut": 0.0,
                "in_horizon_30d": False, "in_horizon_60d": False}

    as_of_iso = as_of.isoformat()

    # Past earnings: dates before as_of — used for historical_reactions
    past = [e for e in full_earnings if (e.get("date") or "")[:10] < as_of_iso]
    # Future earnings: dates >= as_of — used for next_event
    future = [e for e in full_earnings if (e.get("date") or "")[:10] >= as_of_iso]
    future.sort(key=lambda e: (e.get("date") or ""))
    next_event_date = future[0].get("date")[:10] if future else None

    distance = _sessions_between(as_of, date.fromisoformat(next_event_date)) if next_event_date else None

    # Historical reactions: close-to-close around each past earnings date.
    historical_reactions = _compute_historical_reactions(past, truncated_prices, as_of)

    proximity_haircut = 0.0
    in_horizon_30d = False
    in_horizon_60d = False
    if distance is not None:
        # Use the same proximity haircut bands as production
        bands = config.THRESHOLDS.conviction.vetoes.catalyst.proximity_haircut_bands
        for band in bands:
            if distance <= band.max_sessions:
                proximity_haircut = band.haircut
                break
        in_horizon_30d = distance <= config.THRESHOLDS.horizons.primary_days
        in_horizon_60d = distance <= config.THRESHOLDS.horizons.secondary_days

    return {
        "status": "ok",
        "ticker": ticker,
        "as_of": as_of_iso,
        "next_event": {"type": "earnings", "date": next_event_date} if next_event_date else None,
        "distance_sessions": distance,
        "historical_reactions": historical_reactions,
        "options_implied_move_pct": None,    # current-only - skip
        "news_bullets": [],                  # current-only - skip
        "analyst_revisions": {},             # current-only - skip
        "engine_recommendation": "",
        "proximity_haircut": proximity_haircut,
        "in_horizon_30d": in_horizon_30d,
        "in_horizon_60d": in_horizon_60d,
        "short_interest": None,              # current-only - skip
        "price_target_consensus": None,      # current-only - skip
        "errors": [],
    }


def _compute_historical_reactions(
    past_earnings: list[dict], truncated_prices: list[dict], as_of: date
) -> list[dict]:
    """Reactions for past earnings that ALREADY HAPPENED before as_of,
    using only prices up to as_of (no future leak). Matches the
    production catalyst._historical_reactions algorithm."""
    if not past_earnings or not truncated_prices:
        return []
    import bisect
    price_by_date = {p["date"]: p for p in truncated_prices}
    sorted_dates = sorted(price_by_date.keys())
    as_of_iso = as_of.isoformat()

    reactions: list[dict] = []
    for e in past_earnings:
        d_str = (e.get("date") or "")[:10]
        if not d_str or d_str >= as_of_iso:
            continue
        # Find on-or-before and strictly-after
        idx_before = bisect.bisect_right(sorted_dates, d_str) - 1
        idx_after = bisect.bisect_right(sorted_dates, d_str)
        if idx_before < 0 or idx_after >= len(sorted_dates):
            continue
        before_date = sorted_dates[idx_before]
        after_date = sorted_dates[idx_after]
        if after_date >= as_of_iso:
            # Reaction happens after as_of - data leak; skip.
            continue
        close_before = price_by_date[before_date].get("close")
        close_after = price_by_date[after_date].get("close")
        if not close_before or not close_after:
            continue
        reaction_pct = (close_after - close_before) / close_before * 100
        reactions.append({
            "date": d_str, "type": "earnings",
            "reaction_pct": round(reaction_pct, 2),
        })

    reactions.sort(key=lambda r: r["date"], reverse=True)
    return reactions[:config.THRESHOLDS.dashboard.earnings_reactions_lookback]


# ---------- realized outcome ----------


def _compute_realized_outcome(
    future_prices: list[dict],
    target_price: float,
    stop_price: float,
    horizon_days: int,
) -> dict | None:
    """Determine first-passage outcome from actual subsequent prices.

    Daily-close first-passage (matches the trader's executional reality
    on Trading 212 - they see closes, not intraday). No Brownian bridge
    on realized outcomes; this is what actually happened, not a
    simulation.

    Returns None if there are fewer than `horizon_days` future prices
    available (can't evaluate the trade).
    """
    if len(future_prices) < horizon_days:
        return None
    window = future_prices[:horizon_days]

    target_first_day = None
    stop_first_day = None
    for i, p in enumerate(window, start=1):
        close = p.get("adj_close") or p.get("close")
        if close is None:
            continue
        if close >= target_price and target_first_day is None:
            target_first_day = i
        if close <= stop_price and stop_first_day is None:
            stop_first_day = i
        if target_first_day is not None and stop_first_day is not None:
            break

    if target_first_day is None and stop_first_day is None:
        outcome = "neither"
        first_passage_day = horizon_days
    elif target_first_day is not None and (stop_first_day is None or target_first_day < stop_first_day):
        outcome = "target"
        first_passage_day = target_first_day
    elif stop_first_day is not None and (target_first_day is None or stop_first_day < target_first_day):
        outcome = "stop"
        first_passage_day = stop_first_day
    else:
        # Same-day touch (rare with daily resolution; resolve to "both")
        outcome = "both"
        first_passage_day = target_first_day

    terminal_close = window[-1].get("adj_close") or window[-1].get("close")
    open_close = future_prices[0].get("adj_close") or future_prices[0].get("close")
    # Realized return: at the price the trader would have transacted at
    # given the first-passage outcome (target or stop price if hit, else
    # terminal close). Compared to the "current price" at as_of (which
    # is the last bar BEFORE as_of in the truncated price series).
    return {
        "outcome": outcome,
        "first_passage_day": first_passage_day,
        "terminal_pct": (terminal_close - open_close) / open_close * 100 if open_close else 0.0,
        "realized_return_pct": _outcome_return_pct(
            outcome, target_price, stop_price, terminal_close, open_close
        ),
    }


def _outcome_return_pct(
    outcome: str, target: float, stop: float, terminal: float, anchor: float
) -> float:
    """Return realized return based on outcome (what the trader would have
    actually realized in P&L)."""
    if not anchor:
        return 0.0
    if outcome == "target":
        return (target - anchor) / anchor * 100
    if outcome == "stop":
        return (stop - anchor) / anchor * 100
    if outcome == "both":
        return ((target + stop) / 2 - anchor) / anchor * 100
    return (terminal - anchor) / anchor * 100 if terminal else 0.0


# ---------- aggregation ----------


def _aggregate_metrics(trades: list[dict]) -> dict:
    """Per-verdict hit rate + mean realized return + calibration."""
    if not trades:
        return {"trades_evaluated": 0}

    by_verdict: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_verdict[t["predicted_verdict"]].append(t)

    per_verdict_stats = {}
    for verdict_label, rows in by_verdict.items():
        n = len(rows)
        target_hits = sum(1 for r in rows if r["realized_outcome"] == "target")
        stop_hits = sum(1 for r in rows if r["realized_outcome"] == "stop")
        neither = sum(1 for r in rows if r["realized_outcome"] == "neither")
        mean_realized = float(np.mean([r["realized_return_pct"] for r in rows]))
        mean_predicted_ev = float(np.mean([r["predicted_ev_pct"] for r in rows]))
        mean_predicted_p_target = float(np.mean([r["predicted_p_target"] for r in rows]))
        realized_hit_rate = target_hits / n if n else 0.0
        per_verdict_stats[verdict_label] = {
            "n": n,
            "target_hit_count": target_hits,
            "stop_hit_count": stop_hits,
            "neither_count": neither,
            "realized_target_hit_rate": realized_hit_rate,
            "realized_stop_hit_rate": stop_hits / n if n else 0.0,
            "mean_realized_return_pct": mean_realized,
            "mean_predicted_ev_pct": mean_predicted_ev,
            "mean_predicted_p_target": mean_predicted_p_target,
            "ev_calibration_delta_pct": mean_realized - mean_predicted_ev,
            "p_target_calibration_delta_pp": (realized_hit_rate - mean_predicted_p_target) * 100,
        }

    # Calibration: bucket all trades by predicted P(target) decile,
    # compare to realized hit rate per bucket. If the engine's
    # probabilities are well-calibrated, the realized hit rate in
    # the 30-40% bucket should be roughly 30-40%.
    calibration_buckets = _build_calibration_buckets(trades)

    return {
        "trades_evaluated": len(trades),
        "by_verdict": per_verdict_stats,
        "p_target_calibration": calibration_buckets,
    }


def _build_calibration_buckets(trades: list[dict]) -> list[dict]:
    """Decile buckets of predicted P(target) with realized hit rate."""
    buckets = [{"low": d / 10, "high": (d + 1) / 10, "rows": []} for d in range(10)]
    for t in trades:
        p = t["predicted_p_target"]
        idx = min(int(p * 10), 9)
        buckets[idx]["rows"].append(t)
    out = []
    for b in buckets:
        rows = b["rows"]
        if not rows:
            out.append({
                "predicted_p_target_low": b["low"],
                "predicted_p_target_high": b["high"],
                "n": 0,
                "realized_hit_rate": None,
                "mean_predicted_p_target": None,
            })
        else:
            hits = sum(1 for r in rows if r["realized_outcome"] == "target")
            out.append({
                "predicted_p_target_low": b["low"],
                "predicted_p_target_high": b["high"],
                "n": len(rows),
                "realized_hit_rate": hits / len(rows),
                "mean_predicted_p_target": float(np.mean([r["predicted_p_target"] for r in rows])),
            })
    return out


# ---------- helpers ----------


def _truncate_prices(prices: list[dict], as_of: date) -> list[dict]:
    """Keep only prices STRICTLY BEFORE as_of (no future leak).
    The most recent kept price is the "current price" at as_of."""
    as_of_iso = as_of.isoformat()
    return [p for p in prices if (p.get("date") or "") < as_of_iso]


def _truncate_prices_after(prices: list[dict], as_of: date) -> list[dict]:
    """Prices ON or AFTER as_of (for realized-outcome simulation)."""
    as_of_iso = as_of.isoformat()
    return [p for p in prices if (p.get("date") or "") >= as_of_iso]


def _sessions_between(start: date, end: date) -> int:
    """Approximate trading sessions between two dates. For backtest
    purposes a calendar-day-based approximation is fine."""
    if end <= start:
        return 0
    delta_days = (end - start).days
    # ~5/7 of calendar days are trading days
    return int(delta_days * 5.0 / 7.0)


def _generate_sample_dates(
    full_price_data: dict, start: date, end: date, every: int
) -> list[date]:
    """Generate sample dates at `every` calendar-day intervals, only
    keeping dates that are actual trading days (i.e., dates present
    in the price series)."""
    prices = full_price_data.get("prices") or []
    date_set = {p["date"] for p in prices}
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.isoformat() in date_set:
            out.append(cursor)
        cursor += timedelta(days=every)
    return out


def _safe_classify(snap: dict, tier: str | None) -> dict:
    try:
        cls = tier_classifier.classify(snap["_price_data"])
        cls["status"] = "ok"
        return cls
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "reason": str(e),
                "measured_tier": tier or "B",
                "properties": {"adv_usd": {"value": 1e9}}}


def _fetch_full_earnings_history(ticker: str) -> list[dict]:
    """Pull every earnings event for this ticker from FMP /earnings.
    Returns empty list on failure (catalyst block falls back gracefully).
    """
    try:
        from src.data_sources import fmp
        body = fmp.get("earnings", ticker)
        if isinstance(body, list):
            return body
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{ticker}: full earnings fetch failed: {e}")
    return []


def _load_watchlist() -> dict:
    if not config.WATCHLIST_PATH.exists():
        return {}
    with config.WATCHLIST_PATH.open() as f:
        return yaml.safe_load(f) or {}


# ---------- CLI ----------


def _print_summary(payload: dict) -> None:
    cfg = payload["run_config"]
    summary = payload["summary"]
    metrics = payload["metrics"]
    print()
    print(f"=== Backtest run ===")
    print(f"  Tickers:        {', '.join(cfg['tickers'])}")
    print(f"  Window:         {cfg['as_of_start']} to {cfg['as_of_end']}, every {cfg['sample_every_days']}d")
    print(f"  Horizons:       {cfg['horizons']}")
    print(f"  Trades:         {summary['trades_evaluated']} evaluated, {summary['skipped']} skipped")
    if summary["trades_evaluated"] == 0:
        return

    print()
    print("=== Per-verdict performance ===")
    for verdict_label, stats in (metrics.get("by_verdict") or {}).items():
        print(f"\n  {verdict_label}: n={stats['n']}")
        print(f"    Target hit:     {stats['target_hit_count']}/{stats['n']} = {stats['realized_target_hit_rate']*100:.1f}%")
        print(f"    Stop hit:       {stats['stop_hit_count']}/{stats['n']} = {stats['realized_stop_hit_rate']*100:.1f}%")
        print(f"    Mean realized:  {stats['mean_realized_return_pct']:+.2f}%")
        print(f"    Mean predicted EV: {stats['mean_predicted_ev_pct']:+.2f}%")
        print(f"    EV calibration: {stats['ev_calibration_delta_pct']:+.2f}% (realized vs predicted)")
        print(f"    P(target) calibration: {stats['p_target_calibration_delta_pp']:+.1f}pp (realized hit rate vs mean predicted)")

    print()
    print("=== P(target) calibration buckets ===")
    print(f"  {'Predicted band':<22} {'n':<6} {'Mean predicted':<16} {'Realized hit rate':<18}")
    for b in metrics.get("p_target_calibration", []):
        if b["n"] == 0:
            continue
        band = f"{b['predicted_p_target_low']*100:.0f}-{b['predicted_p_target_high']*100:.0f}%"
        print(
            f"  {band:<22} {b['n']:<6} {b['mean_predicted_p_target']*100:>6.1f}%          "
            f"{b['realized_hit_rate']*100:>6.1f}%"
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Walk-forward backtest.")
    parser.add_argument("--tickers", nargs="+", default=None, help="ticker(s); defaults to watchlist")
    parser.add_argument("--from", dest="from_date", default=None, help="start as-of date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, help="end as-of date YYYY-MM-DD")
    parser.add_argument("--every", type=int, default=7, help="sample every N calendar days (default 7)")
    parser.add_argument("--as-of", default=None, help="single as-of date (dry run on one date)")
    parser.add_argument("--out", default=None, help="output JSON path (default data/backtest/{run-date}.json)")
    args = parser.parse_args()

    tickers = args.tickers or list(_load_watchlist().keys())
    if not tickers:
        print("ERROR: no tickers given and watchlist empty", file=sys.stderr)
        return 1

    if args.as_of:
        single = date.fromisoformat(args.as_of)
        as_of_start = single
        as_of_end = single
    else:
        # Defaults: from 1 year ago, to 90 days ago (so 60d horizon has realized outcomes)
        today = date.today()
        as_of_end = date.fromisoformat(args.to_date) if args.to_date else today - timedelta(days=90)
        as_of_start = date.fromisoformat(args.from_date) if args.from_date else today - timedelta(days=365)

    payload = run(
        tickers=tickers,
        as_of_start=as_of_start,
        as_of_end=as_of_end,
        sample_every_days=args.every,
    )

    # Write to disk
    out_path = Path(args.out) if args.out else (
        config.DATA_DIR / "backtest" / f"{date.today().isoformat()}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nWrote {out_path} ({len(payload['trades'])} trades)")

    _print_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
