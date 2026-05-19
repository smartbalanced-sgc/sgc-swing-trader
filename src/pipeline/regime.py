"""Step 2 — Regime detection.

Classifies the current volatility/trend regime into one of five states
(uptrend_quiet, uptrend_noisy, downtrend, sideways, crisis) using a
**feature-centroid + softmax classifier** rather than the originally
specified Hidden Markov Model.

Why the design pivot (HMM → centroid classifier):

  1. **Explainability matters more than theoretical elegance.** A user
     asking "why did the engine call this uptrend_noisy and not
     crisis?" gets a clean three-feature answer with this approach.
     HMM Baum-Welch outputs are notoriously opaque — the state
     assignments work but defy intuitive justification.

  2. **Deterministic and reproducible.** Same data → same result,
     every run, forever. HMM EM training can converge to different
     local optima across runs depending on initialization; even
     hmmlearn's "best of N restarts" mode has run-to-run jitter on
     real financial data.

  3. **No exotic dependencies.** Pure numpy. `hmmlearn` is a
     maintenance burden — last release was 2024, it's sensitive to
     numpy/scipy ABI breaks (we just lived through one of those for
     pandas_market_calendars), and dropping it now means one less
     pinned upper bound in requirements.txt.

  4. **Calibratable without code changes.** The feature centroids and
     normalization scales live in `config/thresholds.yml`. Tuning is
     a one-line YAML edit + commit message explaining rationale —
     exactly the calibration philosophy the rest of the system uses.

  5. **Still produces real confidence.** Softmax over normalized
     distance-to-centroid gives a proper 0-1 probability. The
     70%-confidence threshold for the Layer-3 ENTER veto remains
     meaningful — at a true regime boundary, no single centroid
     dominates and confidence correctly stays below the veto floor.

  6. **HMM transition memory is approximated.** A true HMM models
     state-transition probabilities (the temporal memory that prefers
     persisting in the current state over flipping each day). We
     approximate that with `days_in_regime` — re-classify each of the
     last N days and find the most recent flip. Downstream consumers
     (conviction Layer-3 veto, dashboard narrative) only need the
     "current state + how long it's been" signal, which this gives
     them directly.

The three features (computed over the last `feature_window_days` daily
log-returns):

  1. **trend_annualized** — slope of log-price vs day index, scaled
     to annualized return. Captures direction.
  2. **vol_annualized** — stdev of daily log-returns × sqrt(252).
     Captures magnitude.
  3. **vol_of_vol** — stdev of rolling-`vol_of_vol_inner_window`-day
     vol across the feature window, annualized. Captures regime
     stability — "is the vol regime itself stable or shifting?"

Each state has a centroid in this 3-D feature space. The classifier
computes normalized Euclidean distance from observed features to each
centroid, applies softmax (temperature controls decisiveness), and
returns argmax + the winning probability as confidence.

Output schema (matches dashboard `_render_regime` and conviction's
`regime_state` / `regime_confidence` inputs):

    {
        "status": "ok",
        "state": str,                       # one of regime.states
        "confidence": float in [0, 1],
        "days_in_regime": int,
        "annualized_drift_implied": float,  # from drift_per_regime[state]
        "veto_active": bool,
        "narrative": str,
        "features": {
            "trend_annualized": float,
            "vol_annualized": float,
            "vol_of_vol": float,
        },
        "state_probabilities": {state: prob, ...},   # full distribution
        "fetched_at": ISO timestamp,
    }

CLI smoke test:

    python -m src.pipeline.regime NVDA
    python -m src.pipeline.regime NVDA AMAT IONQ
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
_R = config.THRESHOLDS.regime
_STATES: list[str] = list(_R.states)
_DRIFT_PER_REGIME: dict[str, float] = {s: getattr(_R.drift_per_regime, s) for s in _STATES}
_FEATURE_WINDOW = _R.feature_window_days
_VOV_INNER_WINDOW = _R.vol_of_vol_inner_window
_DAYS_IN_REGIME_LOOKBACK = _R.days_in_regime_lookback
_SOFTMAX_TEMPERATURE = _R.softmax_temperature

# Normalization scales — used to weight feature dimensions equally
# despite their very different natural units.
_SCALE_TREND = _R.normalization_scale.trend_annualized
_SCALE_VOL = _R.normalization_scale.vol_annualized
_SCALE_VOV = _R.normalization_scale.vol_of_vol

# Cap for the trend feature when computing classifier distance.
# Observed annualized log-return slope on a 60-day window during
# parabolic moves can hit hundreds of percent (NVDA: +135%/yr, IONQ:
# +745%/yr in May 2026). Without a cap, the trend dimension hijacks
# the distance metric for momentum stocks and forces every other
# feature (vol, vov) to fight for irrelevance. Capping at ±50%/yr
# treats anything beyond as "momentum at the rail" — vol and vov take
# over the discrimination from there. Raw uncapped value is still
# surfaced in the `features` dict so users see reality.
_TREND_CAP_ABS = 0.50

# Centroid feature vectors per state, materialized once at import.
# Each row is [trend_annualized, vol_annualized, vol_of_vol] aligned
# to _STATES order so softmax indexing matches.
_CENTROIDS: np.ndarray = np.array(
    [
        [
            getattr(getattr(_R.centroids, s), "trend_annualized"),
            getattr(getattr(_R.centroids, s), "vol_annualized"),
            getattr(getattr(_R.centroids, s), "vol_of_vol"),
        ]
        for s in _STATES
    ],
    dtype=float,
)
_NORMALIZER: np.ndarray = np.array([_SCALE_TREND, _SCALE_VOL, _SCALE_VOV], dtype=float)

# Layer-3 ENTER veto config
_VETO_REGIMES: set[str] = set(config.THRESHOLDS.conviction.vetoes.regime.enter_veto_regimes)
_VETO_MIN_CONFIDENCE: float = config.THRESHOLDS.conviction.vetoes.regime.enter_veto_min_confidence

TRADING_DAYS_PER_YEAR = 252


# ---------- public API ----------


def detect(ticker: str, price_data: dict, tier: str | None = None) -> dict:
    """Classify the current regime for one ticker.

    Args:
        ticker: ticker symbol.
        price_data: output of src.pipeline.data.fetch(ticker). Must
            include a 'prices' list with chronologically-ordered daily
            bars (each having a 'close' field).
        tier: anchor tier from watchlist (advisory — not used in v1
            classification, kept for signature symmetry with other
            pipeline steps and future per-tier calibration).

    Returns the payload described in the module docstring.

    Raises:
        ValueError: if there isn't enough price history to compute the
            features (need at least feature_window_days bars). The
            orchestrator will catch this and record status='fail' in
            the snapshot.
    """
    del tier  # unused in v1; reserved for future per-tier calibration
    prices = price_data.get("prices") or []
    closes = _extract_closes(prices)

    if len(closes) < _FEATURE_WINDOW + 1:
        raise ValueError(
            f"need at least {_FEATURE_WINDOW + 1} price bars to compute regime "
            f"features (got {len(closes)}); ticker may be too new"
        )

    # Compute today's classification using the most recent feature window.
    features = _compute_features(closes[-(_FEATURE_WINDOW + 1):])
    probs, state, confidence = _classify(features)

    # Walk back through history to find when the current state began —
    # gives us the days_in_regime signal that approximates HMM state
    # persistence without requiring HMM math.
    days_in_regime = _count_days_in_current_regime(closes, current_state=state)

    drift = _DRIFT_PER_REGIME[state]
    veto_active = state in _VETO_REGIMES and confidence >= _VETO_MIN_CONFIDENCE

    narrative = _build_narrative(
        state=state,
        confidence=confidence,
        days_in_regime=days_in_regime,
        features=features,
        probs=probs,
        veto_active=veto_active,
        ticker=ticker,
    )

    return {
        "status": "ok",
        "state": state,
        "confidence": confidence,
        "days_in_regime": days_in_regime,
        "annualized_drift_implied": drift,
        "veto_active": veto_active,
        "narrative": narrative,
        "features": {
            "trend_annualized": features[0],
            "vol_annualized": features[1],
            "vol_of_vol": features[2],
        },
        "state_probabilities": {s: p for s, p in zip(_STATES, probs)},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- features ----------


def _extract_closes(prices: list[dict]) -> np.ndarray:
    """Pull adj_close (preferred) or close from the price-bar list.
    Returns a numpy array of floats. Drops any None entries."""
    out = []
    for p in prices:
        c = p.get("adj_close") or p.get("close")
        if c is not None and c > 0:
            out.append(float(c))
    return np.array(out, dtype=float)


def _compute_features(closes: np.ndarray) -> np.ndarray:
    """Compute the 3-feature regime vector from a slice of closes.

    Input is expected to be `feature_window_days + 1` closes (one extra
    for the lead-in to compute `feature_window_days` log-returns). If
    shorter, vol-of-vol may degrade or fall back to 0.
    """
    log_returns = np.diff(np.log(closes))

    # 1. Trend: slope of log-price vs day index, annualized.
    # Use the log prices directly (excluding the lead-in) so the slope
    # is in units of "log-return per trading day" → ×252 = annualized
    # log-return → exp - 1 = annualized arithmetic return.
    log_prices = np.log(closes[1:])  # align with log_returns
    days = np.arange(len(log_prices), dtype=float)
    slope = _ols_slope(days, log_prices)
    trend_annualized = math.exp(slope * TRADING_DAYS_PER_YEAR) - 1.0

    # 2. Realized vol: stdev of log-returns × sqrt(252).
    vol_annualized = float(np.std(log_returns, ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)

    # 3. Vol-of-vol: stdev of rolling-N-day vol across the window.
    rolling_vols = _rolling_std(log_returns, window=_VOV_INNER_WINDOW)
    # Annualize each rolling vol (same factor) before measuring its
    # stdev — keeps vol_of_vol in interpretable annualized units.
    rolling_vols_annualized = rolling_vols * math.sqrt(TRADING_DAYS_PER_YEAR)
    vol_of_vol = float(np.std(rolling_vols_annualized, ddof=1)) if len(rolling_vols) >= 2 else 0.0

    return np.array([trend_annualized, vol_annualized, vol_of_vol], dtype=float)


def _ols_slope(x: np.ndarray, y: np.ndarray) -> float:
    """Pure-numpy OLS slope. Avoids importing scipy just for this."""
    x_mean = x.mean()
    y_mean = y.mean()
    denom = float(((x - x_mean) ** 2).sum())
    if denom == 0:
        return 0.0
    return float(((x - x_mean) * (y - y_mean)).sum() / denom)


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling stdev over a 1-D array with ddof=1. Returns an array of
    length max(0, len(arr) - window + 1). If `arr` is shorter than
    `window`, returns an empty array."""
    if len(arr) < window:
        return np.array([], dtype=float)
    n = len(arr) - window + 1
    out = np.empty(n, dtype=float)
    for i in range(n):
        out[i] = arr[i : i + window].std(ddof=1)
    return out


