"""Swing-mode metrics — planned dip→rally trade analytics.

Where the existing target-mode analytics answer "is this stock likely
to be higher in N days?" (trend-follow at current price, aim for
+1σ target), swing-mode answers "would I get a meaningfully better
entry by waiting for a dip, and is there a realistic exit within the
horizon to plan a sell at?".

The Monte Carlo step (monte_carlo.py) already produces 50k simulated
price paths per horizon. This module re-interprets those paths for
the swing-trade question — no new simulation, just additional
statistics on the path minima/maxima already implicit in the data.

Outputs (per horizon):
  - dip_entry: deepest level 70% of paths touch on the way down
  - rally_exit: highest level 70% of paths touch on the way up
  - sequence_prob: P(dip-then-rally in order, both within horizon)
  - miss_dip_prob: P(rally touched without prior dip)
  - swing_stop: level below dip where recovery odds drop below 25%
  - reward_to_risk: $ upside per $ risked (rally vs swing-stop)
  - verdict: ACTIONABLE / NO_ACTIONABLE_SWING (four-condition gate)

See config/thresholds.yml's `swing_mode` section for all calibration
knobs and their rationales.
"""

from __future__ import annotations

import numpy as np

from src import config

_SM = config.THRESHOLDS.swing_mode

_TOUCH_CONF = _SM.touch_confidence
_RECOVERY_CONF = _SM.recovery_confidence
_MIN_STOP_COHORT = _SM.min_stop_cohort

_MIN_SEQ = _SM.min_sequence_prob
_MIN_RR = _SM.min_reward_to_risk
_MIN_YIELD_PCT = _SM.min_yield_pct
_MIN_DIP_PCT = _SM.min_dip_below_current_pct
_MIN_RALLY_PCT = _SM.min_rally_above_current_pct

_TAIL_PCTILE = _SM.tail_percentile
_DISAGREE_PCT = _SM.cross_check_disagreement_pct


def compute_for_horizon(paths_h: np.ndarray, current_price: float) -> dict:
    """All swing-mode metrics for one horizon.

    `paths_h`: numpy array shape (n_paths, n_days_in_horizon),
               simulated close prices.
    `current_price`: spot price the simulation started from.

    Returns the block to be written under
    snap["swing_mode"]["horizons"][h].
    """
    n_paths, n_days = paths_h.shape
    path_mins = paths_h.min(axis=1)
    path_maxes = paths_h.max(axis=1)

    # --- Dip-entry, rally-exit (70%-touch levels) ---
    dip_entry = float(np.percentile(path_mins, _TOUCH_CONF * 100))
    rally_exit = float(np.percentile(path_maxes, (1.0 - _TOUCH_CONF) * 100))

    # --- Median days-to-touch ---
    median_days_to_dip = int(np.median(paths_h.argmin(axis=1)))
    median_days_to_rally = int(np.median(paths_h.argmax(axis=1)))

    # --- Sequence prob (dip then rally) and miss-dip prob (rally first) ---
    sequence_prob, miss_dip_prob = _compute_sequence_metrics(
        paths_h, dip_entry, rally_exit
    )

    # --- Yield % from dip to rally ---
    yield_pct = (
        (rally_exit - dip_entry) / dip_entry * 100.0 if dip_entry > 0 else 0.0
    )

    # --- Swing stop (conditional recovery) and cross-check (5%-tile) ---
    swing_stop, recovery_prob_at_stop = _conditional_recovery_stop(
        paths_h=paths_h,
        dip_entry=dip_entry,
        recovery_threshold=_RECOVERY_CONF,
        min_cohort=max(_MIN_STOP_COHORT, int(0.005 * n_paths)),
    )
    tail_stop = float(np.percentile(path_mins, _TAIL_PCTILE * 100))

    cross_check_disagrees = _cross_check_disagrees(
        primary=swing_stop, tail_cross=tail_stop,
        anchor=dip_entry, threshold_pct=_DISAGREE_PCT,
    )

    # --- Reward / risk ---
    if swing_stop is not None and dip_entry > swing_stop:
        risk = dip_entry - swing_stop
        reward = rally_exit - dip_entry
        reward_to_risk = reward / risk if risk > 0 else 0.0
    else:
        reward_to_risk = 0.0

    # --- Component % moves ---
    dip_below_current_pct = (
        (current_price - dip_entry) / current_price * 100.0
        if current_price > 0 else 0.0
    )
    rally_above_current_pct = (
        (rally_exit - current_price) / current_price * 100.0
        if current_price > 0 else 0.0
    )

    # --- Verdict: all four gates must pass ---
    passes = {
        "dip_below_current_min": dip_below_current_pct >= _MIN_DIP_PCT,
        "rally_above_current_min": rally_above_current_pct >= _MIN_RALLY_PCT,
        "yield_min": yield_pct >= _MIN_YIELD_PCT,
        "sequence_prob_min": sequence_prob >= _MIN_SEQ,
        "reward_to_risk_min": reward_to_risk >= _MIN_RR,
        "swing_stop_defined": swing_stop is not None,
    }
    all_pass = all(passes.values())

    if all_pass:
        verdict_label = "ACTIONABLE"
        verdict_reason = "all swing conditions met"
    else:
        verdict_label = "NO_ACTIONABLE_SWING"
        verdict_reason = _explain_failures(
            passes=passes,
            dip_pct=dip_below_current_pct,
            rally_pct=rally_above_current_pct,
            yield_pct=yield_pct,
            sequence_prob=sequence_prob,
            reward_to_risk=reward_to_risk,
        )

    return {
        "dip_entry": dip_entry,
        "rally_exit": rally_exit,
        "median_days_to_dip": median_days_to_dip,
        "median_days_to_rally": median_days_to_rally,
        "yield_pct": yield_pct,
        "sequence_prob": sequence_prob,
        "miss_dip_prob": miss_dip_prob,
        "swing_stop": {
            "primary": swing_stop,
            "tail_cross": tail_stop,
            "method": "conditional_recovery",
            "recovery_threshold": _RECOVERY_CONF,
            "recovery_prob_at_stop": recovery_prob_at_stop,
            "cross_check_disagrees": cross_check_disagrees,
        },
        "reward_to_risk": reward_to_risk,
        "components": {
            "dip_below_current_pct": dip_below_current_pct,
            "rally_above_current_pct": rally_above_current_pct,
            "yield_pct": yield_pct,
            "sequence_prob": sequence_prob,
            "miss_dip_prob": miss_dip_prob,
            "reward_to_risk": reward_to_risk,
            "passes": passes,
        },
        "verdict": {
            "label": verdict_label,
            "reason": verdict_reason,
        },
        "thresholds_used": {
            "touch_confidence": _TOUCH_CONF,
            "recovery_confidence": _RECOVERY_CONF,
            "min_sequence_prob": _MIN_SEQ,
            "min_reward_to_risk": _MIN_RR,
            "min_yield_pct": _MIN_YIELD_PCT,
            "min_dip_below_current_pct": _MIN_DIP_PCT,
            "min_rally_above_current_pct": _MIN_RALLY_PCT,
        },
    }


