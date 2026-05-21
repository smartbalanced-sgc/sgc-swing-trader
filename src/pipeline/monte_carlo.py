"""Step 6 - Monte Carlo simulation with earnings-jump overlay.

Simulates N price paths per ticker using Geometric Brownian Motion
with three v1-specific augmentations that matter for our swing-trade
use case:

  1. **Drift from regime** - the SDE drift parameter comes from
     `snap["regime"]["annualized_drift_implied"]`, which is the
     regime-conditioned expected return (uptrend_quiet -> +15%/yr,
     downtrend -> -20%/yr, etc.). Itô correction applied: the SDE
     drift is the continuously-compounded log of (1 + arithmetic
     return), so `mu_sde = log(1 + mu_arith)`.

  2. **Variance from GARCH forecast** - sigma_annual comes from
     `snap["volatility"]["forecast_30d_pct"]`. Treated as
     horizon-constant for v1 (deterministic vol path could be wired
     later by walking the GARCH recursion forward, but the
     improvement is marginal at our horizons).

  3. **Earnings-jump overlay** - if `snap["catalyst"]["next_event"]`
     is an earnings event within the simulation horizon AND
     `snap["catalyst"]["historical_reactions"]` is non-empty, the GBM
     step on that day is REPLACED with a bootstrap draw from the
     ticker's empirical earnings reactions. This is the
     v1-distinguishing feature: textbook GBM ignores known-date
     jumps and systematically underestimates P(stop) for trades
     that span an earnings print. For NVDA reporting tomorrow, this
     matters enormously.

First-passage logic:

  For each (user, horizon) pair we test path-by-path whether the
  daily-close series first crossed the user's target_price or
  stop_price. Outcomes per path: target_wins / stop_wins / both /
  neither. Aggregates -> p_target, p_stop, p_neither.

  EV is computed per path as the realized return at the first hit
  (target/stop) or at the horizon end (neither), then averaged.
  ev_normalized = E[return_pct] / |stop_pct| - "units of risk."

  Documented limitation: daily-close first-passage understates
  intraday stop hits. Step 7's PDE-based analytic verifier doesn't
  have this limitation and is the cross-check.

Per-horizon dip/rally zones (for price_levels panel):

  dip_price = the value P such that `dip_conviction_percentile`
              of paths touched at or below P on the way down.
              Computed as the percentile of per-path minimums.
  rally_price = mirror, percentile of per-path maximums.

  Date range: median day-index across paths of the
              min/max touch, +- date_range_half_width_sessions.

Daily-path summary (for the daily_path panel):

  One representative scenario - the median price per day across all
  paths - with dip/rally zone tinting on days that fall within the
  zone date ranges.

Random seed:

  np.random.default_rng(seed=hash((ticker, run_date)) % 2**32)
  -> same-day re-runs of `python -m src.main` produce identical MC
  results (matches the snapshot-overwrite philosophy), but different
  tickers / different days draw independent random streams.

Output schema (returned by simulate, unpacked by main.py into three
snap blocks):

    {
        "status": "ok",
        "fetched_at": ISO timestamp,
        "n_paths": int,
        "model_components": {
            "gbm": True,
            "earnings_jump_overlay": True | False,
            "earnings_jump_day_30d": int | None,
            "earnings_jump_day_60d": int | None,
            "earnings_reactions_count": int,
        },
        "drift_annualized": float,     # from regime
        "vol_annualized": float,       # from GARCH
        "users": {
            "aidy": {
                "target_price": float, "stop_price": float,
                "target_source": str,
                "horizons": {
                    30: {p_target, p_stop, p_both, p_neither,
                         ev_normalized, ev_pct, mean_terminal_pct},
                    60: {...},
                },
            },
            ...
        },
        "price_levels": {              # promoted to snap["price_levels"]
            "current_price": float,
            "rsi": float | None,
            "high_60d": float | None,
            "horizons": {
                30: {dip: {price, date_range}, rally: {price, date_range}},
                60: {...},
            },
        },
        "daily_path": {                # promoted to snap["daily_path"]
            "days": [
                {"day": 1, "date": "YYYY-MM-DD", "median_price": float,
                 "zone": "" | "dip" | "rally"},
                ...
            ],
        },
    }

CLI smoke test:

    python -m src.pipeline.monte_carlo NVDA
    python -m src.pipeline.monte_carlo NVDA AMAT IONQ
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas_market_calendars as mcal

from src import config
from src.pipeline import swing_mode

logger = logging.getLogger(__name__)

NYSE_CAL = mcal.get_calendar("XNYS")

_MC = config.THRESHOLDS.monte_carlo
_EARNINGS_JUMP_ON = _MC.earnings_jump_overlay
_BRIDGE_CORRECTION_ENABLED = getattr(_MC, "brownian_bridge_correction", True)
_DAILY_PATH_DAYS = _MC.daily_path_days

N_PATHS = config.MC_PATHS
HORIZONS = config.HORIZONS  # (30, 60)

_PL = config.THRESHOLDS.price_levels
_DIP_PCTILE = _PL.dip_conviction_percentile
_RALLY_PCTILE = _PL.rally_conviction_percentile
_DATE_HALF_WIDTH = _PL.date_range_half_width_sessions

TRADING_DAYS_PER_YEAR = 252


# ---------- public API ----------


def simulate(ticker: str, snap: dict, run_date: str | None = None) -> dict:
    """Run Monte Carlo for one ticker. Returns the full payload
    (mc stats + price_levels + daily_path); main.py unpacks the sub-
    blocks into the corresponding snap keys."""
    if run_date is None:
        run_date = snap.get("run_date") or date.today().isoformat()

    # --- Pull inputs from upstream snap blocks ---
    targets_block = snap.get("targets") or {}
    if targets_block.get("status") != "ok":
        return _fail(f"targets unavailable: {targets_block.get('reason', 'no targets block')}")

    regime_block = snap.get("regime") or {}
    if regime_block.get("status") != "ok":
        return _fail(f"regime unavailable: {regime_block.get('reason', 'no regime block')}")

    vol_block = snap.get("volatility") or {}
    if vol_block.get("status") != "ok":
        return _fail(f"volatility unavailable: {vol_block.get('reason', 'no vol block')}")

    current_price = targets_block["current_price"]
    drift_annualized = regime_block["annualized_drift_implied"]
    sigma_annualized = vol_block["forecast_30d_pct"]

    # --- Earnings jump info from catalyst (optional) ---
    cat = snap.get("catalyst") or {}
    earnings_reactions: list[float] = []
    earnings_jump_day: int | None = None
    options_implied_move: float | None = None
    if _EARNINGS_JUMP_ON:
        ne = cat.get("next_event") or {}
        if (ne.get("type") == "earnings"
                and cat.get("distance_sessions") is not None
                and cat.get("historical_reactions")):
            dist = int(cat["distance_sessions"])
            # distance_sessions=0 means "earnings TODAY" - typically
            # AMC (after market close), so the reaction lands in the
            # day-1 close-to-close window. distance_sessions=N (N>=1)
            # means earnings on day N, reaction in close-to-close from
            # day N-1 to day N (which we represent as the day-N step in
            # the simulation). For distance=0 we clamp to day 1 since
            # any AMC reaction will be picked up by the day-1 close.
            if 0 <= dist <= max(HORIZONS):
                earnings_jump_day = max(dist, 1)
                earnings_reactions = [
                    float(r["reaction_pct"]) / 100.0
                    for r in cat["historical_reactions"]
                    if r.get("reaction_pct") is not None
                ]
                if not earnings_reactions:
                    earnings_jump_day = None  # no reactions = no jump
                else:
                    # Optional second variance source: the market's
                    # straddle-implied move from the option chain
                    # (catalyst step fetches this via yfinance when an
                    # earnings event is upcoming). When present, MC
                    # blends 50/50 with the empirical bootstrap so
                    # the jump distribution reflects BOTH the historical
                    # reaction pattern (direction bias) AND the market's
                    # current variance forecast (the option market's
                    # expected magnitude). Caveat: implied move is a
                    # FRACTIONAL move (e.g. 0.06 = ±6%), used as the
                    # std-dev of a Gaussian centered at the empirical
                    # log-return mean.
                    raw = cat.get("options_implied_move_pct")
                    if raw is not None and 0.01 <= raw <= 0.50:
                        options_implied_move = float(raw)

    # --- Random seed: reproducible per (ticker, run_date) ---
    seed = _seed_for(ticker, run_date)
    rng = np.random.default_rng(seed)

    # --- Simulate paths out to the longer horizon ---
    # When earnings-jump overlay is active we ALSO generate a parallel
    # GBM-only path set (same random draws, jump day not overwritten)
    # so the analytic_verifier step (PDE-based cross-check) has
    # something to compare against — PDE assumes continuous diffusion
    # and can't represent the discrete empirical-reaction jump.
    n_days = max(HORIZONS)
    paths, paths_gbm_only = _simulate_paths(
        s0=current_price,
        drift_annual=drift_annualized,
        sigma_annual=sigma_annualized,
        n_paths=N_PATHS,
        n_days=n_days,
        rng=rng,
        earnings_jump_day=earnings_jump_day,
        earnings_reactions=earnings_reactions,
        options_implied_move=options_implied_move,
    )

    # --- Forecast trading dates aligned to path day indices ---
    forecast_dates = _build_forecast_dates(n_days)

    # --- Per-user, per-horizon first-passage stats ---
    # When jump overlay is active, also compute first-passage on the
    # parallel GBM-only paths and surface as `p_target_gbm_only` /
    # `p_stop_gbm_only` for the analytic_verifier cross-check.
    users_out: dict[str, dict] = {}
    for user, user_targets in targets_block["users"].items():
        users_out[user] = _compute_user_stats(
            user_targets=user_targets,
            paths=paths,
            paths_gbm_only=paths_gbm_only,
            current_price=current_price,
            horizons=HORIZONS,
            sigma_annual=sigma_annualized,
            rng=rng,
            earnings_jump_day=earnings_jump_day,
        )

    # --- Dip/rally price-level zones per horizon ---
    price_levels_horizons: dict[int, dict] = {}
    swing_mode_horizons: dict[int, dict] = {}
    for h in HORIZONS:
        paths_h = paths[:, :h]
        price_levels_horizons[h] = _compute_zones_for_horizon(
            paths_h=paths_h,
            forecast_dates=forecast_dates[:h],
        )
        # Swing-mode metrics: planned dip→rally trade analytics.
        # Re-uses the same paths array — no new simulation.
        swing_mode_horizons[h] = swing_mode.compute_for_horizon(
            paths_h=paths_h,
            current_price=current_price,
        )

    # --- Aux fields for price_levels panel (RSI, 60d high) ---
    raw_prices = snap.get("_price_data", {}).get("prices") or []
    rsi = _compute_rsi_14(raw_prices)
    high_60d = _compute_high_60d(raw_prices)

    price_levels_out = {
        "status": "ok",
        "current_price": current_price,
        "rsi": rsi,
        "high_60d": high_60d,
        "horizons": price_levels_horizons,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    swing_mode_out = {
        "status": "ok",
        "current_price": current_price,
        "horizons": swing_mode_horizons,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # --- Daily-path summary for the panel ---
    daily_path_out = _compute_daily_path(
        paths=paths,
        forecast_dates=forecast_dates,
        days_to_show=min(_DAILY_PATH_DAYS, n_days),
        zone_horizons=price_levels_horizons,
        earnings_jump_day=earnings_jump_day,
    )

    return {
        "status": "ok",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_paths": N_PATHS,
        "model_components": {
            "gbm": True,
            "earnings_jump_overlay": earnings_jump_day is not None,
            "earnings_jump_day": earnings_jump_day,
            "earnings_jump_day_in_30d_horizon": earnings_jump_day is not None and earnings_jump_day <= HORIZONS[0],
            "earnings_jump_day_in_60d_horizon": earnings_jump_day is not None and earnings_jump_day <= HORIZONS[1],
            "earnings_reactions_count": len(earnings_reactions),
            "options_implied_blend_active": options_implied_move is not None,
            "options_implied_move": options_implied_move,
        },
        "drift_annualized": drift_annualized,
        "vol_annualized": sigma_annualized,
        "users": users_out,
        "price_levels": price_levels_out,
        "swing_mode": swing_mode_out,
        "daily_path": daily_path_out,
    }


# ---------- core simulation ----------


def _simulate_paths(
    s0: float,
    drift_annual: float,
    sigma_annual: float,
    n_paths: int,
    n_days: int,
    rng: np.random.Generator,
    earnings_jump_day: int | None,
    earnings_reactions: list[float],
    options_implied_move: float | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Generate n_paths x n_days price matrices.

    GBM in log-space:
        log(S_{t+1}/S_t) = (mu_log - sigma_daily^2 / 2)
                          + sigma_daily * Z, Z ~ N(0, 1)
        mu_log_annual = log(1 + drift_annual)    # Itô correction so
                                                  # E[S_T/S_0] = exp(mu_log_annual*T)
                                                  # = 1 + drift_annual at T=1y
        mu_log_daily = mu_log_annual / 252
        sigma_daily  = sigma_annual / sqrt(252)

    Returns (paths, paths_gbm_only):
      paths           - the production simulation. If earnings_jump_day
                        is set, the GBM step on that day is REPLACED
                        with a bootstrap draw from earnings_reactions
                        (which are fractional returns, e.g. -0.05 = -5%).
      paths_gbm_only  - same random draws but WITHOUT the earnings jump
                        overlay applied. Used by analytic_verifier
                        (PDE cross-check) which assumes continuous
                        diffusion and can't represent discrete jumps.
                        None when no jump was active (paths_gbm_only ==
                        paths in that case; second copy would be waste).
    """
    mu_log_annual = math.log(1.0 + drift_annual) if drift_annual > -1.0 else math.log(1e-9)
    mu_log_daily = mu_log_annual / TRADING_DAYS_PER_YEAR
    sigma_daily = sigma_annual / math.sqrt(TRADING_DAYS_PER_YEAR)
    drift_term = mu_log_daily - 0.5 * sigma_daily ** 2

    # All log-returns in one shot (vectorized)
    z = rng.standard_normal(size=(n_paths, n_days))
    log_returns_base = drift_term + sigma_daily * z

    # GBM-only paths (no jump overlay) — built once, used by both the
    # production path (if no jump) and the cross-check (always).
    log_prices_base = np.cumsum(log_returns_base, axis=1)
    paths_gbm_only = s0 * np.exp(log_prices_base)

    # If jump is inactive, production paths ARE the GBM-only paths.
    if earnings_jump_day is None or not earnings_reactions:
        return paths_gbm_only, None

    # Apply earnings jump overlay on day_idx, then re-cumsum.
    day_idx = earnings_jump_day - 1
    reactions = np.asarray(earnings_reactions, dtype=float)
    log_reactions = np.log(1.0 + reactions)
    log_returns_with_jump = log_returns_base.copy()

    if options_implied_move is not None and options_implied_move > 0:
        # Blended jump: 50% from empirical bootstrap (captures direction
        # bias and skew from the ticker's actual reaction history) +
        # 50% from a Gaussian centered at empirical mean with std =
        # options-implied move (captures the market's current variance
        # forecast - typically wider than recent empirical for stocks
        # heading into uncertain reports, tighter when the market thinks
        # the print will be "in line").
        n_empirical = n_paths // 2
        n_gaussian = n_paths - n_empirical
        empirical_samples = rng.choice(log_reactions, size=n_empirical, replace=True)
        empirical_mean = float(np.mean(log_reactions))
        # Convert fractional implied move to log-return std: log(1+m) ~ m
        # for small m. Use log(1+implied) for accuracy.
        gaussian_std = math.log(1.0 + options_implied_move)
        gaussian_samples = rng.normal(empirical_mean, gaussian_std, size=n_gaussian)
        blended = np.concatenate([empirical_samples, gaussian_samples])
        rng.shuffle(blended)  # break the empirical-first ordering
        log_returns_with_jump[:, day_idx] = blended
    else:
        # Pure empirical bootstrap (no options data, or sanity check failed)
        log_returns_with_jump[:, day_idx] = rng.choice(
            log_reactions, size=n_paths, replace=True
        )

    log_prices_with_jump = np.cumsum(log_returns_with_jump, axis=1)
    paths_with_jump = s0 * np.exp(log_prices_with_jump)

    return paths_with_jump, paths_gbm_only