# ---------- classification ----------


def _classify(features: np.ndarray) -> tuple[np.ndarray, str, float]:
    """Score the features against all centroids, softmax, return
    (probability vector, selected state, confidence).

    The trend feature is clipped to ±_TREND_CAP_ABS before distance
    computation so that parabolic-momentum names don't hijack the
    classification via an extreme trend value. Vol and vov are not
    clipped — their natural ranges (0-1.5 and 0-1) are already
    bounded enough to behave well in distance space."""
    # Clip trend for classification only; keep raw value in features dict.
    features_clipped = features.copy()
    features_clipped[0] = max(-_TREND_CAP_ABS, min(_TREND_CAP_ABS, features_clipped[0]))

    deltas = (_CENTROIDS - features_clipped) / _NORMALIZER  # shape: (n_states, 3)
    distances = np.linalg.norm(deltas, axis=1)  # shape: (n_states,)
    # Softmax over negative distance (smaller distance → higher prob).
    # Subtract max for numerical stability.
    scores = -distances / max(_SOFTMAX_TEMPERATURE, 1e-9)
    scores -= scores.max()
    exps = np.exp(scores)
    probs = exps / exps.sum()
    winner_idx = int(np.argmax(probs))
    return probs, _STATES[winner_idx], float(probs[winner_idx])


