"""Render the dashboard with rich synthetic data — for design iteration.

Usage:
    python tools/preview_dashboard.py [output_path.html]

Produces a fully-populated dashboard (every panel rendered, not "pending")
using realistic synthetic data for NVDA / AMAT / IONQ. Useful for
iterating on the visual design without needing FMP + a fully-built
pipeline behind it.

The synthetic conviction breakdowns are computed by the production
src.conviction module — the layout reflects exactly what real production
output will look like once all pipeline steps are live.

The synthetic regime/catalyst/vol/fair-value/MC/PDE numbers below are
illustrative only; they're chosen to exercise each panel and demonstrate
the three-layer-conviction interactions (catalyst veto on NVDA 60d,
WAIT-vs-HOLD divergence on AMAT across users, regime + lottery + double
haircut on IONQ).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root on sys.path so this can be run from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import conviction, dashboard


def build_payload() -> dict:
    """Construct a rich synthetic payload across NVDA / AMAT / IONQ."""

    nvda_snap = _build_nvda()
    amat_snap = _build_amat()
    ionq_snap = _build_ionq()

    return {
        "run_date": "2026-05-18",
        "run_started_at": "2026-05-18T03:00:00+00:00",
        "run_finished_at": "2026-05-18T03:02:18+00:00",
        "market_context": {"regime": "NEUTRAL", "vix": "18.4"},
        "watchlist": {
            "NVDA": {
                "tier": "A",
                "notes": "AI infra leader",
                "holders": {
                    "aidy":  {"state": "watching"},
                    "jesse": {"state": "watching"},
                },
            },
            "AMAT": {
                "tier": "B",
                "notes": "Semi-cap-equipment, AI-capex beneficiary",
                "holders": {
                    "aidy":  {"state": "watching"},
                    "jesse": {"state": "entered", "entry": 390.00, "target": 475.00, "stop": 360.00, "entered_date": "2026-04-22"},
                },
            },
            "IONQ": {
                "tier": "C",
                "notes": "Quantum pure-play, narrative-driven fat-tailed catalyst",
                "holders": {
                    "aidy":  {"state": "watching"},
                    "jesse": {"state": "watching"},
                },
            },
        },
        "tickers": {
            "NVDA": nvda_snap,
            "AMAT": amat_snap,
            "IONQ": ionq_snap,
        },
        "errors": [],
        "data_quality_warnings": [
            "IONQ: 8 single-day moves > 20% in last 24 months (max 41%). Real volatility for quantum names — included in calibration.",
            "AMAT: 1 single-day move > 22% (May 2026 guidance miss). Real event. Included in vol estimate.",
        ],
        "backtest": {"status": "pending"},
    }


# ---------- NVDA: Tier A, strong edge, dual-horizon divergence via catalyst ----------


def _build_nvda() -> dict:
    inputs = {
        "p_target": 0.65, "p_stop": 0.12, "ev_normalized": 2.2,
        "mc_pde_p_target_delta_pp": 1.2,
        "trajectory_direction_changes": 1,
        "watchlist_tier": "A", "measured_tier": "A", "tier_mismatch_consecutive_nights": 0,
        "catalyst_distance_sessions": 35,
        "regime_state": "uptrend_quiet", "regime_confidence": 0.84,
        "fair_value_premium_sigmas": -0.2,
        "avg_daily_dollar_volume": 28e9,
    }
    breakdowns = {}
    for h in (30, 60):
        for user in ("aidy", "jesse"):
            br = conviction.evaluate({**inputs, "user_state": "watching"}, h)
            breakdowns.setdefault(h, {})[user] = {
                "breakdown": br,
                "targets": {
                    "entry": "$216 — $220",
                    "target": 240.00,
                    "stop": 198.00,
                    "copy_paste": "bought NVDA at 218.00" if br["verdict_label"] in ("ENTER",) else "watch NVDA",
                },
            }

    # Daily expected path — illustrative
    days = []
    base = 225.32
    for i in range(20):
        d = ["May 19","May 20","May 21","May 22","May 23","May 26","May 27","May 28","May 29","May 30","Jun 02","Jun 03","Jun 04","Jun 05","Jun 06","Jun 09","Jun 10","Jun 11","Jun 12","Jun 13"][i]
        price = base + (i - 8) * 0.4 + (1.2 if 4 < i < 9 else 0)
        zone = "rally" if i < 5 else ("dip" if 9 < i < 14 else "")
        days.append({"day": i + 1, "date": d, "median_price": round(price, 2), "zone": zone})

    return {
        "ticker": "NVDA", "run_date": "2026-05-18", "as_of": "2026-05-16", "tier_anchor": "A",
        "data": _ok_data_block(profile={"sector": "Technology", "industry": "Semiconductors", "market_cap": 4_800_000_000_000}, bar_count=1258),
        "price_levels": {
            "status": "ok",
            "current_price": 225.32,
            "rsi": 65,
            "high_60d": 238.40,
            "horizons": {
                30: {
                    "dip":   {"price": 217.20, "date_range": "May 28 — Jun 06"},
                    "rally": {"price": 240.95, "date_range": "Jun 02 — Jun 11"},
                },
                60: {
                    "dip":   {"price": 212.50, "date_range": "Jun 04 — Jun 18"},
                    "rally": {"price": 250.70, "date_range": "Jun 11 — Jun 25"},
                },
            },
        },
        "thesis": {
            "status": "ok",
            "text": (
                "NVDA sits in an uptrend-quiet regime (84% confidence) with vol forecast trending down "
                "(31% → 30% annualized over 30d). Earnings on June 26 — 35 trading sessions out, beyond "
                "the 30d window but inside 60d. Options market implies ±6.1% earnings-day move; last "
                "four reactions averaged +7.5%. Fair value $215–$248 vs $225 current — fairly valued. "
                "Monte Carlo and Fokker-Planck PDE agree within 1.2pp on P(target); §4.2 classifier confirms "
                "Tier A behavior across all four properties. Conviction trajectory has been rising 4 of "
                "the last 5 nights."
            ),
        },
        "conviction": {"status": "ok", "horizons": breakdowns},
        "catalyst": {
            "status": "ok",
            "distance_sessions": 35,
            "next_event": {"type": "Earnings (Q1 FY27)", "date": "2026-06-26"},
            "options_implied_move_pct": 0.061,
            "historical_reactions": [
                {"date": "2026-02-22", "type": "earnings", "reaction_pct": 8.5},
                {"date": "2025-11-19", "type": "earnings", "reaction_pct": -2.1},
                {"date": "2025-08-27", "type": "earnings", "reaction_pct": 14.1},
                {"date": "2025-05-21", "type": "earnings", "reaction_pct": 9.4},
            ],
            "news_bullets": [
                "Goldman: PT raised to $290 citing Blackwell ramp visibility (2026-05-15)",
                "Morgan Stanley: PT $295 with sovereign-AI tailwind narrative (2026-05-13)",
                "CFO at JPM conference confirmed Blackwell shipments on-schedule (2026-05-12)",
            ],
            "analyst_revisions": {"count": 3, "avg_pt": 290, "trend": "↑ rising"},
            "engine_recommendation": (
                "Earnings 35 sessions out — outside 30d horizon (clean ENTER possible on 30d), inside 60d "
                "(Layer-3 catalyst veto fires on 60d). Recommend ENTER on the 30d horizon ahead of next "
                "Tuesday's Goldman conference; defer 60d entry until after the June 26 print to avoid "
                "binary-event risk on a position you'd otherwise hold through it."
            ),
        },
        "regime": {
            "status": "ok",
            "state": "uptrend_quiet",
            "confidence": 0.84,
            "days_in_regime": 28,
            "annualized_drift_implied": 0.15,
            "narrative": (
                "Price action confirms the uptrend regime: 23/28 daily closes above the 20-day moving "
                "average, daily realized vol stable at 1.9% (within band for uptrend-quiet), and no "
                "regime-state-change probability spikes in last 14 sessions. Drift parameter fed to MC: "
                "+15% annualized."
            ),
            "veto_active": False,
        },
        "volatility": {
            "status": "ok",
            "current_realized_pct": 0.31,
            "forecast_30d_pct": 0.30,
            "forecast_60d_pct": 0.32,
            "confidence_band": "tight (Tier A)",
        },
        "fair_value": {
            "status": "ok",
            "range_low": 215.00, "range_mean": 232.00, "range_high": 248.00,
            "current_price": 225.32,
            "premium_sigmas": -0.20,
            "methods": ["forward P/E vs semi peers", "DCF (10y, 4% terminal)", "EV/sales 2-yr forward"],
        },
        "cross_check": {
            "status": "ok",
            "horizons": {
                30: {
                    "mc_p_target": 0.640, "pde_p_target": 0.652, "delta_p_target": 0.012,
                    "mc_p_stop": 0.120, "pde_p_stop": 0.118, "delta_p_stop": 0.002,
                    "mc_ev": 0.058, "pde_ev": 0.061, "delta_ev": 0.003,
                    "agree_p_target": True, "agree_p_stop": True, "agree_ev": True,
                },
                60: {
                    "mc_p_target": 0.710, "pde_p_target": 0.731, "delta_p_target": 0.021,
                    "mc_p_stop": 0.180, "pde_p_stop": 0.172, "delta_p_stop": 0.008,
                    "mc_ev": 0.094, "pde_ev": 0.098, "delta_ev": 0.004,
                    "agree_p_target": True, "agree_p_stop": True, "agree_ev": True,
                },
            },
        },
        "tier_classifier": {
            "status": "ok",
            "measured_tier": "A",
            "decisive_property": "vol_annualized",
            "properties": {
                "vol_annualized": {"value": 0.31, "tier": "A"},
                "vol_of_vol": {"value": 0.12, "tier": "A"},
                "adv_usd": {"value": 28e9, "tier": "A"},
                "history_days": {"value": 1258, "tier": "A"},
            },
            "comparison": {"anchor": "A", "measured": "A", "agreement": True, "direction": "match"},
        },
        "trajectory": {
            "status": "ok",
            "nightly_scores": [0.62, 0.61, 0.64, 0.66, 0.65, 0.67, 0.69, 0.68, 0.70, 0.71, 0.70, 0.72, 0.71, 0.73, 0.74, 0.74, 0.73, 0.75, 0.74, 0.74],
            "annotation": "rising",
            "summary": "Conviction rising 4 of last 5 nights — durable signal. No haircut.",
        },
        "verdict": {
            "aidy":  {"status": "ok", "primary_label": breakdowns[30]["aidy"]["breakdown"]["verdict_label"]},
            "jesse": {"status": "ok", "primary_label": breakdowns[30]["jesse"]["breakdown"]["verdict_label"]},
        },
        "daily_path": {"status": "ok", "days": days},
    }


# ---------- AMAT: Tier B, mixed user-states (HOLD demo vs WAIT demo) ----------


def _build_amat() -> dict:
    base_inputs = {
        "p_target": 0.58, "p_stop": 0.18, "ev_normalized": 1.5,
        "mc_pde_p_target_delta_pp": 1.4,
        "trajectory_direction_changes": 1,
        "watchlist_tier": "B", "measured_tier": "B", "tier_mismatch_consecutive_nights": 0,
        "catalyst_distance_sessions": 45,
        "regime_state": "uptrend_noisy", "regime_confidence": 0.72,
        "fair_value_premium_sigmas": 0.5,
        "avg_daily_dollar_volume": 9e8,
    }
    breakdowns = {}
    for h in (30, 60):
        for user, state in (("aidy", "watching"), ("jesse", "entered")):
            br = conviction.evaluate({**base_inputs, "user_state": state}, h)
            if state == "entered":
                targets = {
                    "current_position_entry": 390.00,
                    "target": 475.00,
                    "stop": 360.00,
                    "copy_paste": "sold AMAT at 437.00",
                }
            else:
                targets = {
                    "entry": "$425 — $432",
                    "target": 475.00,
                    "stop": 405.00,
                    "copy_paste": "watch AMAT",
                }
            breakdowns.setdefault(h, {})[user] = {"breakdown": br, "targets": targets}

    return {
        "ticker": "AMAT", "run_date": "2026-05-18", "as_of": "2026-05-16", "tier_anchor": "B",
        "data": _ok_data_block(profile={"sector": "Technology", "industry": "Semiconductor Equipment", "market_cap": 172_000_000_000}, bar_count=1258),
        "price_levels": {
            "status": "ok",
            "current_price": 436.62,
            "rsi": 62,
            "high_60d": 478.20,
            "horizons": {
                30: {
                    "dip":   {"price": 414.30, "date_range": "May 29 — Jun 09"},
                    "rally": {"price": 467.20, "date_range": "Jun 03 — Jun 13"},
                },
                60: {
                    "dip":   {"price": 402.10, "date_range": "Jun 05 — Jun 19"},
                    "rally": {"price": 488.40, "date_range": "Jun 12 — Jun 26"},
                },
            },
        },
        "thesis": {
            "status": "ok",
            "text": (
                "AMAT in uptrend-noisy regime (72% confidence) — riding the AI-capex theme but with "
                "elevated day-to-day volatility (42% annualized, 0.18 vol-of-vol). Earnings 45 sessions "
                "out — outside both horizons, no catalyst veto. Edge is moderate (P(target)=58%, EV=1.5× "
                "risk) — conviction score 0.56 sits in the WAIT band for watching state but supports HOLD "
                "for entered. Fair value $400–$485, current $436 — slight premium (+0.5σ) but within range. "
                "MC↔PDE within 1.4pp; §4.2 confirms Tier B behavior. Trajectory stable."
            ),
        },
        "conviction": {"status": "ok", "horizons": breakdowns},
        "catalyst": {
            "status": "ok",
            "distance_sessions": 45,
            "next_event": {"type": "Earnings (Q2 FY26)", "date": "2026-08-13"},
            "options_implied_move_pct": 0.052,
            "historical_reactions": [
                {"date": "2026-02-13", "type": "earnings", "reaction_pct": 3.2},
                {"date": "2025-11-14", "type": "earnings", "reaction_pct": -5.8},
                {"date": "2025-08-15", "type": "earnings", "reaction_pct": 7.1},
                {"date": "2025-05-16", "type": "earnings", "reaction_pct": 1.4},
            ],
            "news_bullets": [
                "Citi reiterated Buy with $510 PT, citing TSMC capex acceleration (2026-05-14)",
                "Memory equipment demand inflection — Bernstein note flagged AMAT well-positioned (2026-05-09)",
                "China export-control overhang reportedly easing; smallcap semicap names ripped 8% on news (2026-05-06)",
            ],
            "analyst_revisions": {"count": 2, "avg_pt": 502, "trend": "→ stable"},
            "engine_recommendation": (
                "No binary catalyst within either horizon. Conviction at 0.56 — between the 0.55 WAIT floor "
                "and 0.70 ENTER threshold — so watchers should WAIT for either edge to expand or a clearer "
                "regime call. Entered (Jesse): HOLD, thesis intact, stop ($360) provides 17% downside "
                "protection from current price."
            ),
        },
        "regime": {
            "status": "ok",
            "state": "uptrend_noisy",
            "confidence": 0.72,
            "days_in_regime": 19,
            "annualized_drift_implied": 0.18,
            "narrative": (
                "Trend up but choppier than NVDA — daily realized vol oscillating between 1.8% and 3.2% "
                "(consistent with uptrend-noisy classification). Regime confidence below the 70% strict "
                "downtrend-veto threshold but firmly above the 50% fade-into-sideways risk. Drift "
                "modulation only — no Layer-3 veto."
            ),
            "veto_active": False,
        },
        "volatility": {
            "status": "ok",
            "current_realized_pct": 0.42,
            "forecast_30d_pct": 0.41,
            "forecast_60d_pct": 0.43,
            "confidence_band": "moderate (Tier B widened)",
        },
        "fair_value": {
            "status": "ok",
            "range_low": 400.00, "range_mean": 442.00, "range_high": 485.00,
            "current_price": 436.62,
            "premium_sigmas": 0.50,
            "methods": ["forward P/E vs semi-cap peers", "DCF (10y, 3.5% terminal)", "EV/EBITDA vs LRCX/KLAC"],
        },
        "cross_check": {
            "status": "ok",
            "horizons": {
                30: {
                    "mc_p_target": 0.580, "pde_p_target": 0.594, "delta_p_target": 0.014,
                    "mc_p_stop": 0.180, "pde_p_stop": 0.175, "delta_p_stop": 0.005,
                    "mc_ev": 0.041, "pde_ev": 0.044, "delta_ev": 0.003,
                    "agree_p_target": True, "agree_p_stop": True, "agree_ev": True,
                },
                60: {
                    "mc_p_target": 0.640, "pde_p_target": 0.652, "delta_p_target": 0.012,
                    "mc_p_stop": 0.220, "pde_p_stop": 0.214, "delta_p_stop": 0.006,
                    "mc_ev": 0.072, "pde_ev": 0.076, "delta_ev": 0.004,
                    "agree_p_target": True, "agree_p_stop": True, "agree_ev": True,
                },
            },
        },
        "tier_classifier": {
            "status": "ok",
            "measured_tier": "B",
            "decisive_property": "vol_annualized",
            "properties": {
                "vol_annualized": {"value": 0.42, "tier": "B"},
                "vol_of_vol": {"value": 0.18, "tier": "B"},
                "adv_usd": {"value": 9e8, "tier": "A"},
                "history_days": {"value": 1258, "tier": "A"},
            },
            "comparison": {"anchor": "B", "measured": "B", "agreement": True, "direction": "match"},
        },
        "trajectory": {
            "status": "ok",
            "nightly_scores": [0.51, 0.53, 0.52, 0.54, 0.55, 0.56, 0.54, 0.55, 0.56, 0.57, 0.56, 0.55, 0.56, 0.55, 0.56, 0.57, 0.56, 0.56, 0.55, 0.56],
            "annotation": "stable",
            "summary": "Conviction holding steady around 0.55–0.57 — stable signal but consistently below ENTER threshold.",
        },
        "verdict": {
            "aidy":  {"status": "ok", "primary_label": breakdowns[30]["aidy"]["breakdown"]["verdict_label"]},
            "jesse": {"status": "ok", "primary_label": breakdowns[30]["jesse"]["breakdown"]["verdict_label"]},
        },
        "daily_path": {"status": "ok", "days": _synthetic_daily_path(base_price=436.62, n=20)},
    }


# ---------- IONQ: Tier C, regime veto + double Layer-2 haircut ----------


def _build_ionq() -> dict:
    inputs = {
        "p_target": 0.35, "p_stop": 0.32, "ev_normalized": 0.2,
        "mc_pde_p_target_delta_pp": 6.5,                # triggers low-band MC↔PDE haircut (30%)
        "trajectory_direction_changes": 4,              # triggers trajectory haircut (10%)
        "watchlist_tier": "C", "measured_tier": "C", "tier_mismatch_consecutive_nights": 0,
        "catalyst_distance_sessions": None,
        "regime_state": "downtrend", "regime_confidence": 0.78,   # triggers regime veto
        "fair_value_premium_sigmas": -0.8,
        "avg_daily_dollar_volume": 3e8,
        "user_state": "watching",
    }
    breakdowns = {}
    for h in (30, 60):
        for user in ("aidy", "jesse"):
            br = conviction.evaluate(inputs, h)
            breakdowns.setdefault(h, {})[user] = {
                "breakdown": br,
                "targets": {
                    "entry": "— wait for reversal (RSI > 45 and 20d return positive)",
                    "copy_paste": "watch IONQ",
                },
            }

    return {
        "ticker": "IONQ", "run_date": "2026-05-18", "as_of": "2026-05-16", "tier_anchor": "C",
        "data": _ok_data_block(profile={"sector": "Technology", "industry": "Quantum Computing", "market_cap": 7_500_000_000}, bar_count=1100, has_warn=True),
        "price_levels": {
            "status": "ok",
            "current_price": 51.95,
            "rsi": 31,
            "high_60d": 75.40,
            "horizons": {
                30: {
                    "dip":   {"price": 42.30, "date_range": "Jun 02 — Jun 13"},
                    "rally": {"price": 62.10, "date_range": "May 26 — Jun 06"},
                },
                60: {
                    "dip":   {"price": 38.50, "date_range": "Jun 12 — Jun 26"},
                    "rally": {"price": 68.40, "date_range": "May 28 — Jun 11"},
                },
            },
        },
        "thesis": {
            "status": "ok",
            "text": (
                "IONQ in confirmed downtrend (78% confidence, 22 sessions in regime), price 31% below "
                "60-day peak, RSI 31 — Layer-3 regime veto fires on both 30d and 60d for watching state. "
                "Edge thin to begin with (P(target)=35%, P(stop)=32%; EV barely positive) and additionally "
                "haircut 30% for MC↔PDE disagreement (Δ 6.5pp on P(target) — model strained by recent "
                "extreme moves) and 10% for trajectory flip-flopping (4 direction changes in last 5 nights). "
                "Final conviction 0.13 — well below the 0.55 WAIT floor. SKIP regardless of regime veto. "
                "Tier C low-liquidity warning visible by default."
            ),
        },
        "conviction": {"status": "ok", "horizons": breakdowns},
        "catalyst": {
            "status": "ok",
            "distance_sessions": None,
            "next_event": None,
            "options_implied_move_pct": None,
            "historical_reactions": [
                {"date": "2026-02-26", "type": "earnings", "reaction_pct": -18.2},
                {"date": "2025-11-12", "type": "earnings", "reaction_pct": 41.0},
                {"date": "2025-08-07", "type": "earnings", "reaction_pct": -11.4},
                {"date": "2025-05-15", "type": "earnings", "reaction_pct": 22.8},
            ],
            "news_bullets": [
                "IBM quantum-roadmap update reportedly accelerated; pulled $ out of IONQ as relative momentum trade (2026-05-12)",
                "Goldman cut PT to $48 from $62 citing competitive pressure (2026-05-09)",
                "Insider selling: CFO sold $2.1M, CTO sold $0.8M (2026-05-06)",
            ],
            "analyst_revisions": {"count": 2, "avg_pt": 51, "trend": "↓ falling"},
            "engine_recommendation": (
                "No scheduled binary catalyst, but qualitative news flow is meaningfully negative: "
                "Goldman PT cut + insider selling + competitive narrative pressure. Combined with the "
                "downtrend regime override, the engine recommends SKIP for both Aidy and Jesse. Revisit "
                "after either: (a) regime detector flips to sideways or uptrend, or (b) price retakes "
                "the 20-day moving average with rising volume."
            ),
        },
        "regime": {
            "status": "ok",
            "state": "downtrend",
            "confidence": 0.78,
            "days_in_regime": 22,
            "annualized_drift_implied": -0.20,
            "narrative": (
                "Stock is in a sustained downtrend with no sign of bottoming — 31% below its 60-day "
                "peak, price has weakened over the past month, momentum (RSI 31) remains weak. "
                "Layer-3 ENTER veto fires for watching state. For entry: don't try to catch a falling "
                "knife — wait for reversal signals (RSI recovering above 45, 20-day momentum turning "
                "positive, volume picking up on green days)."
            ),
            "veto_active": True,
        },
        "volatility": {
            "status": "ok",
            "current_realized_pct": 0.95,
            "forecast_30d_pct": 0.92,
            "forecast_60d_pct": 0.88,
            "confidence_band": "wide (Tier C — fat-tailed priors)",
        },
        "fair_value": {
            "status": "ok",
            "range_low": 35.00, "range_mean": 58.00, "range_high": 85.00,
            "current_price": 51.95,
            "premium_sigmas": -0.80,
            "methods": ["narrative-DCF (high-uncertainty)", "EV/qubit-capacity vs peers", "private-market comps"],
        },
        "cross_check": {
            "status": "ok",
            "horizons": {
                30: {
                    "mc_p_target": 0.350, "pde_p_target": 0.285, "delta_p_target": 0.065,
                    "mc_p_stop": 0.320, "pde_p_stop": 0.355, "delta_p_stop": 0.035,
                    "mc_ev": 0.005, "pde_ev": -0.012, "delta_ev": 0.017,
                    "agree_p_target": False, "agree_p_stop": True, "agree_ev": False,
                },
                60: {
                    "mc_p_target": 0.410, "pde_p_target": 0.342, "delta_p_target": 0.068,
                    "mc_p_stop": 0.360, "pde_p_stop": 0.395, "delta_p_stop": 0.035,
                    "mc_ev": 0.012, "pde_ev": -0.018, "delta_ev": 0.030,
                    "agree_p_target": False, "agree_p_stop": True, "agree_ev": False,
                },
            },
        },
        "tier_classifier": {
            "status": "ok",
            "measured_tier": "C",
            "decisive_property": "vol_annualized",
            "properties": {
                "vol_annualized": {"value": 0.95, "tier": "C"},
                "vol_of_vol": {"value": 0.42, "tier": "C"},
                "adv_usd": {"value": 3e8, "tier": "B"},
                "history_days": {"value": 1100, "tier": "A"},
            },
            "comparison": {"anchor": "C", "measured": "C", "agreement": True, "direction": "match"},
        },
        "trajectory": {
            "status": "ok",
            "nightly_scores": [0.42, 0.38, 0.41, 0.35, 0.29, 0.33, 0.28, 0.21, 0.25, 0.19, 0.22, 0.16, 0.18, 0.14, 0.15, 0.12, 0.14, 0.11, 0.13, 0.13],
            "annotation": "decaying",
            "summary": "Conviction collapsing from 0.42 to 0.13 over last 20 nights. Decaying-trend regime confirmed.",
        },
        "verdict": {
            "aidy":  {"status": "ok", "primary_label": "SKIP"},
            "jesse": {"status": "ok", "primary_label": "SKIP"},
        },
        "daily_path": {"status": "ok", "days": _synthetic_daily_path(base_price=51.95, n=20, drift_pct_per_day=-0.6)},
    }


# ---------- shared helpers ----------


def _ok_data_block(profile: dict, bar_count: int, has_warn: bool = False) -> dict:
    sanity = {
        "freshness":    {"status": "ok", "message": "last bar 0 trading day(s) old (threshold 5)"},
        "completeness": {"status": "ok", "message": f"{bar_count} bars, 0 missing of {bar_count} expected"},
        "split_div":    {"status": "warn" if has_warn else "ok", "message": "1 bar with |return| > 30% — review for unadjusted action" if has_warn else "no implausibly large single-day moves"},
        "volume":       {"status": "ok", "message": "0 zero-volume bar(s)"},
        "overall":      "warn" if has_warn else "ok",
    }
    return {"status": "ok", "sanity": sanity, "profile": profile, "bar_count": bar_count}


def _synthetic_daily_path(base_price: float, n: int, drift_pct_per_day: float = 0.0) -> list[dict]:
    days = []
    date_labels = ["May 19","May 20","May 21","May 22","May 23","May 26","May 27","May 28","May 29","May 30","Jun 02","Jun 03","Jun 04","Jun 05","Jun 06","Jun 09","Jun 10","Jun 11","Jun 12","Jun 13"]
    for i in range(min(n, len(date_labels))):
        price = base_price * (1 + (drift_pct_per_day / 100.0) * i)
        zone = "rally" if i < 5 else ("dip" if 9 < i < 14 else "")
        days.append({"day": i + 1, "date": date_labels[i], "median_price": round(price, 2), "zone": zone})
    return days


# ---------- entrypoint ----------


def main() -> int:
    payload = build_payload()
    html = dashboard.render(payload)
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (REPO_ROOT / "dashboard_preview.html")
    out_path.write_text(html)
    print(f"rendered {len(html):,} bytes → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
