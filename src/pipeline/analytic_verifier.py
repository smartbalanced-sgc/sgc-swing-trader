"""Step 7 - Analytic verifier: PDE-based cross-check of Monte Carlo.

Solves the Kolmogorov backward PDE for the same drift+vol parameters
Monte Carlo used, computes P(target), P(stop), and EV by completely
different math (deterministic finite-difference, not random
simulation), and reports the per-(user, horizon) delta. The existing
Layer-2 confidence haircut (configured in YAML under
conviction.haircuts.mc_pde_disagreement) fires when MC and PDE
disagree by >5pp on P(target).

Why this matters:

  Independent validation. If MC has a subtle bug (off-by-one in
  earnings-day index, wrong Itô correction, biased random draws),
  the PDE solver will produce a different number and the
  disagreement becomes visible. If both methods agree, the answer is
  trustworthy regardless of any single method's blind spots.

  This is NOT a check on the empirical earnings-jump overlay - the
  PDE assumes continuous diffusion and can't represent a discrete
  jump. We compare against MC's `p_target_gbm_only` / `p_stop_gbm_only`
  / `ev_gbm_only` (parallel paths run without the overlay), so the
  cross-check validates the underlying continuous-time math.

The PDE (Kolmogorov backward equation with τ = horizon - t):

  For each user's (target_price, stop_price) pair, solve

    ∂u/∂τ = (μ - σ²/2) · ∂u/∂x + (σ²/2) · ∂²u/∂x²

  on x ∈ [log(stop), log(target)] with three quantities of interest
  (each is its own PDE solve, same operator, different boundary +
  initial conditions):

  1. P(target first):
     u(log T, τ) = 1
     u(log S, τ) = 0
     u(x, 0) = 0  for x ∈ (log S, log T)

  2. P(stop first):
     u(log T, τ) = 0
     u(log S, τ) = 1
     u(x, 0) = 0

  3. E[realized return] (Feynman-Kac):
     V(log T, τ) = (T - S_0) / S_0
     V(log S, τ) = (S - S_0) / S_0
     V(x, 0) = (exp(x) - S_0) / S_0   # terminal: didn't hit either

  Where μ = log(1 + drift_annual), σ = sigma_annual, x = log price.
  All time units are calendar years; τ_horizon = horizon_days / 252.

Numerical scheme: implicit Euler with Thomas tridiagonal solver
(no scipy dependency, unconditionally stable). Mesh: 200 space
points × (4 × horizon_days) time steps. PDE discretization error
itself is <0.5pp on P(target).

Known systematic bias: MC vs PDE typically disagree by 3-6pp on
P(target) and P(stop) - PDE > MC. This is a MODELING difference,
not a bug:

  - MC checks first-passage on daily closes only (missing intraday
    excursions that recovered by close).
  - PDE is continuous in time and counts every barrier crossing.

So PDE is the TRUE continuous-time first-passage probability; MC
underestimates because of the daily-monitoring approximation. The
existing 5pp agreement tolerance was calibrated for the closed-form
"agree perfectly" case - for our setup it'll be exceeded for tight-
band / high-vol tickers. A future enhancement would be to apply a
Brownian bridge correction to MC's first-passage check (the standard
industry technique to recover continuous-time probabilities from
daily closes). For v1 we accept the bias and surface it honestly in
the cross-check panel.

Output schema (matches dashboard's _render_cross_check expectations
PLUS additional per-(user, horizon) detail for verdict layer-2):

    {
        "status": "ok",
        "fetched_at": ISO timestamp,
        "horizons": {
            30: {
                # Headline fields - taken from first user's solve
                # (shared-watching common case)
                "mc_p_target": float, "pde_p_target": float,
                "delta_p_target": float, "agree_p_target": bool,
                "mc_p_stop": float, "pde_p_stop": float,
                "delta_p_stop": float, "agree_p_stop": bool,
                "mc_ev": float, "pde_ev": float,
                "delta_ev": float, "agree_ev": bool,
                # Per-user detail
                "users": {
                    "aidy": {
                        "mc_p_target_gbm_only": ..., "pde_p_target": ...,
                        "delta_p_target_pp": ..., "agree_p_target": ...,
                        (same for p_stop and ev)
                    },
                },
            },
            60: {...},
        },
        "max_p_target_delta_pp": float,   # headline for verdict
    }

CLI smoke test:

    python -m src.pipeline.analytic_verifier NVDA
    python -m src.pipeline.analytic_verifier NVDA AMAT IONQ
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import traceback
from datetime import datetime, timezone

import numpy as np

from src import config

logger = logging.getLogger(__name__)

# Numerical config — hardcoded for v1. Could move to YAML later.
N_SPACE = 200            # log-price grid points (200 -> ~0.001 log-units for typical spreads)
TIME_STEPS_PER_DAY = 4   # 4 steps/day for 30d horizon = 120 steps

TRADING_DAYS_PER_YEAR = 252

# Cross-check tolerances (from existing YAML config — the conviction
# Layer-2 haircut config). We read these here to compute the
# `agree_*` boolean fields the dashboard renders.
_CC = config.THRESHOLDS.cross_check
TOL_P_TARGET_PP = _CC.p_target_agreement_tolerance_pp     # 5.0pp default
TOL_EV_REL = _CC.ev_agreement_tolerance_pct               # 0.05 default

HORIZONS = config.HORIZONS


# ---------- public API ----------


def verify(ticker: str, snap: dict) -> dict:
    """Run PDE cross-check on Monte Carlo's GBM-only first-passage stats.

    Args:
        ticker: ticker symbol (informational).
        snap: in-progress snapshot. Required upstream blocks:
            - monte_carlo (with users.{user}.horizons.{h}.p_target_gbm_only,
              p_stop_gbm_only, ev_pct, ev_normalized)
            - targets (with users.{user}.{target_price, stop_price},
              current_price)
            - regime (annualized_drift_implied)
            - volatility (forecast_30d_pct)
    """
    del ticker  # informational only

    mc_block = snap.get("monte_carlo") or {}
    targets_block = snap.get("targets") or {}
    regime_block = snap.get("regime") or {}
    vol_block = snap.get("volatility") or {}

    if mc_block.get("status") != "ok":
        return _fail("monte_carlo not ok")
    if targets_block.get("status") != "ok":
        return _fail("targets not ok")
    if regime_block.get("status") != "ok":
        return _fail("regime not ok")
    if vol_block.get("status") != "ok":
        return _fail("volatility not ok")

    current_price = float(targets_block["current_price"])
    drift_annual = float(regime_block["annualized_drift_implied"])
    sigma_annual = float(vol_block["forecast_30d_pct"])

    # Convert arithmetic drift to log-drift (Itô). MC uses the same
    # convention; we mirror it for the cross-check to be apples-to-apples.
    if drift_annual <= -1.0:
        return _fail(f"drift_annual {drift_annual} not allowed (would require log of non-positive)")
    mu_log_annual = math.log(1.0 + drift_annual)
    # Convection coefficient (μ - σ²/2) and diffusion coefficient (σ²/2).
    # Both in per-year units; PDE solver scales τ in years.
    convection = mu_log_annual - 0.5 * sigma_annual ** 2
    diffusion = 0.5 * sigma_annual ** 2

    horizons_out: dict[int, dict] = {}
    max_p_target_delta_pp = 0.0

    for h in HORIZONS:
        per_user: dict[str, dict] = {}
        for user, user_targets in targets_block["users"].items():
            target_price = float(user_targets["target_price"])
            stop_price = float(user_targets["stop_price"])
            if not (stop_price < current_price < target_price):
                logger.warning(
                    f"{user} {h}d: current price {current_price} not strictly between "
                    f"stop {stop_price} and target {target_price}; skipping PDE"
                )
                continue

            mc_horizon = ((mc_block.get("users") or {}).get(user) or {}).get("horizons", {}).get(h)
            if mc_horizon is None:
                continue

            # Three PDE solves (same operator, different BC/IC)
            pde = _solve_all_three(
                s0=current_price,
                target_price=target_price,
                stop_price=stop_price,
                convection=convection,
                diffusion=diffusion,
                horizon_days=h,
            )

            mc_pt = float(mc_horizon.get("p_target_gbm_only", mc_horizon["p_target"]))
            mc_ps = float(mc_horizon.get("p_stop_gbm_only", mc_horizon["p_stop"]))
            # Use GBM-only EV when present (jump active); otherwise the
            # production EV (no jump in horizon -> they're identical).
            mc_ev_pct = float(
                mc_horizon.get("ev_gbm_only_pct")
                if mc_horizon.get("ev_gbm_only_pct") is not None
                else mc_horizon.get("ev_pct", 0.0)
            )
            mc_ev_fraction = mc_ev_pct / 100.0  # convert percent -> fraction for comparison

            delta_pt = mc_pt - pde["p_target"]
            delta_ps = mc_ps - pde["p_stop"]
            delta_ev = mc_ev_fraction - pde["ev_fraction"]

            agree_pt = abs(delta_pt) * 100 <= TOL_P_TARGET_PP
            agree_ps = abs(delta_ps) * 100 <= TOL_P_TARGET_PP
            # EV agreement: pass if EITHER the relative tolerance OR
            # the absolute-0.5pp floor is met. Small-EV setups (NVDA-
            # like 1% EV) would otherwise need <0.05pp absolute to pass
            # 5% relative - too tight for any realistic methodology
            # difference. 0.5pp absolute floor preserves the relative
            # check for larger EVs while not penalizing precision-of-
            # zero on near-zero EVs.
            abs_ev_delta_pp = abs(delta_ev) * 100
            rel_ev_ok = (
                abs(mc_ev_fraction) >= 0.005
                and abs(delta_ev) / abs(mc_ev_fraction) <= TOL_EV_REL
            )
            abs_ev_ok = abs_ev_delta_pp <= 0.5
            agree_ev = rel_ev_ok or abs_ev_ok

            per_user[user] = {
                "mc_p_target_gbm_only": mc_pt,
                "pde_p_target": pde["p_target"],
                "delta_p_target_pp": abs(delta_pt) * 100,
                "agree_p_target": agree_pt,
                "mc_p_stop_gbm_only": mc_ps,
                "pde_p_stop": pde["p_stop"],
                "delta_p_stop_pp": abs(delta_ps) * 100,
                "agree_p_stop": agree_ps,
                "mc_ev_gbm_only_pct": mc_ev_pct,
                "pde_ev_pct": pde["ev_fraction"] * 100,
                "delta_ev_pct": abs(delta_ev) * 100,
                "agree_ev": agree_ev,
            }
            max_p_target_delta_pp = max(max_p_target_delta_pp, abs(delta_pt) * 100)

        # Headline fields: take the first user's values (typical case is
        # shared watchlist - everyone has same targets).
        if per_user:
            first_user_data = next(iter(per_user.values()))
            headline = {
                "mc_p_target": first_user_data["mc_p_target_gbm_only"],
                "pde_p_target": first_user_data["pde_p_target"],
                "delta_p_target": first_user_data["delta_p_target_pp"] / 100.0,
                "agree_p_target": first_user_data["agree_p_target"],
                "mc_p_stop": first_user_data["mc_p_stop_gbm_only"],
                "pde_p_stop": first_user_data["pde_p_stop"],
                "delta_p_stop": first_user_data["delta_p_stop_pp"] / 100.0,
                "agree_p_stop": first_user_data["agree_p_stop"],
                "mc_ev": first_user_data["mc_ev_gbm_only_pct"] / 100.0,
                "pde_ev": first_user_data["pde_ev_pct"] / 100.0,
                "delta_ev": first_user_data["delta_ev_pct"] / 100.0,
                "agree_ev": first_user_data["agree_ev"],
                "users": per_user,
            }
            horizons_out[h] = headline
        else:
            horizons_out[h] = {"users": {}, "skipped_reason": "no valid per-user PDE results"}

    return {
        "status": "ok",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizons": horizons_out,
        "max_p_target_delta_pp": max_p_target_delta_pp,
    }


# ---------- PDE solver ----------


def _solve_all_three(
    s0: float,
    target_price: float,
    stop_price: float,
    convection: float,
    diffusion: float,
    horizon_days: int,
) -> dict:
    """Solve the three Feynman-Kac PDEs for one (target, stop) pair.

    Returns {"p_target": float, "p_stop": float, "ev_fraction": float}
    where ev_fraction is the expected realized return (e.g. +0.015 = +1.5%).
    """
    # Build space grid in log-price space.
    x_low = math.log(stop_price)
    x_high = math.log(target_price)
    x_grid = np.linspace(x_low, x_high, N_SPACE + 1)
    dx = x_grid[1] - x_grid[0]

    # Build time grid in years (forward in τ from 0 to horizon).
    tau_horizon = horizon_days / TRADING_DAYS_PER_YEAR
    n_time = max(int(TIME_STEPS_PER_DAY * horizon_days), 16)
    dtau = tau_horizon / n_time

    # Tridiagonal system coefficients (implicit Euler).
    # u_i^n = (p - q) · u_{i-1}^{n+1} + (1 + 2q) · u_i^{n+1} + (-p - q) · u_{i+1}^{n+1}
    # where p = a·dτ / (2·dx), q = b·dτ / dx².
    p_coef = convection * dtau / (2.0 * dx)
    q_coef = diffusion * dtau / (dx * dx)
    sub = p_coef - q_coef        # coefficient on u_{i-1}^{n+1}
    diag = 1.0 + 2.0 * q_coef    # coefficient on u_i^{n+1}
    sup = -p_coef - q_coef       # coefficient on u_{i+1}^{n+1}

    # Interior grid: i = 1..N-1 (N+1 total points; indices 0 and N are
    # boundaries enforced by BC).
    n_interior = N_SPACE - 1
    a = np.full(n_interior - 1, sub, dtype=float)   # sub-diagonal (length n-1)
    b_diag = np.full(n_interior, diag, dtype=float)  # main diagonal
    c = np.full(n_interior - 1, sup, dtype=float)   # super-diagonal

    # Initial condition (terminal of original PDE; τ = 0) and BCs depend on
    # which quantity we're solving for. Run three separate solves.

    # The x index of the "current price" — closest grid point.
    x_current = math.log(s0)
    idx_current = int(round((x_current - x_low) / dx))
    idx_current = max(1, min(n_interior, idx_current))

    # 1. P(target first):
    #    BC: u(x_0) = 0, u(x_N) = 1
    #    IC: u(x, 0) = 0 for interior x
    u_target = np.zeros(n_interior, dtype=float)
    bc_low_target = 0.0
    bc_high_target = 1.0
    for _ in range(n_time):
        rhs = u_target.copy()
        # Boundary contributions to the RHS
        rhs[0] -= sub * bc_low_target       # subtracting (sub * u_0^{n+1})
        rhs[-1] -= sup * bc_high_target     # subtracting (sup * u_N^{n+1})
        u_target = _thomas_solve(a, b_diag, c, rhs)
    p_target = float(u_target[idx_current - 1])  # interior index = i-1
    p_target = max(0.0, min(1.0, p_target))

    # 2. P(stop first):
    #    BC: u(x_0) = 1, u(x_N) = 0
    #    IC: u(x, 0) = 0
    u_stop = np.zeros(n_interior, dtype=float)
    bc_low_stop = 1.0
    bc_high_stop = 0.0
    for _ in range(n_time):
        rhs = u_stop.copy()
        rhs[0] -= sub * bc_low_stop
        rhs[-1] -= sup * bc_high_stop
        u_stop = _thomas_solve(a, b_diag, c, rhs)
    p_stop = float(u_stop[idx_current - 1])
    p_stop = max(0.0, min(1.0, p_stop))

    # 3. E[realized_return] (Feynman-Kac):
    #    BC: V(x_0) = (stop - s0) / s0
    #         V(x_N) = (target - s0) / s0
    #    IC: V(x, 0) = (exp(x) - s0) / s0 for interior x
    target_return = (target_price - s0) / s0
    stop_return = (stop_price - s0) / s0
    interior_x = x_grid[1:-1]
    v = (np.exp(interior_x) - s0) / s0  # terminal value
    bc_low_v = stop_return
    bc_high_v = target_return
    for _ in range(n_time):
        rhs = v.copy()
        rhs[0] -= sub * bc_low_v
        rhs[-1] -= sup * bc_high_v
        v = _thomas_solve(a, b_diag, c, rhs)
    ev_fraction = float(v[idx_current - 1])

    return {
        "p_target": p_target,
        "p_stop": p_stop,
        "ev_fraction": ev_fraction,
    }


def _thomas_solve(a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Thomas algorithm — O(n) tridiagonal solve. No scipy dependency.

    Solves A·x = d where A has:
      sub-diagonal `a` of length n-1 (A[i, i-1] = a[i-1] for i=1..n-1)
      main diagonal `b` of length n
      super-diagonal `c` of length n-1 (A[i, i+1] = c[i] for i=0..n-2)
    """
    n = len(d)
    c_prime = np.zeros(n - 1, dtype=float)
    d_prime = np.zeros(n, dtype=float)
    c_prime[0] = c[0] / b[0]
    d_prime[0] = d[0] / b[0]
    for i in range(1, n):
        m = b[i] - a[i - 1] * c_prime[i - 1] if i > 0 else b[i]
        if i < n - 1:
            c_prime[i] = c[i] / m
        d_prime[i] = (d[i] - a[i - 1] * d_prime[i - 1]) / m
    x = np.zeros(n, dtype=float)
    x[-1] = d_prime[-1]
    for i in range(n - 2, -1, -1):
        x[i] = d_prime[i] - c_prime[i] * x[i + 1]
    return x


