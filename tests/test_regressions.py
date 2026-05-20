"""Regression tests — one per real bug we've ever fixed.

Each test asserts that the SPECIFIC failure mode doesn't return.
The tests are the institutional memory. If someone (or future-me)
refactors the code and accidentally re-introduces a fixed bug, the
test fires.

Each test starts with a comment block:
  - Bug ID (commit short SHA of the fix)
  - One-line description
  - Failure mode the test catches

Run via: python -m unittest tests.test_regressions -v
"""

from __future__ import annotations

import os
import unittest

# Force test mode so tests don't pollute production paths.
os.environ.setdefault("SGC_RUN_MODE", "test")

import numpy as np

from src import config
from src.data_sources import fmp


class TestEarningsJumpAtDistanceZero(unittest.TestCase):
    """Bug fixed in commit 7e78894.
    Description: When earnings was TODAY (distance_sessions=0), the
    earnings-jump overlay silently disappeared because the gating
    condition was `1 <= dist <= max(HORIZONS)`. Verdict for NVDA went
    from SKIP -> WAIT on earnings day because MC ran pure-GBM through
    the most variable day in the horizon.
    Failure mode caught: regression in the gating condition.
    """

    def test_distance_zero_clamps_to_jump_day_one(self):
        from src.pipeline import monte_carlo as mc
        snap = {
            "ticker": "TEST", "tier_anchor": "A",
            "targets": {"status": "ok", "current_price": 100.0,
                "users": {"u": {"target_price": 110.0, "stop_price": 92.0,
                    "source": "vol-scaled-default"}}},
            "regime": {"status": "ok", "annualized_drift_implied": 0.10},
            "volatility": {"status": "ok", "forecast_30d_pct": 0.30},
            "catalyst": {
                "status": "ok", "distance_sessions": 0,
                "next_event": {"type": "earnings", "date": "2026-05-19"},
                "historical_reactions": [{"reaction_pct": -3.0}, {"reaction_pct": 1.0}],
            },
            "data": {"profile": {"price": 100.0}},
        }
        result = mc.simulate("TEST", snap, run_date="2026-05-19")
        self.assertTrue(result["model_components"]["earnings_jump_overlay"],
            "Jump must fire for distance=0 (earnings today AMC)")
        self.assertEqual(result["model_components"]["earnings_jump_day"], 1,
            "When distance=0, jump must clamp to day 1")


class TestBacktestProfilePriceLeak(unittest.TestCase):
    """Bug fixed in commit 40d5957.
    Description: _build_historical_snapshot was passing the CURRENT
    FMP profile (with today's market price) into historical snaps.
    Targets module read profile.price and used today's price for all
    historical as-of evaluations. Backtest results were meaningless.
    Failure mode caught: if anyone re-introduces full_profile as the
    snap's profile without overriding price, this test fails.
    """

    def test_historical_snapshot_overrides_profile_price_with_as_of_close(self):
        from datetime import date
        from src import backtest
        truncated = [
            {"date": "2025-05-28", "close": 119.0, "adj_close": 119.0,
             "high": 120, "low": 117},
            {"date": "2025-05-30", "close": 120.5, "adj_close": 120.5,
             "high": 121, "low": 119},
        ]
        full_profile = {
            "price": 225.32,           # TODAY's price (would-be leak)
            "market_cap": 5.48e12,
            "shares_outstanding": 24.32e9,
            "sector": "Technology",
        }
        snap = backtest._build_historical_snapshot(
            ticker="NVDA",
            as_of=date(2025, 6, 1),
            truncated_prices=truncated,
            full_profile=full_profile,
            full_earnings=[],
            entry={"tier": "A", "holders": {"backtest": {"state": "watching"}}},
        )
        # The historical profile must use the as-of close, NOT today's price.
        historical_price = snap["data"]["profile"]["price"]
        self.assertAlmostEqual(historical_price, 120.5, places=2,
            msg=f"Historical profile.price = ${historical_price}; "
                f"expected $120.50 (as-of close), got today's $225.32 leak")