# ---------- sequence + miss-dip probabilities ----------


def _compute_sequence_metrics(
    paths_h: np.ndarray, dip_entry: float, rally_exit: float
) -> tuple[float, float]:
    """Returns (sequence_prob, miss_dip_prob) computed across all paths.

    sequence_prob: fraction of paths that touched dip_entry on some
                   day t1, then touched rally_exit on some later day
                   t2 > t1, both within the horizon.
    miss_dip_prob: fraction of paths that touched rally_exit without
                   first touching dip_entry — these are the futures
                   where waiting for the dip costs you the trade
                   because price runs up without offering an entry.
    """
    n_paths, n_days = paths_h.shape

    below_dip = paths_h <= dip_entry      # (n_paths, n_days)
    above_rally = paths_h >= rally_exit    # (n_paths, n_days)

    has_dip = below_dip.any(axis=1)
    has_rally = above_rally.any(axis=1)

    # First day of each event per path; n_days = "never" sentinel.
    # argmax(bool array) returns first True index, or 0 if all False.
    # Wrap with where(has_*) so "never" is encoded as n_days, not 0.
    first_dip_day = np.where(has_dip, below_dip.argmax(axis=1), n_days)
    first_rally_day = np.where(has_rally, above_rally.argmax(axis=1), n_days)

    # Sequence: dip then rally — first_dip_day < first_rally_day AND
    # both within horizon. (first_rally_day < n_days suffices because
    # if dip was never touched, first_dip_day = n_days, which can't
    # be strictly less than first_rally_day < n_days.)
    sequence_completes = has_dip & has_rally & (first_dip_day < first_rally_day)
    sequence_prob = float(sequence_completes.mean())

    # Miss-the-dip: rally touched, but no prior dip touch. Two cases:
    # (a) rally touched, dip never touched, OR
    # (b) rally touched BEFORE first dip touch.
    rally_without_prior_dip = has_rally & (first_rally_day <= first_dip_day)
    miss_dip_prob = float(rally_without_prior_dip.mean())

    return sequence_prob, miss_dip_prob


# ---------- conditional-recovery swing stop ----------


