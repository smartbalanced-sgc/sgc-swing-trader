"""§5.1 Conviction engine — three-layer scoring + verdict labelling.

See docs/V1_SPEC.md §5.1 (forthcoming). Pure function over the inputs
the pipeline produces. No I/O, no FMP calls, no dependencies beyond
config.

The model has three layers (documented at length in V1_SPEC §5.1):

  Layer 1 — EDGE         "Does the math say yes?"
    edge_score = ev_weight × normalized_EV
               + prob_weight × (P(target) - P(stop))
    Hard precondition: P(target) - P(stop) > lottery_filter_threshold
    (else edge_score = 0).

  Layer 2 — CONFIDENCE   "Should we trust the math?"
    Multiplier on edge_score. Three multiplicative haircuts:
      - MC↔PDE disagreement (graduated by delta size)
      - Trajectory flip-flopping
      - Watchlist tier ≠ measured tier (sustained)

  Layer 3 — VETOES       "Specific reasons to back off"
    Discrete label override (verdict label, not score). One or more of:
      - Catalyst inside horizon + watching state (ENTER → WAIT)
      - Regime is downtrend/crisis with high confidence (ENTER → WAIT)
      - Fair value premium ≥ N σ (ENTER → SKIP for watching; TRIM for entered)
      - Liquidity below floor (Tier C only) (always SKIP)

All thresholds come from config/thresholds.yml. To tune, edit YAML and
commit — no code changes needed.

Inputs are a "ConvictionInputs" dict; outputs a structured "breakdown"
dict the dashboard can render directly.
"""

from __future__ import annotations

from src import config


# ---------- public API ----------


def evaluate(inputs: dict, horizon_days: int) -> dict:
    """Compute the conviction breakdown for one (ticker, horizon, user).

    Required keys in `inputs`:
        p_target            float in [0, 1]   — MC P(hit target first)
        p_stop              float in [0, 1]   — MC P(hit stop first)
        ev_normalized       float             — EV / stop_distance (units of risk)
        mc_pde_p_target_delta_pp  float       — |MC - PDE| on P(target), in pp
        trajectory_direction_changes  int     — direction flips in lookback window
        watchlist_tier      str               — "A" / "B" / "C"
        measured_tier       str               — "A" / "B" / "C"
        tier_mismatch_consecutive_nights int  — # nights anchor ≠ measured
        catalyst_distance_sessions  int|None  — None if no scheduled catalyst
        regime_state        str               — one of regime.states
        regime_confidence   float in [0, 1]
        fair_value_premium_sigmas  float      — (price - FV_mean) / FV_sigma
        avg_daily_dollar_volume    float      — for the Tier C liquidity veto
        user_state          str               — "watching" or "entered"

    Returns a breakdown dict whose shape matches the dashboard renderer's
    expectations — see _layer1_edge, _layer2_confidence, _layer3_vetoes
    for the per-layer schemas, and the assembled result at the bottom of
    `evaluate`.
    """
    cfg = config.THRESHOLDS.conviction

    layer1 = _layer1_edge(inputs, cfg.edge)
    layer2 = _layer2_confidence(inputs, cfg.haircuts)
    layer3 = _layer3_vetoes(inputs, cfg.vetoes, horizon_days)

    final_score = layer1["score"] * layer2["multiplier"]

    label, label_reason = _derive_label(
        final_score=final_score,
        user_state=inputs["user_state"],
        vetoes=layer3["fired"],
        thresholds=cfg.vetoes,
    )

    return {
        "horizon_days": horizon_days,
        "final_score": final_score,
        "verdict_label": label,
        "verdict_reason": label_reason,
        "layer1_edge": layer1,
        "layer2_confidence": layer2,
        "layer3_vetoes": layer3,
    }


# ---------- Layer 1 — Edge ----------