class TestBacktestRealizedOutcomeIntraday(unittest.TestCase):
    """Bug fixed in commit 40d5957.
    Description: _compute_realized_outcome checked daily CLOSES only.
    A real Trading 212 stop-loss order fires INTRADAY when price
    touches the stop, not at the close. Using closes systematically
    undercounts hits — the same daily-monitoring bias MC's Brownian
    bridge correction now addresses for simulations.
    Failure mode caught: if someone reverts to close-based realized
    detection, this test catches it.
    """

    def test_intraday_low_below_stop_triggers_stop_hit(self):
        from src import backtest
        # Day 2: low = 88 (below stop=90), close = 95 (above stop)
        # Old behavior: stop NOT hit. New behavior: stop hit on day 2.
        future_prices = [
            {"date": "d1", "high": 102, "low": 99, "close": 100, "adj_close": 100},
            {"date": "d2", "high": 100, "low": 88, "close": 95, "adj_close": 95},
        ] + [{"date": f"d{i}", "high": 96, "low": 94, "close": 95, "adj_close": 95}
             for i in range(3, 31)]
        outcome = backtest._compute_realized_outcome(
            future_prices, target_price=120, stop_price=90, horizon_days=20)
        self.assertEqual(outcome["outcome"], "stop",
            "Intraday low 88 < stop 90 must trigger stop-hit, even though close 95 > 90")
        self.assertEqual(outcome["first_passage_day"], 2)

    def test_intraday_high_above_target_triggers_target_hit(self):
        from src import backtest
        future_prices = [
            {"date": "d1", "high": 105, "low": 99, "close": 102, "adj_close": 102},
            {"date": "d2", "high": 125, "low": 100, "close": 110, "adj_close": 110},
        ] + [{"date": f"d{i}", "high": 112, "low": 108, "close": 110, "adj_close": 110}
             for i in range(3, 31)]
        outcome = backtest._compute_realized_outcome(
            future_prices, target_price=120, stop_price=90, horizon_days=20)
        self.assertEqual(outcome["outcome"], "target",
            "Intraday high 125 >= target 120 must trigger target-hit")
        self.assertEqual(outcome["first_passage_day"], 2)


class TestConvictionSchemaDirectUserKey(unittest.TestCase):
    """Bug fixed in commit 1833535.
    Description: I added a "users" wrapper in verdict.synthesize's
    output schema (`horizons[h]["users"][user]`), but the dashboard
    actually reads `horizons[h][user]` directly. The _conviction_
    headline function iterates `next(iter(horizons[h].keys()))` to
    find the first user — with my wrapper this returned "users" not
    "aidy", and dashboard crashed with KeyError: 'breakdown'.
    Failure mode caught: any reintroduction of the wrapper.
    """

    def test_verdict_horizons_keyed_directly_by_user(self):
        from src.pipeline import verdict
        # Synthetic snap that lets verdict.synthesize run
        snap = {
            "ticker": "TEST", "tier_anchor": "A",
            "monte_carlo": {"status": "ok", "users": {
                "aidy": {"horizons": {30: {"p_target": 0.30, "p_stop": 0.40,
                    "ev_normalized": 0.10, "ev_pct": 1.5, "p_target_gbm_only": 0.30,
                    "p_stop_gbm_only": 0.40},
                    60: {"p_target": 0.35, "p_stop": 0.45, "ev_normalized": 0.12,
                         "ev_pct": 1.8, "p_target_gbm_only": 0.35, "p_stop_gbm_only": 0.45}}}}},
            "targets": {"status": "ok",
                "users": {"aidy": {"target_price": 110.0, "stop_price": 92.0,
                    "source": "vol-scaled-default"}}},
            "regime": {"status": "ok", "state": "uptrend_quiet", "confidence": 0.50,
                "days_in_regime": 10},
            "tier_classifier": {"status": "ok", "measured_tier": "A",
                "properties": {"adv_usd": {"value": 1e9}}},
            "catalyst": {"status": "ok", "distance_sessions": None,
                "next_event": None, "historical_reactions": []},
        }
        entry = {"tier": "A", "holders": {"aidy": {"state": "watching"}}}
        result = verdict.synthesize("TEST", snap, entry)
        # Critical: horizons[h] must contain user keys DIRECTLY, not via
        # a "users" wrapper.
        h30_keys = list(result["horizons"][30].keys())
        self.assertIn("aidy", h30_keys,
            f"horizons[30] must contain user key 'aidy' directly; got {h30_keys}")
        self.assertNotIn("users", h30_keys,
            "horizons[30] must NOT contain a 'users' wrapper key — "
            "dashboard's _conviction_headline iterates horizons[h].keys() "
            "directly and would break with a wrapper")
        # And the user entry must have 'breakdown' directly
        self.assertIn("breakdown", result["horizons"][30]["aidy"])