def _conditional_recovery_stop(
    paths_h: np.ndarray,
    dip_entry: float,
    recovery_threshold: float,
    min_cohort: int,
) -> tuple[float | None, float | None]:
    """Find the deepest stop level below dip_entry where the cohort of
    paths that fell to that depth still has recovery odds ≥ threshold.

    Returns (stop_price, recovery_prob_at_stop) or (None, None) if no
    such level exists (very few paths went below dip, or even the
    full cohort recovers < threshold).

    Recovery definition: a path "recovered" if, after first touching
    dip_entry, its maximum subsequent price reached or exceeded
    dip_entry. This is a simple, intuitive recovery measure; a more
    semantically exact "recovery after first stop trigger" would
    require per-candidate-level recomputation and is deferred (the
    approximation slightly overestimates recovery in the rare
    pattern "recover to dip → fall again below stop"; acceptable
    for v1).

    Algorithm:
      1. For each path that touched dip, record its post-horizon
         minimum (depth) and whether it recovered.
      2. Sort paths by depth ascending (deepest fallers first).
      3. For each candidate stop level X = sorted_depths[k], the
         cohort = first k+1 paths (those that fell to depth ≤ X).
      4. Recovery rate of cohort = cumulative mean of recovered.
      5. Return the smallest k (deepest cohort) where cohort size
         ≥ min_cohort AND recovery rate ≥ threshold.
    """
    n_paths, n_days = paths_h.shape
    path_mins = paths_h.min(axis=1)

    below_dip = paths_h <= dip_entry
    has_dip = below_dip.any(axis=1)
    if not has_dip.any():
        return None, None

    # Per-path recovery flag — did the path reach dip_entry again
    # AFTER first touching it? Compute over paths that touched dip.
    dip_idx = np.where(has_dip)[0]
    first_dip_day = below_dip[dip_idx].argmax(axis=1)

    day_col = np.arange(n_days)[None, :]
    after_dip_mask = day_col > first_dip_day[:, None]
    post_dip = np.where(after_dip_mask, paths_h[dip_idx], -np.inf)
    post_dip_max = post_dip.max(axis=1)
    recovered = post_dip_max >= dip_entry

    # Depth = whole-path min (path touched dip, so min ≤ dip).
    depths = path_mins[dip_idx]

    # Restrict to paths that went STRICTLY below dip (paths that
    # merely grazed and bounced have depth == dip_entry; they aren't
    # informative for "where to set a stop below dip").
    strictly_below = depths < dip_entry
    if not strictly_below.any():
        return None, None

    depths = depths[strictly_below]
    recovered = recovered[strictly_below].astype(int)

    # Sort by depth ascending — deepest falls first.
    order = np.argsort(depths)
    sorted_depths = depths[order]
    sorted_recovered = recovered[order]

    # Cumulative recovery rate. cum_count[k] = k+1 = cohort size at
    # candidate level X = sorted_depths[k].
    cum_rec = np.cumsum(sorted_recovered)
    cum_count = np.arange(1, len(sorted_depths) + 1)
    recovery_rates = cum_rec / cum_count

    # Find smallest k (= deepest cohort) where cohort ≥ min_cohort
    # AND rate ≥ threshold.
    valid = (cum_count >= min_cohort) & (recovery_rates >= recovery_threshold)
    if not valid.any():
        # Even the largest (most-inclusive) cohort doesn't recover
        # enough — there is no safe stop level below dip.
        return None, None

    k = int(np.where(valid)[0][0])
    return float(sorted_depths[k]), float(recovery_rates[k])


# ---------- cross-check disagreement ----------


def _cross_check_disagrees(
    primary: float | None,
    tail_cross: float | None,
    anchor: float,
    threshold_pct: float,
) -> bool:
    """True if primary swing stop differs from the tail cross-check
    by more than threshold_pct of the anchor (dip_entry) price.

    Used as a sanity flag — if the two principled methods disagree
    by > 30% of dip_entry, the MC may be producing an unusual path
    distribution and the swing-stop number warrants manual review.
    """
    if primary is None or tail_cross is None or anchor <= 0:
        return False
    diff_pct = abs(primary - tail_cross) / anchor * 100.0
    return diff_pct > threshold_pct


# ---------- failure-reason text ----------


def _explain_failures(
    passes: dict,
    dip_pct: float,
    rally_pct: float,
    yield_pct: float,
    sequence_prob: float,
    reward_to_risk: float,
) -> str:
    parts = []
    if not passes["dip_below_current_min"]:
        parts.append(
            f"dip is only {dip_pct:.1f}% below current "
            f"(need ≥ {_MIN_DIP_PCT}%)"
        )
    if not passes["rally_above_current_min"]:
        parts.append(
            f"rally is only {rally_pct:.1f}% above current "
            f"(need ≥ {_MIN_RALLY_PCT}%)"
        )
    if not passes["yield_min"]:
        parts.append(
            f"yield is {yield_pct:.1f}% "
            f"(need ≥ {_MIN_YIELD_PCT}%)"
        )
    if not passes["sequence_prob_min"]:
        parts.append(
            f"sequence prob is {sequence_prob * 100:.0f}% "
            f"(need ≥ {_MIN_SEQ * 100:.0f}%)"
        )
    if not passes["reward_to_risk_min"]:
        parts.append(
            f"R:R is {reward_to_risk:.1f}× "
            f"(need ≥ {_MIN_RR:.1f}×)"
        )
    if not passes["swing_stop_defined"]:
        parts.append(
            "no safe stop level below dip — even the broadest cohort "
            f"recovers < {_RECOVERY_CONF * 100:.0f}%"
        )
    return "; ".join(parts) if parts else "unknown failure"
