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


class TestBacktestIntradayFirstPassage(unittest.TestCase):
    """The backtest must use intraday high/low — not daily closes — when
    scoring whether a target or stop was actually hit. A Trading 212
    stop-loss order fires the moment price TOUCHES the stop intraday,
    not at the close. Using closes would systematically undercount
    stop-hits (and target-hits).
    Failure mode caught: anyone simplifying _first_passage to use only
    closes will break this test.
    """

    def test_intraday_low_below_stop_triggers_stop_hit(self):
        from src import backtest
        # Day 2: low = 88 (below stop=90), close = 95 (above stop)
        # A close-only check would MISS this. Intraday must catch it.
        window = [
            {"date": "d1", "high": 102, "low": 99, "close": 100, "adj_close": 100},
            {"date": "d2", "high": 100, "low": 88, "close": 95, "adj_close": 95},
        ] + [{"date": f"d{i}", "high": 96, "low": 94, "close": 95, "adj_close": 95}
             for i in range(3, 21)]
        outcome = backtest._first_passage(window, target_price=120, stop_price=90)
        self.assertEqual(outcome["outcome"], "stop",
            "Intraday low 88 < stop 90 must trigger stop-hit, even though close 95 > 90")
        self.assertTrue(outcome["stop_hit"])
        self.assertEqual(outcome["first_passage_day"], 2)

    def test_intraday_high_above_target_triggers_target_hit(self):
        from src import backtest
        window = [
            {"date": "d1", "high": 105, "low": 99, "close": 102, "adj_close": 102},
            {"date": "d2", "high": 125, "low": 100, "close": 110, "adj_close": 110},
        ] + [{"date": f"d{i}", "high": 112, "low": 108, "close": 110, "adj_close": 110}
             for i in range(3, 21)]
        outcome = backtest._first_passage(window, target_price=120, stop_price=90)
        self.assertEqual(outcome["outcome"], "target",
            "Intraday high 125 >= target 120 must trigger target-hit")
        self.assertTrue(outcome["target_hit"])
        self.assertEqual(outcome["first_passage_day"], 2)


class TestBacktestDipAndRallyChecks(unittest.TestCase):
    """The backtest must verify BOTH the dip prediction (did the daily
    LOW touch the predicted dip level?) AND the rally prediction (did
    the daily HIGH touch the predicted rally level?) — that's what
    'check both dips and rallies' means. A symmetric check on the MC
    output, independent of any per-user verdict.
    """

    def test_dip_hit_detects_intraday_low_touching_level(self):
        from src import backtest
        # Predicted dip = 95. The window has a day where low touches 94.
        window = [
            {"high": 102, "low": 98, "close": 100, "adj_close": 100},
            {"high": 100, "low": 94, "close": 99, "adj_close": 99},
            {"high": 101, "low": 97, "close": 100, "adj_close": 100},
        ]
        self.assertTrue(backtest._touched_below(window, 95),
            "Daily low 94 <= dip level 95 must count as 'dip happened'")

    def test_rally_hit_detects_intraday_high_touching_level(self):
        from src import backtest
        # Predicted rally = 110. The window has a day where high reaches 112.
        window = [
            {"high": 102, "low": 98, "close": 100, "adj_close": 100},
            {"high": 112, "low": 100, "close": 105, "adj_close": 105},
            {"high": 108, "low": 104, "close": 106, "adj_close": 106},
        ]
        self.assertTrue(backtest._touched_above(window, 110),
            "Daily high 112 >= rally level 110 must count as 'rally happened'")

    def test_dip_miss_when_low_never_reaches_level(self):
        from src import backtest
        # Predicted dip = 90, but the window's lowest low is 96 — dip never happened.
        window = [
            {"high": 102, "low": 99, "close": 100, "adj_close": 100},
            {"high": 101, "low": 96, "close": 99, "adj_close": 99},
        ]
        self.assertFalse(backtest._touched_below(window, 90),
            "Lowest low 96 > dip level 90 means the dip didn't happen")

    def test_rally_miss_when_high_never_reaches_level(self):
        from src import backtest
        # Predicted rally = 130, but window's highest high is 108 — rally didn't happen.
        window = [
            {"high": 102, "low": 99, "close": 100, "adj_close": 100},
            {"high": 108, "low": 100, "close": 105, "adj_close": 105},
        ]
        self.assertFalse(backtest._touched_above(window, 130),
            "Highest high 108 < rally level 130 means the rally didn't happen")