class TestBrownianBridgeFixesDiscretizationBias(unittest.TestCase):
    """Bug fixed in commit 2097769.
    Description: MC's first-passage check used daily closes only,
    systematically UNDERESTIMATING P(stop) by ~5pp vs continuous-time
    PDE. Bridge correction now detects intraday crossings.
    Failure mode caught: if someone disables the bridge correction
    or breaks the formula, the MC-PDE delta blows back up to ~5pp.
    """

    def test_bridge_correction_brings_mc_pde_within_2pp(self):
        from src.pipeline import monte_carlo, analytic_verifier
        snap = {
            "ticker": "TEST", "tier_anchor": "A",
            "targets": {"status": "ok", "current_price": 100.0,
                "users": {"u": {"target_price": 115.0, "stop_price": 92.0,
                    "source": "vol-scaled-default"}}},
            "regime": {"status": "ok", "annualized_drift_implied": 0.10},
            "volatility": {"status": "ok", "forecast_30d_pct": 0.35},
            "catalyst": {"status": "ok"},
            "data": {"profile": {"price": 100.0}},
            "tier_classifier": {"status": "ok", "measured_tier": "A",
                "properties": {"adv_usd": {"value": 1e9}}},
        }
        snap["monte_carlo"] = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        result = analytic_verifier.verify("TEST", snap)
        h30 = result["horizons"][30]
        # Delta should be < 2pp on all three metrics
        self.assertLess(h30["delta_p_target"] * 100, 2.0,
            f"P(target) delta = {h30['delta_p_target']*100:.2f}pp; "
            f"bridge correction should keep this < 2pp (was ~3.65pp pre-fix)")
        self.assertLess(h30["delta_p_stop"] * 100, 2.0,
            f"P(stop) delta = {h30['delta_p_stop']*100:.2f}pp; "
            f"bridge correction should keep this < 2pp (was ~5.90pp pre-fix)")


class TestAPIKeyRedaction(unittest.TestCase):
    """Bug fixed in commit 36c84be.
    Description: requests.HTTPError's default message includes the
    full request URL, which contains apikey query param. Errors were
    leaking the FMP API key into logs, exception traces, and chat
    transcripts.
    Failure mode caught: if the redact() helper breaks or stops being
    applied, error messages would re-leak the key.
    """

    def test_redact_replaces_apikey_in_url(self):
        from src.data_sources.fmp import redact
        url_with_key = "https://financialmodelingprep.com/stable/profile?apikey=secret123&symbol=NVDA"
        redacted = redact(url_with_key)
        self.assertNotIn("secret123", redacted)
        self.assertIn("apikey=REDACTED", redacted)

    def test_redact_handles_mixed_case_and_multiple(self):
        from src.data_sources.fmp import redact
        text = "url1=...apikey=abc123 url2=...APIKEY=def456..."
        redacted = redact(text)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("def456", redacted)