# ---------- per-user first-passage ----------


def _compute_user_stats(
    user_targets: dict,
    paths: np.ndarray,
    paths_gbm_only: np.ndarray | None,
    current_price: float,
    horizons: tuple[int, ...],
    sigma_annual: float,
    rng: np.random.Generator,
    earnings_jump_day: int | None,
) -> dict:
    """For one user, compute first-passage stats at each horizon.

    sigma_annual + rng are passed through to enable Brownian bridge
    intraday-crossing correction (eliminates the ~5pp discretization
    bias vs PDE). earnings_jump_day is also passed so the bridge
    correction can SKIP that day (discrete shock != continuous
    diffusion).

    When paths_gbm_only is non-None (jump overlay was active), ALSO
    compute first-passage on those parallel paths and surface as
    `p_target_gbm_only` / `p_stop_gbm_only` for the analytic verifier
    (PDE cross-check). When None, GBM-only fields mirror the
    production fields since they're identical.
    """
    target_price = user_targets["target_price"]
    stop_price = user_targets["stop_price"]
    # Bridge correction excludes the jump day. jump_day is 1-indexed
    # in the public API; convert to 0-indexed for array slicing.
    jump_idx = (earnings_jump_day - 1) if earnings_jump_day is not None else None
    out_horizons: dict[int, dict] = {}
    for h in horizons:
        # Only apply jump-day exclusion if the jump is INSIDE this horizon's slice.
        jump_idx_in_h = jump_idx if (jump_idx is not None and 0 <= jump_idx < h) else None
        stats = _first_passage_stats(
            paths[:, :h],
            current_price=current_price,
            target_price=target_price,
            stop_price=stop_price,
            sigma_annual=sigma_annual,
            rng=rng,
            jump_day_idx=jump_idx_in_h,
        )
        if paths_gbm_only is not None:
            # GBM-only path set has NO jump (used by PDE cross-check).
            # Bridge correction applies to every day, no skipping.
            gbm_stats = _first_passage_stats(
                paths_gbm_only[:, :h],
                current_price=current_price,
                target_price=target_price,
                stop_price=stop_price,
                sigma_annual=sigma_annual,
                rng=rng,
                jump_day_idx=None,
            )
            stats["p_target_gbm_only"] = gbm_stats["p_target"]
            stats["p_stop_gbm_only"] = gbm_stats["p_stop"]
            stats["ev_gbm_only_pct"] = gbm_stats["ev_pct"]
            stats["ev_gbm_only_normalized"] = gbm_stats["ev_normalized"]
        else:
            # No jump overlay -> production and GBM-only are identical.
            stats["p_target_gbm_only"] = stats["p_target"]
            stats["p_stop_gbm_only"] = stats["p_stop"]
            stats["ev_gbm_only_pct"] = stats["ev_pct"]
            stats["ev_gbm_only_normalized"] = stats["ev_normalized"]
        out_horizons[h] = stats
    return {
        "target_price": target_price,
        "stop_price": stop_price,
        "target_source": user_targets["source"],
        "horizons": out_horizons,
    }