class TestBacktestColdStartGate(unittest.TestCase):
    """When fewer than MIN_HISTORY_TRADING_DAYS of snapshots have
    accumulated, the backtest must return 'insufficient_data' rather
    than empty/misleading metrics. Cold-start honesty.
    """

    def test_empty_snapshot_archive_returns_insufficient_data(self):
        from unittest import mock
        from src import backtest
        with mock.patch.object(backtest, "_accumulated_snapshot_dates", return_value=[]):
            out = backtest.run({})
        self.assertEqual(out["status"], "insufficient_data")
        self.assertEqual(out["days_of_history"], 0)
        self.assertEqual(out["days_needed"], backtest.MIN_HISTORY_TRADING_DAYS)


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


class TestTrajectoryClassification(unittest.TestCase):
    """Conviction trajectory module: classifies multi-night score
    sequences as rising / stable / decaying / unstable. Pinning the
    boundary cases so threshold tuning is visible if it changes."""

    def test_rising_sequence_classified_rising(self):
        from src.trajectory import _classify
        annotation, _ = _classify([0.05, 0.08, 0.12, 0.15, 0.18])
        self.assertEqual(annotation, "rising")

    def test_decaying_sequence_classified_decaying(self):
        from src.trajectory import _classify
        annotation, _ = _classify([0.30, 0.25, 0.20, 0.18, 0.15])
        self.assertEqual(annotation, "decaying")

    def test_stable_within_threshold_classified_stable(self):
        from src.trajectory import _classify
        # drift < ±0.05 → stable even with day-to-day jiggle
        annotation, _ = _classify([0.15, 0.16, 0.15, 0.14, 0.16])
        self.assertEqual(annotation, "stable")

    def test_three_direction_flips_classified_unstable(self):
        from src.trajectory import _classify
        # Up, down, up, down, up = 3 sign flips in last 5 nights → unstable
        annotation, _ = _classify([0.15, 0.25, 0.10, 0.22, 0.08, 0.20])
        self.assertEqual(annotation, "unstable")

    def test_first_night_returns_stable_with_friendly_message(self):
        from src.trajectory import _classify
        # Single-night case must not crash and must produce a useful message
        # so first production run renders cleanly instead of pending.
        annotation, summary = _classify([0.15])
        self.assertEqual(annotation, "stable")
        self.assertIn("First production night", summary)

    def test_extract_avg_score_averages_across_users(self):
        from src.trajectory import _extract_30d_avg_score
        snap = {
            "conviction": {"status": "ok", "horizons": {
                30: {
                    "aidy":  {"breakdown": {"final_score": 0.10}},
                    "jesse": {"breakdown": {"final_score": 0.20}},
                },
            }},
        }
        self.assertAlmostEqual(_extract_30d_avg_score(snap), 0.15)

    def test_extract_avg_score_handles_int_keyed_and_string_keyed_horizons(self):
        # snapshot.write/load round-trips through JSON which converts int
        # dict keys to strings. The extractor must accept both.
        from src.trajectory import _extract_30d_avg_score
        snap_int = {"conviction": {"status": "ok", "horizons": {30: {"aidy": {"breakdown": {"final_score": 0.10}}}}}}
        snap_str = {"conviction": {"status": "ok", "horizons": {"30": {"aidy": {"breakdown": {"final_score": 0.10}}}}}}
        self.assertEqual(_extract_30d_avg_score(snap_int), 0.10)
        self.assertEqual(_extract_30d_avg_score(snap_str), 0.10)

    def test_extract_avg_score_returns_none_when_conviction_pending(self):
        from src.trajectory import _extract_30d_avg_score
        snap = {"conviction": {"status": "pending"}}
        self.assertIsNone(_extract_30d_avg_score(snap))


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
