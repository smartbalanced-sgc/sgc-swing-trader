"""Step 8 - Verdict synthesis.

Translates the snapshot's upstream blocks into the conviction-engine
input shape, runs `conviction.evaluate()` once per (user, horizon)
pair, and assembles the result into the `snap["conviction"]` block
that the dashboard's Action / Conviction / Cross-check panels read.

The actual scoring math lives in `src/conviction.py` (Layer-1 edge,
Layer-2 confidence haircuts, Layer-3 vetoes). This module's job is
the I/O adapter: pull the right fields from the right snap blocks,
provide sensible defaults for steps that aren't wired yet (fair_value,
analytic_verifier, trajectory tracking), and reshape the output.

Why per-ticker (not per-user) entry point:

  The previous stub signature was `synthesize(ticker, snap, user, state)`
  returning a per-user dict. That doesn't match the dashboard's
  expectation - it wants `snap["conviction"]["horizons"][h]["users"][user]`
  nested by horizon first. Doing the per-user loop inside this module
  (rather than in main.py's orchestrator) keeps the conviction block
  contained: one input snapshot, one output block, no caller-side
  reshaping.

Default-handling strategy for not-yet-implemented upstream steps:

  - **mc_pde_p_target_delta_pp** (Step 7 analytic_verifier): default 0
    (no disagreement -> no Layer-2 haircut). When Step 7 ships, the
    real delta replaces the default automatically.
  - **fair_value_premium_sigmas** (Step 5 fair_value): default 0
    (price = fair value -> no Layer-3 veto). When Step 5 ships, the
    real sigma displacement applies.
  - **trajectory_direction_changes** and **tier_mismatch_consecutive_nights**
    (trajectory tracking - requires multi-night history): default 0
    (assume stable, assume aligned). Will start producing meaningful
    values after the snapshot store has accumulated >= 5 nights of
    history (per config.trajectory.smoothing_window_nights).

Output schema (matches `src/dashboard.py:_render_conviction`,
`_render_action`, and `_conviction_headline` expectations — note the
horizon-dict is keyed DIRECTLY by user, not wrapped in a "users"
sub-key, because the headline summary iterates `horizons[h].keys()`
to find the first user and a wrapper would break that):

    {
        "status": "ok",
        "fetched_at": ISO timestamp,
        "horizons": {
            30: {
                "aidy": {
                    "breakdown": { ... from conviction.evaluate() ... },
                    "targets": {
                        "entry": float | None,    # only for entered users
                        "target": float,
                        "stop": float,
                    },
                },
                "jesse": {...},
            },
            60: {...},
        },
    }

CLI smoke test:

    python -m src.pipeline.verdict NVDA
    python -m src.pipeline.verdict NVDA AMAT IONQ
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime, timezone

from src import config, conviction

logger = logging.getLogger(__name__)


# ---------- public API ----------


def synthesize(ticker: str, snap: dict, watchlist_entry: dict) -> dict:
    """Build the full conviction block for one ticker.

    Args:
        ticker: ticker symbol (informational; not used in math).
        snap: in-progress snapshot dict. Required blocks:
            - monte_carlo (with users.{user}.horizons.{30,60}.{p_target, p_stop, ev_normalized})
            - targets (with users.{user}.{target_price, stop_price})
            - regime (with state, confidence)
            - tier_classifier (with measured_tier, properties.adv_usd.value)
            Optional blocks (defaults applied if missing):
            - catalyst.distance_sessions
            - fair_value.premium_sigmas (default 0)
            - analytic_verifier.p_target_delta_pp (default 0)
        watchlist_entry: this ticker's entry from watchlist.yml. Used
            for `holders` map; per-user `state` and (for entered users)
            `entry` field.

    Returns the conviction block (schema in module docstring), OR
    {"status": "fail", "reason": ...} if a required upstream block
    is missing.
    """
    del ticker  # informational only

    mc_block = snap.get("monte_carlo")
    if not mc_block or mc_block.get("status") != "ok":
        return _fail(f"monte_carlo unavailable: {(mc_block or {}).get('reason', 'no MC block')}")

    targets_block = snap.get("targets")
    if not targets_block or targets_block.get("status") != "ok":
        return _fail(f"targets unavailable: {(targets_block or {}).get('reason', 'no targets block')}")

    regime_block = snap.get("regime")
    if not regime_block or regime_block.get("status") != "ok":
        return _fail(f"regime unavailable: {(regime_block or {}).get('reason', 'no regime block')}")

    tier_cls_block = snap.get("tier_classifier")
    if not tier_cls_block or tier_cls_block.get("status") != "ok":
        return _fail(f"tier_classifier unavailable: {(tier_cls_block or {}).get('reason', 'no tier block')}")

    common_inputs = _build_common_inputs(snap)
    holders = (watchlist_entry or {}).get("holders") or {}

    # Output schema is `horizons[h][user]` directly — NOT
    # `horizons[h]["users"][user]`. This matches what the dashboard's
    # _render_conviction and _render_action consume, as well as the
    # _conviction_headline summary which iterates the first user via
    # `next(iter(horizons[first_h].keys()))`. The "users" wrapper was a
    # natural-feeling shape but breaks the headline iteration; staying
    # consistent with the dashboard's read pattern keeps everything
    # wired with no shims.
    horizons_out: dict = {}
    for h in config.HORIZONS:
        per_horizon: dict = {}
        for user, holder in holders.items():
            user_state = (holder or {}).get("state", "watching")
            user_mc_horizon = ((mc_block.get("users") or {}).get(user) or {}).get("horizons", {}).get(h)
            user_targets = (targets_block.get("users") or {}).get(user)

            if not user_mc_horizon or not user_targets:
                logger.warning(f"{user}: missing MC or targets for horizon {h}; skipping")
                continue

            inputs = dict(common_inputs)
            inputs["p_target"] = float(user_mc_horizon["p_target"])
            inputs["p_stop"] = float(user_mc_horizon["p_stop"])
            inputs["ev_normalized"] = float(user_mc_horizon["ev_normalized"])
            inputs["user_state"] = user_state

            breakdown = conviction.evaluate(inputs, horizon_days=h)

            per_horizon[user] = {
                "breakdown": breakdown,
                "targets": {
                    "entry": holder.get("entry"),
                    "target": float(user_targets["target_price"]),
                    "stop": float(user_targets["stop_price"]),
                },
            }
        horizons_out[h] = per_horizon

    # Plain-English thesis paragraph — ticker-level synthesis of
    # regime + catalyst + analyst + short interest + tier sanity + MC
    # stats. Tied to the FIRST user's verdict (typically shared in
    # our two-user model). Surfaced separately via `snap["thesis"]`;
    # main.py promotes the field after verdict returns.
    thesis = _build_thesis(snap, horizons_out)

    return {
        "status": "ok",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizons": horizons_out,
        "thesis": thesis,
    }


def _build_thesis(snap: dict, horizons_out: dict) -> dict:
    """Generate a 3-5 sentence plain-English thesis synthesizing every
    upstream block. Output `{"status": "ok", "text": "..."}` matches
    what the dashboard's `_render_thesis` consumes.

    NO markdown formatting in the text — the dashboard html.escape's
    it, so asterisks pass through literally and would not render bold.
    Keep it conversational research-note quality."""
    ticker = snap.get("ticker", "this ticker")
    regime = snap.get("regime") or {}
    catalyst_block = snap.get("catalyst") or {}
    tier_cls = snap.get("tier_classifier") or {}
    mc = snap.get("monte_carlo") or {}

    parts: list[str] = []

    # 1. Regime read
    state = regime.get("state")
    conf = regime.get("confidence")
    days = regime.get("days_in_regime")
    if state and conf is not None:
        days_phrase = f", {days} sessions" if days else ""
        parts.append(f"{ticker} is in {state} ({conf*100:.0f}% confidence{days_phrase}).")

    # 2. Catalyst proximity
    ne = catalyst_block.get("next_event")
    dist = catalyst_block.get("distance_sessions")
    if ne and dist is not None:
        if dist <= 1:
            parts.append(
                f"{ne['type'].title()} {'today' if dist == 0 else 'tomorrow'} "
                f"({ne['date']}) — Layer-3 catalyst veto active."
            )
        elif dist <= 5:
            parts.append(f"{ne['type'].title()} in {dist} sessions — proximity haircut applies to confidence.")
        elif dist <= 30:
            parts.append(f"{ne['type'].title()} in {dist} sessions, inside the 30d horizon.")
        elif dist <= 60:
            parts.append(f"{ne['type'].title()} in {dist} sessions, outside 30d but inside 60d.")
        else:
            parts.append(f"No catalyst inside the 60d horizon (next event {dist} sessions out).")

    # 3. Historical reactions
    reactions = catalyst_block.get("historical_reactions") or []
    if reactions:
        avg = sum(r["reaction_pct"] for r in reactions) / len(reactions)
        mags = [abs(r["reaction_pct"]) for r in reactions]
        avg_mag = sum(mags) / len(mags)
        parts.append(
            f"Last {len(reactions)} prints averaged {avg:+.1f}% (magnitude {avg_mag:.1f}%) — "
            f"this is the empirical distribution the day-of-event jump bootstraps from."
        )

    # 4. Analyst consensus
    ar = catalyst_block.get("analyst_revisions") or {}
    if ar.get("trend"):
        avg_pt = ar.get("avg_pt")
        if isinstance(avg_pt, (int, float)):
            parts.append(f"Analyst desk: {ar['trend']}; consensus PT ${avg_pt}.")
        else:
            parts.append(f"Analyst desk: {ar['trend']}.")

    # 5. Short interest band (only when it's a real signal)
    si = catalyst_block.get("short_interest") or {}
    spct = si.get("short_percent_of_float")
    trend_pct = si.get("trend_month_over_month_pct")
    if spct is not None:
        if spct >= 0.15:
            band = f"elevated ({spct*100:.1f}% of float, squeeze-candidate territory)"
            parts.append(f"Short interest {band}.")
        elif trend_pct is not None and abs(trend_pct) >= 15:
            direction = "building" if trend_pct > 0 else "easing"
            parts.append(f"Short interest {spct*100:.1f}% of float, {direction} {trend_pct:+.0f}% MoM.")

    # 6. Tier sanity-check (only when mismatched — silent when matched)
    tier_anchor = snap.get("tier_anchor")
    tier_measured = tier_cls.get("measured_tier")
    if tier_anchor and tier_measured and tier_anchor != tier_measured:
        decisive = tier_cls.get("decisive_property", "vol")
        parts.append(
            f"Tier sanity-check flags watchlist {tier_anchor} vs measured {tier_measured} "
            f"({decisive} drives the difference)."
        )

    # 6.5 Fair value premium (only when meaningful — skip the "near
    # fair" middle band where there's no signal worth surfacing).
    fv_block = snap.get("fair_value") or {}
    if fv_block.get("status") == "ok":
        sigmas = fv_block.get("premium_sigmas", 0.0)
        range_mean = fv_block.get("range_mean", 0.0)
        if abs(sigmas) >= 1.0 and range_mean:
            if sigmas >= 2.0:
                phrase = f"current at {sigmas:+.1f}sigma — Layer-3 FV veto FIRES"
            elif sigmas >= 1.0:
                phrase = f"current at {sigmas:+.1f}sigma — modest premium"
            elif sigmas >= -2.0:
                phrase = f"current at {sigmas:+.1f}sigma — meaningfully cheap"
            else:
                phrase = f"current at {sigmas:+.1f}sigma — deeply discounted"
            parts.append(f"Fair value mean ${range_mean:.2f} ({phrase}).")

    # 7. MC stats for the primary horizon (using first user)
    primary_h = config.HORIZONS[0]
    primary_h_users = horizons_out.get(primary_h, {})
    first_user = next(iter(primary_h_users.keys()), None)
    if first_user is not None:
        mc_user_h = ((mc.get("users") or {}).get(first_user) or {}).get("horizons", {}).get(primary_h)
        if mc_user_h:
            p_t = mc_user_h["p_target"]
            p_s = mc_user_h["p_stop"]
            ev = mc_user_h["ev_pct"]
            parts.append(
                f"{primary_h}d Monte Carlo: P(target)={p_t*100:.0f}%, P(stop)={p_s*100:.0f}%, "
                f"EV {ev:+.1f}%."
            )

    if not parts:
        return {"status": "pending", "reason": "insufficient upstream data for thesis"}

    return {"status": "ok", "text": " ".join(parts)}


# ---------- input assembly ----------


def _build_common_inputs(snap: dict) -> dict:
    """Build the per-ticker conviction inputs that don't vary by user
    or horizon. Defaults for not-yet-implemented upstream steps are
    chosen so they produce NO Layer-2 haircut and NO Layer-3 veto -
    i.e., a missing FV / verifier never makes a verdict more
    conservative than the underlying math justifies."""
    regime = snap.get("regime") or {}
    tier_cls = snap.get("tier_classifier") or {}
    catalyst_block = snap.get("catalyst") or {}
    fv_block = snap.get("fair_value") or {}
    av_block = snap.get("analytic_verifier") or {}

    adv = (
        ((tier_cls.get("properties") or {}).get("adv_usd") or {}).get("value")
        or 0.0
    )

    return {
        # Layer-2 haircut inputs
        "mc_pde_p_target_delta_pp": _safe_get_pp_delta(av_block),
        "trajectory_direction_changes": 0,        # requires multi-night history
        "watchlist_tier": snap.get("tier_anchor", "B"),
        "measured_tier": tier_cls.get("measured_tier", snap.get("tier_anchor", "B")),
        "tier_mismatch_consecutive_nights": 0,    # requires multi-night history

        # Layer-3 veto inputs
        "catalyst_distance_sessions": catalyst_block.get("distance_sessions"),
        "regime_state": regime.get("state", "sideways"),
        "regime_confidence": float(regime.get("confidence") or 0.0),
        "fair_value_premium_sigmas": _safe_get_fv_sigmas(fv_block),
        "avg_daily_dollar_volume": float(adv),
    }


def _safe_get_pp_delta(av_block: dict) -> float:
    """Pull the MC<->PDE P(target) delta from analytic_verifier. Default
    0.0 when the step isn't live yet (no agreement penalty applies)."""
    if av_block.get("status") != "ok":
        return 0.0
    return float(av_block.get("p_target_delta_pp") or 0.0)