def _first_passage_stats(
    paths_h: np.ndarray,
    current_price: float,
    target_price: float,
    stop_price: float,
    sigma_annual: float | None = None,
    rng: np.random.Generator | None = None,
    jump_day_idx: int | None = None,
) -> dict:
    """First-passage stats for one (paths, horizon) pair.

    paths_h shape: (n_paths, h)

    When the YAML flag `monte_carlo.brownian_bridge_correction` is on
    AND `sigma_annual` + `rng` are supplied, applies Brownian bridge
    correction to detect intraday barrier crossings that aren't visible
    in daily closes. Without the correction, MC systematically
    UNDERESTIMATES P(stop) by ~5pp for typical setups (the bias the
    PDE cross-check was surfacing). The correction is a per-day per-
    path bridge probability formula:

        P(crossed during [t-1, t] | both closes on same side of barrier)
          = exp(-2 * (barrier - p_{t-1}) * (barrier - p_t) / sigma_d^2)

    derived from Brownian bridge theory (Karatzas & Shreve §2.8). The
    earnings-jump day is excluded from bridge correction (the jump
    dominates continuous diffusion; bridge formula doesn't apply to
    discrete shocks).

    Returns: p_target, p_stop, p_both (both crossed in same path),
             p_neither, ev_normalized, ev_pct, mean_terminal_pct.
    """
    target_hit_mask = paths_h >= target_price  # bool (n_paths, h)
    stop_hit_mask = paths_h <= stop_price

    # Optional Brownian bridge correction.
    if _BRIDGE_CORRECTION_ENABLED and sigma_annual is not None and rng is not None:
        bridge_target_mask, bridge_stop_mask = _bridge_intraday_crossings(
            paths_h=paths_h,
            s0=current_price,
            target_price=target_price,
            stop_price=stop_price,
            sigma_annual=sigma_annual,
            rng=rng,
            jump_day_idx=jump_day_idx,
        )
        # OR the bridge crossings into the close-hit masks. A bridge
        # hit means the path crossed sometime during day t even though
        # the day-t close didn't; for first-passage day-counting that
        # day is the hit day.
        target_hit_mask = target_hit_mask | bridge_target_mask
        stop_hit_mask = stop_hit_mask | bridge_stop_mask

    target_any = target_hit_mask.any(axis=1)
    stop_any = stop_hit_mask.any(axis=1)

    # First-touch day per path; for paths that never touch, default to h
    # (any value > h works as a "this hit later" sentinel). Use a clean
    # +inf semantics via masked argmax (which returns 0 for never-true rows).
    h = paths_h.shape[1]
    target_first_day = np.where(target_any, target_hit_mask.argmax(axis=1), h)
    stop_first_day = np.where(stop_any, stop_hit_mask.argmax(axis=1), h)

    target_wins = target_any & (target_first_day < stop_first_day)
    stop_wins = stop_any & (stop_first_day < target_first_day)
    # Tie (same-day touch) is rare with continuous paths; resolve by
    # noting it as "both" for transparency.
    tie = target_any & stop_any & (target_first_day == stop_first_day)
    neither = ~(target_any | stop_any)

    p_target = float(target_wins.mean())
    p_stop = float(stop_wins.mean())
    p_both = float(tie.mean())
    p_neither = float(neither.mean())

    # Per-path realized return at first-hit (target/stop) or terminal
    target_ret = (target_price - current_price) / current_price
    stop_ret = (stop_price - current_price) / current_price
    terminal_ret = (paths_h[:, -1] - current_price) / current_price

    realized_ret = np.where(
        target_wins, target_ret,
        np.where(stop_wins, stop_ret,
                 np.where(tie, 0.5 * (target_ret + stop_ret),
                          terminal_ret))
    )
    mean_terminal_pct = float(terminal_ret.mean()) * 100.0
    ev_pct = float(realized_ret.mean()) * 100.0
    # EV normalized in units of stop-distance (risk units)
    risk_distance = current_price - stop_price
    ev_normalized = float(realized_ret.mean() * current_price / risk_distance) if risk_distance > 0 else 0.0

    return {
        "p_target": p_target,
        "p_stop": p_stop,
        "p_both": p_both,
        "p_neither": p_neither,
        "ev_normalized": ev_normalized,
        "ev_pct": ev_pct,
        "mean_terminal_pct": mean_terminal_pct,
    }


