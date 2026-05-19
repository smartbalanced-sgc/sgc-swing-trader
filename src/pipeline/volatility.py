"""Step 4 — Volatility forecast.

Forecasts forward 30-day and 60-day annualized volatility for a ticker
using a GARCH(1,1) model with literature-default persistence parameters
and a data-derived long-run variance.

Why GARCH(1,1) over HAR-RV (the other v1-candidate model):

  HAR-RV's signature advantage is decomposing realized volatility into
  daily, weekly, and monthly components computed from intraday data.
  We only have daily bars (FMP Starter doesn't include intraday on
  this plan), so HAR-RV loses its discriminating power — its three
  components collapse toward each other and the model behaves like a
  glorified moving average.

  GARCH(1,1) is built for daily-bar data, captures volatility
  clustering (the empirical fact that calm days follow calm days and
  wild days follow wild days), and produces a MEAN-REVERTING multi-
  step forecast — exactly what the dashboard's "vol falling / rising /
  flat" narrative needs. Today's elevated vol predicts tomorrow's
  elevated vol with persistence parameter alpha+beta, but over the
  forecast horizon the variance converges toward the long-run level.

Parameter sourcing — why we don't fit by MLE:

  The textbook approach fits all three GARCH parameters (omega, alpha,
  beta) by maximum-likelihood, which requires scipy.optimize. We don't
  carry scipy as a dependency (would re-open the numpy 2.0 ABI issue
  we just fought through), so MLE is off the table for v1.

  Workable alternative: use literature-standard alpha + beta (the
  defaults used at JPM/RiskMetrics-adjacent desks before any tuning:
  alpha=0.05, beta=0.90) and derive omega from the sample variance
  over a 1-year window. This makes the model DYNAMICS standard but
  the LEVELS fully data-driven per ticker. Result is
  indistinguishable from MLE-fit GARCH for typical equities at the
  resolution we need.

Mean-reverting forecast math:

  Let sigma_squared_LR be the long-run variance (sample variance of
  daily log-returns over the long_run_window).
  Let omega = sigma_squared_LR * (1 - alpha - beta) — the "reversion
  pull" toward sigma_squared_LR.

  Recursion:
      sigma_squared_(t+1) = omega + alpha * r_squared_t + beta * sigma_squared_t

  Multi-step (assuming E[r_squared_(t+h)] = sigma_squared_(t+h)):
      sigma_squared_(t+h) = sigma_squared_LR
                          + (alpha + beta)^(h-1)
                              * (sigma_squared_(t+1) - sigma_squared_LR)

  Horizon-averaged annualized vol:
      vol_H = sqrt( mean(sigma_squared_(t+1) ... sigma_squared_(t+H)) * 252 )

Output schema (matches dashboard `_render_volatility`):

    {
        "status": "ok",
        "current_realized_pct": float,    # annualized, 60d trailing window
        "forecast_30d_pct": float,        # annualized, horizon-averaged
        "forecast_60d_pct": float,
        "long_run_vol_pct": float,        # annualized, 1-year window
        "confidence_band": "tight"|"moderate"|"wide",   # by anchor tier
        "model": "GARCH(1,1)",
        "model_params": {
            "alpha": float, "beta": float,
            "omega": float, "persistence": float,
            "half_life_days": float,
        },
        "narrative": str,
        "fetched_at": ISO timestamp,
    }

CLI smoke test:

    python -m src.pipeline.volatility NVDA
    python -m src.pipeline.volatility NVDA AMAT IONQ
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

# Threshold convenience aliases
_V = config.THRESHOLDS.volatility
_CURRENT_WINDOW = _V.current_window_days
_LONG_RUN_WINDOW = _V.long_run_window_days
_ALPHA = _V.garch_alpha
_BETA = _V.garch_beta
_PERSISTENCE = _ALPHA + _BETA  # alpha + beta — must be < 1 for stationarity
_BAND_BY_TIER: dict[str, str] = {
    tier: getattr(_V.confidence_band_by_tier, tier)
    for tier in ("A", "B", "C")
}

TRADING_DAYS_PER_YEAR = 252

# Forecast horizons match the engine-wide horizons (30d primary, 60d secondary)
_HORIZON_30 = config.THRESHOLDS.horizons.primary_days
_HORIZON_60 = config.THRESHOLDS.horizons.secondary_days


# ---------- public API ----------


def forecast(ticker: str, price_data: dict, tier: str | None = None) -> dict:
    """Forecast forward volatility for one ticker.

    Args:
        ticker: ticker symbol.
        price_data: output of src.pipeline.data.fetch(ticker). Must
            include a 'prices' list with chronologically-ordered daily
            bars (each having a 'close' field).
        tier: anchor tier from watchlist ("A" | "B" | "C"). Used to
            set the confidence_band label only — does NOT modify the
            point forecasts (those are data-driven via GARCH).

    Returns the payload described in the module docstring.

    Raises:
        ValueError: if there isn't enough price history for the model.
            Need at least long_run_window_days + 1 bars to estimate
            the long-run variance plus the current sigma_squared.
    """
    prices = price_data.get("prices") or []
    closes = _extract_closes(prices)

    min_bars = _LONG_RUN_WINDOW + 1
    if len(closes) < min_bars:
        raise ValueError(
            f"need at least {min_bars} price bars to fit GARCH (got {len(closes)}); "
            f"ticker may be too new for a 1-year long-run estimate"
        )

    log_returns = np.diff(np.log(closes))

    # 1. Estimate long-run variance from the most recent 1-year window.
    lr_returns = log_returns[-_LONG_RUN_WINDOW:]
    sigma_squared_lr = float(np.var(lr_returns, ddof=1))
    long_run_vol_annualized = math.sqrt(sigma_squared_lr * TRADING_DAYS_PER_YEAR)

    # 2. Compute current realized vol over the 60d trailing window.
    current_returns = log_returns[-_CURRENT_WINDOW:]
    sigma_squared_current = float(np.var(current_returns, ddof=1))
    current_realized_vol_annualized = math.sqrt(sigma_squared_current * TRADING_DAYS_PER_YEAR)

    # 3. Derive omega from sigma_squared_LR (mean-reversion target).
    omega = sigma_squared_lr * (1.0 - _PERSISTENCE)

    # 4. Run the GARCH recursion forward from sigma_squared_current.
    # The "one-step-ahead" variance uses the most recent return as the
    # innovation, with sigma_squared_current as the starting variance.
    last_return = float(log_returns[-1])
    sigma_squared_t1 = omega + _ALPHA * (last_return ** 2) + _BETA * sigma_squared_current

    # 5. Generate the forecast path out to the longer horizon (60d).
    # sigma_squared(t+h) = sigma_squared_LR + persistence^(h-1) * (sigma_squared(t+1) - sigma_squared_LR)
    horizons = np.arange(1, _HORIZON_60 + 1)
    persistence_powers = _PERSISTENCE ** (horizons - 1)
    sigma_squared_path = sigma_squared_lr + persistence_powers * (sigma_squared_t1 - sigma_squared_lr)

    # 6. Horizon-averaged variance, then annualized vol.
    forecast_30_vol_annualized = math.sqrt(float(sigma_squared_path[:_HORIZON_30].mean()) * TRADING_DAYS_PER_YEAR)
    forecast_60_vol_annualized = math.sqrt(float(sigma_squared_path.mean()) * TRADING_DAYS_PER_YEAR)

    # 7. Confidence band from anchor tier (advisory label only).
    band = _BAND_BY_TIER.get(tier or "", "moderate")

    # 8. Model metadata
    half_life = math.log(0.5) / math.log(_PERSISTENCE) if _PERSISTENCE < 1.0 else float("inf")

    narrative = _build_narrative(
        ticker=ticker,
        current_vol=current_realized_vol_annualized,
        forecast_30=forecast_30_vol_annualized,
        forecast_60=forecast_60_vol_annualized,
        long_run_vol=long_run_vol_annualized,
        band=band,
        half_life=half_life,
    )

    return {
        "status": "ok",
        "current_realized_pct": current_realized_vol_annualized,
        "forecast_30d_pct": forecast_30_vol_annualized,
        "forecast_60d_pct": forecast_60_vol_annualized,
        "long_run_vol_pct": long_run_vol_annualized,
        "confidence_band": band,
        "model": "GARCH(1,1)",
        "model_params": {
            "alpha": _ALPHA,
            "beta": _BETA,
            "omega": omega,
            "persistence": _PERSISTENCE,
            "half_life_days": half_life,
        },
        "narrative": narrative,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- helpers ----------


def _extract_closes(prices: list[dict]) -> np.ndarray:
    """Pull adj_close (preferred) or close from the price-bar list."""
    out = []
    for p in prices:
        c = p.get("adj_close") or p.get("close")
        if c is not None and c > 0:
            out.append(float(c))
    return np.array(out, dtype=float)


def _build_narrative(
    ticker: str,
    current_vol: float,
    forecast_30: float,
    forecast_60: float,
    long_run_vol: float,
    band: str,
    half_life: float,
) -> str:
    """One paragraph explaining the vol level + forecast direction."""
    # Direction of 30-day forecast vs current — same threshold the dashboard uses
    if forecast_30 < current_vol * 0.92:
        direction_phrase = "expected to fall meaningfully"
    elif forecast_30 > current_vol * 1.08:
        direction_phrase = "expected to rise"
    else:
        direction_phrase = "expected to stay roughly flat"

    # Position vs long-run
    if current_vol < long_run_vol * 0.85:
        regime_phrase = f"compressed (vol is meaningfully below the 1-year average of {long_run_vol*100:.0f}%)"
    elif current_vol > long_run_vol * 1.15:
        regime_phrase = f"elevated (vol is meaningfully above the 1-year average of {long_run_vol*100:.0f}%)"
    else:
        regime_phrase = f"in line with the 1-year average of {long_run_vol*100:.0f}%"

    # Daily-swing equivalent for plain English
    daily_swing = current_vol * 100 / math.sqrt(TRADING_DAYS_PER_YEAR)

    return (
        f"{ticker} realized vol is {current_vol*100:.1f}%/yr trailing 60 days "
        f"(typical daily move ~{daily_swing:.1f}%), {regime_phrase}. "
        f"GARCH(1,1) projects {forecast_30*100:.1f}%/yr over the next 30d "
        f"and {forecast_60*100:.1f}%/yr over 60d — {direction_phrase}. "
        f"Shock half-life is ~{half_life:.0f} sessions (today's volatility innovations decay to "
        f"half-impact after that many days). Confidence band: {band} (set by anchor tier)."
    )


# ---------- CLI smoke test ----------


def _print_summary(ticker: str, payload: dict) -> None:
    print(f"\n=== {ticker} ===")
    print(f"  model:                 {payload['model']}")
    print(f"  current realized vol:  {payload['current_realized_pct']*100:6.1f}%/yr (60d trailing)")
    print(f"  forecast 30d:          {payload['forecast_30d_pct']*100:6.1f}%/yr")
    print(f"  forecast 60d:          {payload['forecast_60d_pct']*100:6.1f}%/yr")
    print(f"  long-run vol:          {payload['long_run_vol_pct']*100:6.1f}%/yr (1y window)")
    print(f"  confidence band:       {payload['confidence_band']}")
    p = payload["model_params"]
    print(f"  GARCH params:          alpha={p['alpha']:.3f} beta={p['beta']:.3f} omega={p['omega']:.2e}")
    print(f"  persistence:           {p['persistence']:.3f}  half-life {p['half_life_days']:.1f} sessions")
    print(f"\n  narrative:")
    print(f"    {payload['narrative']}")


def main() -> int:
    from src.pipeline import data as data_step

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Volatility forecast smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to forecast")
    parser.add_argument("--tier", default=None, help="anchor tier (A/B/C); else taken from watchlist")
    args = parser.parse_args()

    # Load watchlist for tier lookup if --tier not specified
    import yaml
    watchlist = {}
    if config.WATCHLIST_PATH.exists():
        with config.WATCHLIST_PATH.open() as f:
            watchlist = yaml.safe_load(f) or {}

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
        tier = args.tier or (watchlist.get(ticker) or {}).get("tier")
        try:
            payload = forecast(ticker, price_data, tier=tier)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR forecasting {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