def _safe_get_fv_sigmas(fv_block: dict) -> float:
    """Pull the price-vs-fair-value sigma displacement from fair_value.
    Default 0.0 when the step isn't live yet (price assumed at FV mean
    -> no Layer-3 veto)."""
    if fv_block.get("status") != "ok":
        return 0.0
    return float(fv_block.get("premium_sigmas") or 0.0)


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
    for h in config.HORIZONS:
        per_h = payload["horizons"].get(h) or {}
        print(f"\n  --- {h}d horizon ---")
        for user, u in per_h.items():
            b = u["breakdown"]
            t = u["targets"]
            score = b["final_score"]
            label = b["verdict_label"]
            reason = b["verdict_reason"]
            print(f"    {user:6}  {label:7}  score={score:.2f}  ({reason})")
            print(f"            target ${t['target']:.2f}  stop ${t['stop']:.2f}"
                  + (f"  entry ${t['entry']:.2f}" if t.get("entry") else ""))
            l1 = b["layer1_edge"]
            l2 = b["layer2_confidence"]
            l3 = b["layer3_vetoes"]
            print(f"            L1 edge={l1['score']:.2f}{' [lottery filter FAIL]' if l1.get('lottery_filter_failed') else ''}"
                  f"  L2 mult={l2['multiplier']:.2f}"
                  f"  L3 fired={[v['name'] for v in l3['fired']] or 'none'}")


def main() -> int:
    from src.pipeline import (
        data as data_step,
        regime as regime_step,
        catalyst as cat_step,
        volatility as vol_step,
        targets as targets_step,
        monte_carlo as mc_step,
    )
    from src import tier_classifier
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Verdict synthesis smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to synthesize")
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
        # Don't unpack here — verdict doesn't need price_levels / daily_path

        try:
            payload = synthesize(ticker, snap, entry)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR synthesizing {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