# ---------- Brownian bridge intraday-crossing correction ----------


def _bridge_intraday_crossings(
    paths_h: np.ndarray,
    s0: float,
    target_price: float,
    stop_price: float,
    sigma_annual: float,
    rng: np.random.Generator,
    jump_day_idx: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """For each (path, day), determine whether the path crossed a
    barrier INTRADAY using the Brownian bridge formula.

    Inputs:
      paths_h: (n_paths, h) prices over the horizon.
      s0: starting price (used for the day-0 -> day-1 transition).
      target_price, stop_price: the barriers.
      sigma_annual: the same volatility MC used to generate the paths.
      rng: random generator (consumed for the bridge draws).
      jump_day_idx: 0-indexed day where earnings jump applied (excluded
        from bridge — the jump is a discrete shock, not continuous
        diffusion, and the bridge formula assumes the latter).

    Returns (target_bridge_mask, stop_bridge_mask) shape (n_paths, h)
    where True = "bridge says the path crossed during this day."

    Math: P(max over [t-1, t] >= B | both closes < B) =
            exp(-2 * (B - p_{t-1}) * (B - p_t) / sigma_d^2)
    in log-space, where sigma_d is daily log-vol. Derivation: Brownian
    bridge between two endpoints on the same side of a barrier has a
    known crossing-probability formula (Karatzas & Shreve, "Brownian
    Motion and Stochastic Calculus", §2.8).

    We sample a uniform per (path, day) and mark a bridge crossing when
    u < P_bridge. This converts the systematic underestimate of P(stop)
    that the close-only logic produced (~5pp documented bias vs PDE)
    into an unbiased estimate.
    """
    n_paths, h = paths_h.shape
    sigma_daily = sigma_annual / math.sqrt(TRADING_DAYS_PER_YEAR)
    sigma_sq_d = sigma_daily * sigma_daily
    log_target = math.log(target_price)
    log_stop = math.log(stop_price)
    log_s0 = math.log(s0)

    log_paths = np.log(paths_h)
    # Build "previous close" array - day-0 "previous" is log(s0); for
    # day-t (t>=1) the previous is day-(t-1)'s close.
    log_prev = np.concatenate(
        [np.full((n_paths, 1), log_s0, dtype=float), log_paths[:, :-1]],
        axis=1,
    )
    log_cur = log_paths

    # Target bridge - both prev and current must be BELOW target for
    # the formula to apply. Otherwise the close already crossed (handled
    # by the close-hit mask in the caller) or the prev was on the wrong
    # side (we don't apply bridge backward through a close-cross).
    target_both_below = (log_prev < log_target) & (log_cur < log_target)
    bridge_p_target = np.where(
        target_both_below,
        np.exp(-2.0 * (log_target - log_prev) * (log_target - log_cur) / sigma_sq_d),
        0.0,
    )
    np.clip(bridge_p_target, 0.0, 1.0, out=bridge_p_target)

    # Stop bridge - both prev and current must be ABOVE stop.
    stop_both_above = (log_prev > log_stop) & (log_cur > log_stop)
    bridge_p_stop = np.where(
        stop_both_above,
        np.exp(-2.0 * (log_prev - log_stop) * (log_cur - log_stop) / sigma_sq_d),
        0.0,
    )
    np.clip(bridge_p_stop, 0.0, 1.0, out=bridge_p_stop)

    # Exclude jump day from bridge - the jump is a discrete shock,
    # bridge assumes continuous diffusion.
    if jump_day_idx is not None and 0 <= jump_day_idx < h:
        bridge_p_target[:, jump_day_idx] = 0.0
        bridge_p_stop[:, jump_day_idx] = 0.0

    # Resolve bridges: u < P -> bridge crossing happened.
    u_target = rng.uniform(size=(n_paths, h))
    u_stop = rng.uniform(size=(n_paths, h))
    bridge_target_mask = u_target < bridge_p_target
    bridge_stop_mask = u_stop < bridge_p_stop

    return bridge_target_mask, bridge_stop_mask


# ---------- price-level zones from MC paths ----------


def _compute_zones_for_horizon(paths_h: np.ndarray, forecast_dates: list[str]) -> dict:
    """Compute dip-entry and rally-sell zones for one horizon."""
    path_mins = paths_h.min(axis=1)
    path_maxes = paths_h.max(axis=1)

    # dip_price: 70th percentile of mins -> 70% of paths touched at or below this
    dip_price = float(np.percentile(path_mins, _DIP_PCTILE * 100))
    # rally_price: (100 - 70)th percentile of maxes -> 70% of paths touched at or above this
    rally_price = float(np.percentile(path_maxes, (1.0 - _RALLY_PCTILE) * 100))

    # Median day of dip/rally touch across paths
    dip_day_per_path = paths_h.argmin(axis=1)
    rally_day_per_path = paths_h.argmax(axis=1)
    dip_day = int(np.median(dip_day_per_path))
    rally_day = int(np.median(rally_day_per_path))

    n = paths_h.shape[1]
    dip_window = (max(0, dip_day - _DATE_HALF_WIDTH),
                  min(n - 1, dip_day + _DATE_HALF_WIDTH))
    rally_window = (max(0, rally_day - _DATE_HALF_WIDTH),
                    min(n - 1, rally_day + _DATE_HALF_WIDTH))

    return {
        "dip": {
            "price": dip_price,
            "date_range": _format_range(forecast_dates, dip_window),
            "median_day": dip_day,
            "window_days": list(dip_window),
        },
        "rally": {
            "price": rally_price,
            "date_range": _format_range(forecast_dates, rally_window),
            "median_day": rally_day,
            "window_days": list(rally_window),
        },
    }


def _format_range(dates: list[str], window: tuple[int, int]) -> str:
    a, b = window
    if not dates or a >= len(dates) or b >= len(dates):
        return "?"
    return f"{dates[a]} -> {dates[b]}"


# ---------- daily path summary ----------


def _compute_daily_path(
    paths: np.ndarray,
    forecast_dates: list[str],
    days_to_show: int,
    zone_horizons: dict[int, dict],
    earnings_jump_day: int | None = None,
) -> dict:
    """Median daily price with zone tinting per day.

    Zone precedence (highest first):
      - "earnings" — the single day where the empirical earnings-
        reaction bootstrap was applied. Most informative annotation
        because it explains why the median path "jumps" on that day.
      - "rally" — within the rally window for the primary horizon.
      - "dip" — within the dip window for the primary horizon.

    Zone windows come from the 30-day horizon (primary).
    """
    median_prices = np.median(paths, axis=0)

    primary_h = config.HORIZONS[0]
    z = zone_horizons.get(primary_h, {})
    dip_window = (z.get("dip", {}).get("window_days") or [-1, -1])
    rally_window = (z.get("rally", {}).get("window_days") or [-1, -1])
    # earnings_jump_day is 1-indexed by external convention; convert to
    # 0-indexed for path-array alignment.
    earnings_idx = (earnings_jump_day - 1) if earnings_jump_day is not None else None

    days = []
    for i in range(min(days_to_show, len(median_prices))):
        zone = ""
        if dip_window[0] <= i <= dip_window[1]:
            zone = "dip"
        if rally_window[0] <= i <= rally_window[1]:
            zone = "rally"  # rally over dip if overlap (unusual)
        if earnings_idx is not None and i == earnings_idx:
            zone = "earnings"  # earnings is the most informative — wins
        days.append({
            "day": i + 1,
            "date": forecast_dates[i] if i < len(forecast_dates) else "?",
            "median_price": float(median_prices[i]),
            "zone": zone,
        })

    return {
        "status": "ok",
        "days": days,
    }


# ---------- supporting utils ----------


def _build_forecast_dates(n_days: int) -> list[str]:
    """Return the next n_days NYSE trading dates (ISO format) starting
    from tomorrow."""
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=int(n_days * 1.6) + 10)  # buffer for weekends/holidays
    sched = NYSE_CAL.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    out = [d.strftime("%Y-%m-%d") for d in sched.index[:n_days]]
    # If holidays trim us short (rare for our window sizes), pad with naive +1 days
    while len(out) < n_days:
        out.append((date.today() + timedelta(days=len(out) + 1)).isoformat())
    return out


