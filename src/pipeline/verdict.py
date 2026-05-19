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

Output schema (matches `src/dashboard.py:_render_conviction` and
`_render_action` expectations):

    {
        "status": "ok",
        "fetched_at": ISO timestamp,
        "horizons": {
            30: {
                "users": {
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

    horizons_out: dict = {}
    for h in config.HORIZONS:
        users_out: dict = {}
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

            users_out[user] = {
                "breakdown": breakdown,
                "targets": {
                    "entry": holder.get("entry"),
                    "target": float(user_targets["target_price"]),
                    "stop": float(user_targets["stop_price"]),
                },
            }
        horizons_out[h] = {"users": users_out}

    return {
        "status": "ok",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizons": horizons_out,
    }


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
        users = per_h.get("users") or {}
        print(f"\n  --- {h}d horizon ---")
        for user, u in users.items():
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