def _count_days_in_current_regime(closes: np.ndarray, current_state: str) -> int:
    """Walk backward day-by-day, re-classifying with each day's lookback
    feature window, returning how many consecutive trailing days share
    the current state (today inclusive).

    Skips computation if there's insufficient history for the walkback;
    returns 1 in that case (the current day itself).
    """
    days = 1  # today counts
    # We need feature_window_days + 1 closes ending at each historical
    # day. Walk backward; stop when state diverges from current_state
    # OR when we run out of history OR when we've checked lookback days.
    for offset in range(1, _DAYS_IN_REGIME_LOOKBACK + 1):
        end = len(closes) - offset
        start = end - (_FEATURE_WINDOW + 1)
        if start < 0:
            break
        window = closes[start:end]
        features = _compute_features(window)
        _, state, _ = _classify(features)
        if state != current_state:
            break
        days += 1
    return days


# ---------- narrative ----------


def _build_narrative(
    state: str,
    confidence: float,
    days_in_regime: int,
    features: np.ndarray,
    probs: np.ndarray,
    veto_active: bool,
    ticker: str,
) -> str:
    """One paragraph of plain English explaining the classification —
    what was detected, what drove it, and what the runner-up state
    is. The dashboard renders this verbatim under the state row."""
    trend_pct = features[0] * 100
    vol_pct = features[1] * 100
    vov_pct = features[2] * 100

    # Runner-up state for "what if not"
    sorted_idx = np.argsort(probs)[::-1]
    runner_up_idx = int(sorted_idx[1]) if len(sorted_idx) > 1 else None
    runner_up_phrase = ""
    if runner_up_idx is not None:
        runner_up_state = _STATES[runner_up_idx]
        runner_up_prob = float(probs[runner_up_idx])
        if runner_up_prob >= 0.15:
            runner_up_phrase = (
                f" Nearest alternative is {runner_up_state} "
                f"at {runner_up_prob*100:.0f}% — the call is "
                f"{'clean' if confidence - runner_up_prob >= 0.25 else 'at a boundary, treat with care'}."
            )

    direction_phrase = {
        "uptrend_quiet": "trending up with predictable day-to-day moves",
        "uptrend_noisy": "trending up but with elevated daily swings",
        "downtrend": "in a sustained downtrend",
        "sideways": "range-bound with no clear direction",
        "crisis": "in a high-volatility breakdown regime",
    }.get(state, state)

    duration_phrase = (
        f"first session in this regime"
        if days_in_regime <= 1
        else f"{days_in_regime} sessions in this regime — {'persistent' if days_in_regime >= 20 else 'recent flip'}"
    )

    return (
        f"{ticker} is {direction_phrase} ({confidence*100:.0f}% confidence). "
        f"Observed features over last {_FEATURE_WINDOW} sessions: "
        f"trend {trend_pct:+.1f}%/yr implied, realized vol {vol_pct:.0f}%/yr, "
        f"vol-of-vol {vov_pct:.0f}%. "
        f"{duration_phrase}.{runner_up_phrase}"
        + (" Layer-3 ENTER veto is firing." if veto_active else "")
    )


# ---------- CLI smoke test ----------


def _print_summary(payload: dict) -> None:
    print(f"  state:              {payload['state']}")
    print(f"  confidence:         {payload['confidence']*100:.1f}%")
    print(f"  days_in_regime:     {payload['days_in_regime']}")
    print(f"  drift implied:      {payload['annualized_drift_implied']*100:+.1f}%/yr (fed to MC)")
    print(f"  veto_active:        {payload['veto_active']}")
    f = payload["features"]
    print(f"  features:")
    print(f"    trend_annualized: {f['trend_annualized']*100:+6.1f}%/yr")
    print(f"    vol_annualized:   {f['vol_annualized']*100:6.1f}%/yr")
    print(f"    vol_of_vol:       {f['vol_of_vol']*100:6.1f}%")
    print(f"  state probabilities:")
    for s, p in sorted(payload["state_probabilities"].items(), key=lambda kv: -kv[1]):
        print(f"    {s:<15} {p*100:5.1f}%")
    print(f"\n  narrative:")
    print(f"    {payload['narrative']}")


def main() -> int:
    from src.pipeline import data as data_step

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Regime detector smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to classify")
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
            payload = detect(ticker, price_data)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR classifying {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        print(f"\n=== {ticker} ===")
        _print_summary(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