class TestCacheTTL(unittest.TestCase):
    """Bug fixed in commit fabb0fa.
    Description: data.fetch() had no TTL on its cache. Nightly cron
    would serve yesterday's prices forever because the cache file
    always exists after the first run.
    Failure mode caught: if someone removes the TTL check, cache
    would always be returned regardless of age.
    """

    def test_cache_freshness_checker_rejects_stale(self):
        from datetime import datetime, timedelta, timezone
        from src.pipeline import data
        stale = {
            "ticker": "TEST",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds"),
        }
        self.assertFalse(data._cache_is_fresh(stale),
            "24h-old cache must be classified stale (TTL is 4h)")

    def test_cache_freshness_accepts_fresh(self):
        from datetime import datetime, timezone
        from src.pipeline import data
        fresh = {
            "ticker": "TEST",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.assertTrue(data._cache_is_fresh(fresh),
            "Just-fetched cache must be classified fresh")


class TestFMPRetryRespectsPermanentErrors(unittest.TestCase):
    """Bug guarded in commit 9b574c3.
    Description: FMP retry must NOT retry 402 (plan-gated) or 403
    (auth) — those are permanent and retry just burns quota.
    Failure mode caught: if someone adds 402/403 to retryable codes.
    """

    def test_402_not_retryable(self):
        self.assertNotIn(402, fmp._RETRY_STATUS_CODES,
            "402 (plan-gated) must NOT be retryable — would waste quota")

    def test_403_not_retryable(self):
        self.assertNotIn(403, fmp._RETRY_STATUS_CODES,
            "403 (auth/endpoint wrong) must NOT be retryable")

    def test_429_is_retryable(self):
        self.assertIn(429, fmp._RETRY_STATUS_CODES,
            "429 (rate limit) MUST be retryable")


class TestDataDepthHaircut(unittest.TestCase):
    """Layer-2 data-depth haircut surfaces the cost of thin price history
    explicitly in the conviction breakdown rather than letting it leak
    through indirect haircuts (MC-PDE disagreement, trajectory
    instability). Per CLAUDE.md bug bar: information the system has must
    be surfaced where a competent swing trader would expect to find it."""

    def _evaluate_with_bars(self, bar_count):
        from src import conviction
        inputs = {
            "p_target": 0.35, "p_stop": 0.30, "ev_normalized": 0.10,
            "user_state": "watching",
            "mc_pde_p_target_delta_pp": 1.0,
            "trajectory_direction_changes": 0,
            "watchlist_tier": "A", "measured_tier": "A",
            "tier_mismatch_consecutive_nights": 0,
            "price_bar_count": bar_count,
            "catalyst_distance_sessions": None,
            "regime_state": "uptrend_quiet", "regime_confidence": 0.50,
            "fair_value_premium_sigmas": 0.0,
            "avg_daily_dollar_volume": 1e9,
        }
        return conviction.evaluate(inputs, horizon_days=30)

    def test_mature_history_no_haircut(self):
        result = self._evaluate_with_bars(2000)
        depth = next(h for h in result["layer2_confidence"]["haircuts"] if h["name"] == "Data depth")
        self.assertEqual(depth["haircut"], 0.0, "≥1500 bars must be the no-haircut reference")
        self.assertTrue(depth["passed"])

    def test_moderate_history_10pct_haircut(self):
        result = self._evaluate_with_bars(1100)
        depth = next(h for h in result["layer2_confidence"]["haircuts"] if h["name"] == "Data depth")
        self.assertAlmostEqual(depth["haircut"], 0.10, msg="750-1499 bars must apply 10% haircut")

    def test_thin_history_25pct_haircut(self):
        result = self._evaluate_with_bars(500)
        depth = next(h for h in result["layer2_confidence"]["haircuts"] if h["name"] == "Data depth")
        self.assertAlmostEqual(depth["haircut"], 0.25, msg="250-749 bars must apply 25% haircut")

    def test_haircut_compounds_into_confidence_multiplier(self):
        # 25% data-depth haircut alone should drop the L2 multiplier to ~0.75
        # (all other haircuts at 0). Validates the haircut actually
        # affects the confidence number, not just the report metadata.
        result = self._evaluate_with_bars(500)
        self.assertAlmostEqual(result["layer2_confidence"]["multiplier"], 0.75, places=4)


if __name__ == "__main__":
    unittest.main()