def _layer1_edge(inputs: dict, edge_cfg) -> dict:
    p_target = inputs["p_target"]
    p_stop = inputs["p_stop"]
    ev_norm = inputs["ev_normalized"]

    prob_diff = p_target - p_stop
    # Lottery filter: trade is a "lottery" (huge upside × tiny probability
    # inflating EV on a structurally bad bet) only when BOTH the
    # probability difference is poor AND the EV is non-positive. A trade
    # with negative prob_diff but POSITIVE EV is not a lottery — it's
    # a legitimate asymmetric-payoff swing trade, which our vol-scaled
    # targets (1 sigma reward / 0.7 sigma risk) produce by design. The
    # earlier strict filter (prob_diff alone) blocked every watching
    # default and made the engine output meaningless SKIPs across the
    # board. Both conditions together preserve the original intent
    # (block tiny-probability × huge-payoff lotteries with negative EV)
    # while letting real swing setups through.
    lottery_filter_failed = (
        prob_diff <= edge_cfg.lottery_filter_min_prob_diff
        and ev_norm <= 0.0
    )

    # Normalize EV to [0, 1] by capping at the configured units-of-risk
    # ceiling. EV below 0 gets clipped to 0 (the lottery filter handles
    # the "negative-edge" case anyway).
    ev_capped = max(0.0, min(ev_norm, edge_cfg.ev_cap_units_of_risk))
    ev_score = ev_capped / edge_cfg.ev_cap_units_of_risk

    # Probability term: map prob_diff from [-1, 1] to [0, 1]. (A 0
    # prob_diff scores 0.5 here — but the lottery filter blocks it
    # from reaching the final score.)
    prob_score = max(0.0, min(1.0, (prob_diff + 1.0) / 2.0))

    raw_score = (edge_cfg.ev_weight * ev_score) + (edge_cfg.probability_weight * prob_score)
    score = 0.0 if lottery_filter_failed else raw_score

    return {
        "score": score,
        "lottery_filter_failed": lottery_filter_failed,
        "components": [
            {
                "name": "P(target) - P(stop)",
                "raw_value": prob_diff,
                "display": f"{p_target:.2f} - {p_stop:.2f} = {prob_diff:+.2f}",
                "normalized": prob_score,
                "weight": edge_cfg.probability_weight,
                "contribution": edge_cfg.probability_weight * prob_score,
            },
            {
                "name": "EV (units of stop-distance)",
                "raw_value": ev_norm,
                "display": f"{ev_norm:+.2f}× risk",
                "normalized": ev_score,
                "weight": edge_cfg.ev_weight,
                "contribution": edge_cfg.ev_weight * ev_score,
            },
        ],
    }


# ---------- Layer 2 — Confidence ----------