def _seed_for(ticker: str, run_date: str) -> int:
    """Stable 32-bit seed from (ticker, run_date). Same-day re-runs
    reproduce; different days/tickers draw independent streams."""
    h = hashlib.sha256(f"{ticker}|{run_date}".encode()).digest()
    return int.from_bytes(h[:4], byteorder="big")


def _compute_rsi_14(prices: list[dict]) -> float | None:
    """Standard 14-period Wilder's RSI on closes. Returns the most
    recent value rounded to 1 decimal. Returns None if insufficient
    history."""
    if len(prices) < 15:
        return None
    closes = np.array(
        [p.get("adj_close") or p.get("close") for p in prices[-200:]],
        dtype=float,
    )
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder smoothing (alpha = 1/period)
    period = 14
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    return round(float(rsi), 1)


def _compute_high_60d(prices: list[dict]) -> float | None:
    """Highest close over the last 60 sessions."""
    if len(prices) < 60:
        return None
    closes = [p.get("adj_close") or p.get("close") for p in prices[-60:]]
    closes = [c for c in closes if c is not None]
    if not closes:
        return None
    return round(float(max(closes)), 2)


def _fail(reason: str) -> dict:
    return {
        "status": "fail",
        "reason": reason,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- CLI smoke test ----------


def _print_summary(ticker: str, payload: dict) -> None:
    print(f"\n=== {ticker} ===")
    if payload.get("status") != "ok":
        print(f"  status: {payload.get('status')} - {payload.get('reason', '')}")
        return
    mc = payload["model_components"]
    print(f"  n_paths: {payload['n_paths']:,}  drift={payload['drift_annualized']*100:+.1f}%/yr  "
          f"vol={payload['vol_annualized']*100:.1f}%/yr")
    if mc["earnings_jump_overlay"]:
        print(f"  earnings jump overlay ON: day {mc['earnings_jump_day']} of horizon, "
              f"{mc['earnings_reactions_count']} historical reactions for bootstrap")
    else:
        print(f"  earnings jump overlay: not active (no in-horizon event or no reactions)")

    print()
    for user, u in payload["users"].items():
        print(f"  --- {user} (source={u['target_source']}) ---")
        print(f"      target ${u['target_price']:.2f}  stop ${u['stop_price']:.2f}")
        for h in config.HORIZONS:
            s = u["horizons"][h]
            print(f"      {h}d: P(target)={s['p_target']*100:5.1f}%  P(stop)={s['p_stop']*100:5.1f}%  "
                  f"P(neither)={s['p_neither']*100:5.1f}%  EV={s['ev_pct']:+5.2f}%  "
                  f"EVnorm={s['ev_normalized']:+.3f}")

    print()
    print(f"  --- price levels ---")
    pl = payload["price_levels"]
    print(f"  current=${pl['current_price']:.2f}  RSI={pl['rsi']}  60d-high=${pl['high_60d']}")
    for h in config.HORIZONS:
        z = pl["horizons"][h]
        print(f"  {h}d  dip:   ${z['dip']['price']:.2f}  range {z['dip']['date_range']}")
        print(f"  {h}d  rally: ${z['rally']['price']:.2f}  range {z['rally']['date_range']}")


def main() -> int:
    from src.pipeline import data as data_step, regime as regime_step, volatility as vol_step
    from src.pipeline import catalyst as cat_step, targets as targets_step
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Monte Carlo smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to simulate")
    args = parser.parse_args()

    watchlist = {}
    if config.WATCHLIST_PATH.exists():
        with config.WATCHLIST_PATH.open() as f:
            watchlist = yaml.safe_load(f) or {}

    exit_code = 0
    for ticker in args.tickers:
        ticker = ticker.upper()
        entry = watchlist.get(ticker)
        if entry is None:
            print(f"\nERROR: {ticker} not in watchlist", file=sys.stderr)
            exit_code = 1
            continue
        try:
            price_data = data_step.fetch(ticker)
            tier = entry.get("tier")
            regime = regime_step.detect(ticker, price_data, tier=tier)
            vol = vol_step.forecast(ticker, price_data, tier=tier)
            cat = cat_step.detect(ticker, price_data, tier=tier)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR upstream-prep {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue

        # Build minimal snap-like dict
        snap = {
            "data": {"profile": price_data.get("profile") or {}},
            "_price_data": price_data,
            "regime": regime,
            "volatility": vol,
            "catalyst": cat,
        }
        snap["targets"] = targets_step.derive(ticker, entry, snap)
        try:
            payload = simulate(ticker, snap)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR simulating {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