# ---------- helpers ----------


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
        print(f"  status: {payload.get('status')} - {payload.get('reason')}")
        return

    print(f"  max P(target) delta: {payload['max_p_target_delta_pp']:.2f}pp")
    for h in config.HORIZONS:
        d = (payload.get("horizons") or {}).get(h)
        if not d:
            continue
        print(f"  --- {h}d horizon ---")
        print(
            f"    P(target)  MC={d['mc_p_target']*100:5.1f}%  PDE={d['pde_p_target']*100:5.1f}%  "
            f"Δ={d['delta_p_target']*100:4.2f}pp  agree={d['agree_p_target']}"
        )
        print(
            f"    P(stop)    MC={d['mc_p_stop']*100:5.1f}%  PDE={d['pde_p_stop']*100:5.1f}%  "
            f"Δ={d['delta_p_stop']*100:4.2f}pp  agree={d['agree_p_stop']}"
        )
        print(
            f"    EV         MC={d['mc_ev']*100:+5.2f}%  PDE={d['pde_ev']*100:+5.2f}%  "
            f"Δ={d['delta_ev']*100:4.2f}pp  agree={d['agree_ev']}"
        )


def main() -> int:
    from src.pipeline import (
        data as data_step,
        regime as regime_step,
        volatility as vol_step,
        catalyst as cat_step,
        targets as targets_step,
        monte_carlo as mc_step,
    )
    from src import tier_classifier
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Analytic verifier (PDE cross-check) smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to verify")
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
            tier_classified = tier_classifier.classify(price_data)
            tier_classified["status"] = "ok"
            regime = regime_step.detect(ticker, price_data, tier=tier)
            cat = cat_step.detect(ticker, price_data, tier=tier)
            vol = vol_step.forecast(ticker, price_data, tier=tier)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR upstream-prep {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue

        snap = {
            "ticker": ticker,
            "tier_anchor": tier,
            "data": {"profile": price_data.get("profile") or {}},
            "_price_data": price_data,
            "tier_classifier": tier_classified,
            "regime": regime,
            "catalyst": cat,
            "volatility": vol,
        }
        snap["targets"] = targets_step.derive(ticker, entry, snap)
        snap["monte_carlo"] = mc_step.simulate(ticker, snap, run_date=None)

        try:
            payload = verify(ticker, snap)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR verifying {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