def _layer2_confidence(inputs: dict, haircut_cfg) -> dict:
    haircuts: list[dict] = []

    # MC ↔ PDE disagreement (graduated)
    delta = inputs["mc_pde_p_target_delta_pp"]
    mc_pde_haircut = 0.0
    mc_pde_band = "no haircut"
    if delta >= haircut_cfg.mc_pde_disagreement.high_band.min_pp_delta:
        mc_pde_haircut = haircut_cfg.mc_pde_disagreement.high_band.haircut
        mc_pde_band = f"high band (Δ {delta:.1f}pp ≥ {haircut_cfg.mc_pde_disagreement.high_band.min_pp_delta}pp)"
    elif delta >= haircut_cfg.mc_pde_disagreement.low_band.min_pp_delta:
        mc_pde_haircut = haircut_cfg.mc_pde_disagreement.low_band.haircut
        mc_pde_band = f"low band ({haircut_cfg.mc_pde_disagreement.low_band.min_pp_delta}pp ≤ Δ {delta:.1f}pp < {haircut_cfg.mc_pde_disagreement.low_band.max_pp_delta}pp)"
    haircuts.append({
        "name": "MC ↔ PDE agreement",
        "detail": f"Δ on P(target) = {delta:.1f}pp",
        "band": mc_pde_band,
        "haircut": mc_pde_haircut,
        "passed": mc_pde_haircut == 0.0,
    })

    # Trajectory instability
    flips = inputs["trajectory_direction_changes"]
    traj_haircut = 0.0
    if flips >= haircut_cfg.trajectory_instability.min_direction_changes:
        traj_haircut = haircut_cfg.trajectory_instability.haircut
    haircuts.append({
        "name": "Trajectory stability",
        "detail": f"{flips} direction change(s) in last {haircut_cfg.trajectory_instability.lookback_nights} nights",
        "band": "unstable" if traj_haircut > 0 else "stable",
        "haircut": traj_haircut,
        "passed": traj_haircut == 0.0,
    })

    # Tier mismatch (sustained)
    anchor = inputs["watchlist_tier"]
    measured = inputs["measured_tier"]
    mismatch_nights = inputs["tier_mismatch_consecutive_nights"]
    tier_haircut = 0.0
    if anchor != measured and mismatch_nights >= haircut_cfg.tier_mismatch.min_consecutive_nights_of_disagreement:
        tier_haircut = haircut_cfg.tier_mismatch.haircut
    haircuts.append({
        "name": "Tier classifier match",
        "detail": f"anchor={anchor}, measured={measured}, {mismatch_nights} consecutive night(s) mismatched",
        "band": "mismatched (sustained)" if tier_haircut > 0 else "matched",
        "haircut": tier_haircut,
        "passed": tier_haircut == 0.0,
    })

    # Compound multiplicatively
    multiplier = 1.0
    for h in haircuts:
        multiplier *= (1.0 - h["haircut"])

    return {
        "multiplier": multiplier,
        "haircuts": haircuts,
    }


# ---------- Layer 3 — Vetoes ----------


def _layer3_vetoes(inputs: dict, veto_cfg, horizon_days: int) -> dict:
    fired: list[dict] = []
    checked: list[dict] = []

    # Catalyst — in-horizon ENTER veto (watching only).
    cat_dist = inputs.get("catalyst_distance_sessions")
    cat_veto = (
        veto_cfg.catalyst.in_horizon_veto.enabled
        and cat_dist is not None
        and cat_dist <= horizon_days
        and inputs["user_state"] == "watching"
    )
    cat_entry = {
        "name": "Catalyst proximity",
        "detail": (
            f"catalyst at {cat_dist} sessions (horizon {horizon_days}d)"
            if cat_dist is not None
            else "no scheduled catalyst"
        ),
        "fires": cat_veto,
        "effect": f"ENTER → WAIT — catalyst inside {horizon_days}d window, defer until post-print" if cat_veto else None,
    }
    if cat_veto:
        fired.append(cat_entry)
    checked.append(cat_entry)

    # Regime — downtrend/crisis veto.
    regime = inputs["regime_state"]
    regime_conf = inputs["regime_confidence"]
    regime_veto = (
        regime in list(veto_cfg.regime.enter_veto_regimes)
        and regime_conf >= veto_cfg.regime.enter_veto_min_confidence
        and inputs["user_state"] == "watching"
    )
    regime_entry = {
        "name": "Regime",
        "detail": f"{regime} @ {regime_conf*100:.0f}% confidence",
        "fires": regime_veto,
        "effect": f"ENTER → WAIT — {regime} regime, defer until reversal signal" if regime_veto else None,
    }
    if regime_veto:
        fired.append(regime_entry)
    checked.append(regime_entry)

    # Fair value — premium veto.
    fv_sigma = inputs["fair_value_premium_sigmas"]
    fv_threshold = veto_cfg.fair_value.enter_skip_premium_sigmas
    fv_veto_watching = fv_sigma >= fv_threshold and inputs["user_state"] == "watching"
    fv_veto_entered = (
        fv_sigma >= veto_cfg.fair_value.trim_for_entered_premium_sigmas
        and inputs["user_state"] == "entered"
    )
    fv_entry = {
        "name": "Fair value premium",
        "detail": f"{fv_sigma:+.2f}σ vs FV range",
        "fires": fv_veto_watching or fv_veto_entered,
        "effect": (
            f"ENTER → SKIP — price ≥ {fv_threshold}σ above fair value" if fv_veto_watching
            else f"HOLD → TRIM — price ≥ {fv_threshold}σ above fair value" if fv_veto_entered
            else None
        ),
    }
    if fv_veto_watching or fv_veto_entered:
        fired.append(fv_entry)
    checked.append(fv_entry)

    # Liquidity — Tier C only.
    is_tier_c = inputs["watchlist_tier"] == "C" or inputs["measured_tier"] == "C"
    adv = inputs["avg_daily_dollar_volume"]
    liq_min = veto_cfg.liquidity_tier_c_only.min_adv_usd
    liq_veto = is_tier_c and adv < liq_min
    liq_entry = {
        "name": "Liquidity floor (Tier C only)",
        "detail": (
            f"ADV ${adv/1e6:.1f}M (floor ${liq_min/1e6:.1f}M)" if is_tier_c
            else "not Tier C — veto not applicable"
        ),
        "fires": liq_veto,
        "effect": "SKIP — ADV below Tier-C bare-minimum guard" if liq_veto else None,
    }
    if liq_veto:
        fired.append(liq_entry)
    checked.append(liq_entry)

    return {
        "fired": fired,
        "all_checks": checked,
    }


# ---------- Final label derivation ----------


def _derive_label(final_score: float, user_state: str, vetoes: list, thresholds) -> tuple[str, str]:
    """Map score + vetoes → verdict label.

    Principle: vetoes can only make the verdict MORE conservative, never
    less. If the score-derived label is already SKIP, a veto saying
    "WAIT" doesn't relax it back to WAIT.
    """
    # Severity ordering — higher = more conservative.
    severity = {
        "ENTER": 0, "HOLD": 0,   # most permissive
        "WAIT": 2,
        "TRIM": 2,
        "SKIP": 4, "EXIT": 4,    # most conservative
    }

    # Score-derived label (no vetoes applied yet).
    if user_state == "watching":
        if final_score >= thresholds.enter_score_threshold:
            label = "ENTER"
            reason = f"conviction {final_score:.2f} ≥ ENTER threshold {thresholds.enter_score_threshold:.2f}"
        elif final_score >= thresholds.wait_score_floor:
            label = "WAIT"
            reason = f"conviction {final_score:.2f} between WAIT floor and ENTER threshold"
        else:
            label = "SKIP"
            reason = f"conviction {final_score:.2f} below WAIT floor {thresholds.wait_score_floor:.2f}"
    elif user_state == "entered":
        if final_score >= thresholds.wait_score_floor:
            label = "HOLD"
            reason = f"conviction {final_score:.2f} still supports the thesis"
        else:
            label = "EXIT"
            reason = f"conviction {final_score:.2f} below WAIT floor — thesis weakened"
    else:
        return "—", "no verdict (state unknown)"

    # Apply Layer-3 vetoes — each can only push the label MORE conservative.
    for v in vetoes:
        if not v.get("effect"):
            continue
        effect = v["effect"]
        # Parse out the implied veto-label from the effect string.
        for candidate in ("SKIP", "EXIT", "WAIT", "TRIM"):
            if candidate in effect:
                # Veto-label must be applicable to the user's state.
                if user_state == "watching" and candidate in ("ENTER", "WAIT", "SKIP"):
                    pass
                elif user_state == "entered" and candidate in ("HOLD", "TRIM", "EXIT"):
                    pass
                else:
                    break
                if severity.get(candidate, 0) > severity.get(label, 0):
                    label = candidate
                    reason = effect
                break

    return label, reason
