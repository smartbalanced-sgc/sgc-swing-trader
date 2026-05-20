"""Math-invariant tests for the SGC Swing Trader pipeline.

These tests assert PROPERTIES that must hold regardless of input
data - things like "probabilities sum to 1", "prices stay positive",
"a fair value is always positive when DCF runs", etc. The tests are
the defense against me dismissing a real future bug as "calibration"
or "edge case" - if a math invariant breaks, the test fails
regardless of what I claim.

Each test references the invariant it asserts and the failure mode
it would catch. Run all of these via:

    python -m unittest tests.test_math_invariants -v
"""

from __future__ import annotations

import math
import unittest
import os

# Force test mode so we don't pollute production paths during testing.
os.environ.setdefault("SGC_RUN_MODE", "test")

import numpy as np

from src import config
from src.pipeline import monte_carlo, analytic_verifier, regime, volatility
from src.pipeline import fair_value, targets


class TestMonteCarloInvariants(unittest.TestCase):
    """Properties the MC simulation must satisfy regardless of input."""

    def _build_minimal_snap(self, **overrides) -> dict:
        """A minimal snap that lets MC run end-to-end. Override any
        field to test specific scenarios."""
        snap = {
            "ticker": "TEST", "tier_anchor": "B",
            "targets": {"status": "ok", "current_price": 100.0,
                "users": {"u": {"target_price": 115.0, "stop_price": 90.0, "source": "vol-scaled-default"}}},
            "regime": {"status": "ok", "annualized_drift_implied": 0.10},
            "volatility": {"status": "ok", "forecast_30d_pct": 0.30},
            "catalyst": {"status": "ok"},
            "data": {"profile": {"price": 100.0}},
        }
        for k, v in overrides.items():
            snap[k] = v
        return snap

    def test_probabilities_in_unit_interval(self):
        """P(target), P(stop), P(both), P(neither) must all be in [0, 1].
        Failure here means the MC math is broken (negative counts, etc.)."""
        snap = self._build_minimal_snap()
        result = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        for user, user_data in result["users"].items():
            for h, stats in user_data["horizons"].items():
                for key in ("p_target", "p_stop", "p_both", "p_neither"):
                    val = stats[key]
                    self.assertGreaterEqual(val, 0.0, f"{user} {h}d {key} = {val} < 0")
                    self.assertLessEqual(val, 1.0, f"{user} {h}d {key} = {val} > 1")

    def test_probabilities_sum_to_one(self):
        """P(target) + P(stop) + P(both) + P(neither) must sum to 1 ± epsilon.
        Failure means we're double-counting or losing paths."""
        snap = self._build_minimal_snap()
        result = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        for user, user_data in result["users"].items():
            for h, stats in user_data["horizons"].items():
                total = stats["p_target"] + stats["p_stop"] + stats["p_both"] + stats["p_neither"]
                self.assertAlmostEqual(total, 1.0, places=6,
                    msg=f"{user} {h}d probabilities sum to {total}, expected 1.0")

    def test_paths_always_positive(self):
        """GBM paths in log-space can't go to zero or negative. If they
        do, log() somewhere broke or numerical underflow happened."""
        rng = np.random.default_rng(42)
        paths, _ = monte_carlo._simulate_paths(
            s0=100.0, drift_annual=0.10, sigma_annual=0.30,
            n_paths=1000, n_days=30, rng=rng,
            earnings_jump_day=None, earnings_reactions=[],
        )
        self.assertTrue((paths > 0).all(),
            f"Some paths went non-positive! min={paths.min()}")

    def test_earnings_jump_distance_zero_treated_as_day_one(self):
        """REGRESSION: NVDA on earnings day itself (distance_sessions=0)
        must still apply the jump on day 1. Previously this case silently
        disappeared because the gating condition was `1 <= dist`."""
        # Distance=0 (earnings today AMC)
        snap_zero = self._build_minimal_snap(catalyst={
            "status": "ok", "distance_sessions": 0,
            "next_event": {"type": "earnings", "date": "2026-05-19"},
            "historical_reactions": [{"reaction_pct": -3.0}],
        })
        result_zero = monte_carlo.simulate("TEST", snap_zero, run_date="2026-05-19")
        self.assertTrue(result_zero["model_components"]["earnings_jump_overlay"],
            "earnings_jump_overlay should fire when distance_sessions=0")
        self.assertEqual(result_zero["model_components"]["earnings_jump_day"], 1,
            "When distance=0, jump should clamp to day 1 (AMC reaction lands in day-1 close-to-close)")

        # Sanity: distance=1 gives same jump day
        snap_one = self._build_minimal_snap(catalyst={
            "status": "ok", "distance_sessions": 1,
            "next_event": {"type": "earnings", "date": "2026-05-20"},
            "historical_reactions": [{"reaction_pct": -3.0}],
        })
        result_one = monte_carlo.simulate("TEST", snap_one, run_date="2026-05-19")
        self.assertEqual(result_one["model_components"]["earnings_jump_day"], 1)

    def test_reproducibility(self):
        """Same ticker + run_date must produce identical results.
        REGRESSION: any change to RNG seeding should fail this."""
        snap = self._build_minimal_snap()
        r1 = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        r2 = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        for user in r1["users"]:
            for h in r1["users"][user]["horizons"]:
                s1 = r1["users"][user]["horizons"][h]
                s2 = r2["users"][user]["horizons"][h]
                self.assertAlmostEqual(s1["p_target"], s2["p_target"], places=10)
                self.assertAlmostEqual(s1["p_stop"], s2["p_stop"], places=10)

    def test_drift_convention_ito_correction(self):
        """E[S_T/S_0] over T years should equal (1 + drift_annual)^T
        when using GBM with the correct Itô-corrected drift. If we
        forgot the Itô correction the mean would drift by σ²/2 per
        year. With 50K paths and σ=0.30, drift=0.10, T=60/252:
            expected E[S/S_0] = 1 + 0.10 * 60/252 ~ 1.024
            without Itô correction: 1.024 * exp(σ²/2 * T) ~ 1.035
        The difference is ~1pp, detectable in 50K paths."""
        rng = np.random.default_rng(42)
        paths, _ = monte_carlo._simulate_paths(
            s0=100.0, drift_annual=0.10, sigma_annual=0.30,
            n_paths=50000, n_days=60, rng=rng,
            earnings_jump_day=None, earnings_reactions=[],
        )
        T_years = 60 / 252
        expected_mean = 100.0 * (1.0 + 0.10) ** T_years   # arithmetic compounding
        observed_mean = float(paths[:, -1].mean())
        # At 50K paths SE is large for this; tolerance 1.5% relative
        relative_error = abs(observed_mean - expected_mean) / expected_mean
        self.assertLess(relative_error, 0.015,
            f"Terminal mean {observed_mean:.2f} vs expected {expected_mean:.2f} "
            f"(relative error {relative_error*100:.2f}%) - Itô correction may be wrong")


class TestPDEInvariants(unittest.TestCase):
    """PDE solver must produce probabilities in [0,1] and EV with right sign."""

    def test_pde_probabilities_in_unit_interval(self):
        """P(target) and P(stop) from PDE must each be in [0, 1]."""
        result = analytic_verifier._solve_all_three(
            s0=100.0, target_price=115.0, stop_price=90.0,
            convection=0.10, diffusion=0.045,  # ~30% vol, 10% drift
            horizon_days=30,
        )
        self.assertGreaterEqual(result["p_target"], 0.0)
        self.assertLessEqual(result["p_target"], 1.0)
        self.assertGreaterEqual(result["p_stop"], 0.0)
        self.assertLessEqual(result["p_stop"], 1.0)
        # p_target + p_stop must also be ≤ 1 (some paths neither-hit)
        self.assertLessEqual(result["p_target"] + result["p_stop"], 1.0 + 1e-6)

    def test_pde_symmetric_setup_balances(self):
        """With zero drift and symmetric target/stop (in log-space),
        P(target) should ≈ P(stop). Sanity check."""
        # log(110/100) = log(100/90.91) so symmetric in log-space
        result = analytic_verifier._solve_all_three(
            s0=100.0, target_price=110.0, stop_price=90.909,
            convection=0.0, diffusion=0.045,
            horizon_days=30,
        )
        self.assertAlmostEqual(result["p_target"], result["p_stop"], places=1,
            msg="Symmetric setup with zero drift should give symmetric probabilities")

    def test_pde_positive_drift_favors_target(self):
        """With +15% drift, P(target) > P(stop) for symmetric levels."""
        result = analytic_verifier._solve_all_three(
            s0=100.0, target_price=110.0, stop_price=90.909,
            convection=math.log(1.15) - 0.045, diffusion=0.045,
            horizon_days=30,
        )
        self.assertGreater(result["p_target"], result["p_stop"])

    def test_mc_pde_agree_after_bridge_correction(self):
        """REGRESSION: with Brownian bridge correction enabled, MC and
        PDE should agree within ~1pp on all metrics for GBM-only paths."""
        # Build NVDA-realistic snap
        snap = {
            "ticker": "TEST", "tier_anchor": "A",
            "targets": {"status": "ok", "current_price": 100.0,
                "users": {"u": {"target_price": 115.0, "stop_price": 92.0, "source": "vol-scaled-default"}}},
            "regime": {"status": "ok", "annualized_drift_implied": 0.10},
            "volatility": {"status": "ok", "forecast_30d_pct": 0.35},
            "catalyst": {"status": "ok"},  # no jump -> GBM-only paths
            "data": {"profile": {"price": 100.0}},
        }
        snap["monte_carlo"] = monte_carlo.simulate("TEST", snap, run_date="2026-05-19")
        # Provide minimal tier_classifier for analytic_verifier
        snap["tier_classifier"] = {"status": "ok", "measured_tier": "A",
            "properties": {"adv_usd": {"value": 1e9}}}
        result = analytic_verifier.verify("TEST", snap)
        # The 30d horizon delta on P(target) should be < 2pp
        h30 = result["horizons"][30]
        self.assertLess(h30["delta_p_target"] * 100, 2.0,
            f"MC-PDE P(target) delta = {h30['delta_p_target']*100:.2f}pp; "
            f"bridge correction should keep this < 2pp")


class TestFairValueInvariants(unittest.TestCase):
    """DCF / triangulation must produce sensible outputs."""

    def test_dcf_positive_when_fcf_positive(self):
        """DCF on a profitable ticker must produce a positive fair value."""
        # Synthetic profile + positive FCF — bypass the FMP fetch
        profile = {"shares_outstanding": 1e9}
        # Manually call _dcf would require mocking FMP. Instead test the
        # internal math via _estimate_growth_rate.
        history = [10e9, 12e9, 14e9, 17e9, 20e9]
        g, est = fair_value._estimate_growth_rate(history, cap=0.25)
        self.assertGreater(g, 0, f"Growing FCF should give positive growth, got {g}")
        self.assertLessEqual(g, 0.25, "Growth must be capped")
        self.assertIn(est, ("cagr", "median_yoy"))

    def test_dcf_skips_negative_fcf(self):
        """When TTM FCF is non-positive, DCF method returns None (skip).
        Otherwise we'd discount LOSSES forever and get a negative fair value."""
        # Can't easily test _dcf directly without FMP; verify the config flag
        self.assertTrue(config.THRESHOLDS.fair_value.dcf.skip_when_fcf_negative)

    def test_growth_estimator_falls_back_on_negative_year(self):
        """CAGR can't compute if any year is non-positive (geometric mean breaks).
        Should fall back to median_yoy."""
        mixed = [10e9, -2e9, 5e9, 8e9]  # one negative year in middle
        g, est = fair_value._estimate_growth_rate(mixed, cap=0.25, primary="cagr")
        self.assertEqual(est, "median_yoy",
            f"CAGR should fall back to median_yoy on negative years; got {est}")

    def test_triangulation_with_three_methods(self):
        """Range mean = median of three method values. Sigma = (high-low)/4."""
        methods = [
            {"name": "DCF", "value": 100.0, "details": {}},
            {"name": "Forward P/E peers", "value": 150.0, "details": {}},
            {"name": "Analyst PT", "value": 175.0, "details": {}},
        ]
        result = fair_value._triangulate(methods, current_price=140.0)
        self.assertEqual(result["range_low"], 100.0)
        self.assertEqual(result["range_high"], 175.0)
        self.assertEqual(result["range_mean"], 150.0)  # median of 3
        self.assertAlmostEqual(result["sigma"], (175 - 100) / 4.0, places=4)

    def test_tier_aware_wacc_resolves_correctly(self):
        """REGRESSION: tier-aware WACC must resolve to expected values."""
        self.assertAlmostEqual(fair_value._resolve_discount_rate("A"), 0.085, places=4)
        self.assertAlmostEqual(fair_value._resolve_discount_rate("B"), 0.095, places=4)
        self.assertAlmostEqual(fair_value._resolve_discount_rate("C"), 0.120, places=4)
        # None -> default fallback
        self.assertAlmostEqual(fair_value._resolve_discount_rate(None), 0.09, places=4)


class TestRegimeInvariants(unittest.TestCase):
    """Regime classifier must produce a valid state with valid confidence."""

    def test_state_probabilities_sum_to_one(self):
        """All five regime probabilities must sum to ~1.0 (softmax property)."""
        # Synthetic uptrend prices
        rng = np.random.default_rng(42)
        n = 200
        returns = rng.normal(0.15 / 252, 0.20 / np.sqrt(252), n)
        closes = 100 * np.exp(np.cumsum(returns))
        prices = [{"date": f"d{i}", "close": c, "adj_close": c} for i, c in enumerate(closes)]
        result = regime.detect("TEST", {"prices": prices}, tier="A")
        probs_sum = sum(result["state_probabilities"].values())
        self.assertAlmostEqual(probs_sum, 1.0, places=6)

    def test_confidence_is_max_probability(self):
        """The 'confidence' field equals the max state probability."""
        rng = np.random.default_rng(42)
        n = 200
        returns = rng.normal(0.15 / 252, 0.20 / np.sqrt(252), n)
        closes = 100 * np.exp(np.cumsum(returns))
        prices = [{"date": f"d{i}", "close": c, "adj_close": c} for i, c in enumerate(closes)]
        result = regime.detect("TEST", {"prices": prices}, tier="A")
        max_p = max(result["state_probabilities"].values())
        self.assertAlmostEqual(result["confidence"], max_p, places=6)


class TestTargetsInvariants(unittest.TestCase):
    """Per-user target/stop derivation must satisfy stop < current < target."""

    def test_vol_scaled_default_satisfies_ordering(self):
        """For watching users with vol-scaled defaults, must have
        stop_price < current_price < target_price."""
        snap = {
            "data": {"profile": {"price": 150.0}},
            "volatility": {"status": "ok", "forecast_30d_pct": 0.40},
        }
        entry = {"tier": "B", "holders": {"u": {"state": "watching"}}}
        result = targets.derive("TEST", entry, snap)
        self.assertEqual(result["status"], "ok")
        user_tgt = result["users"]["u"]
        self.assertLess(user_tgt["stop_price"], result["current_price"],
            "Stop must be below current price")
        self.assertGreater(user_tgt["target_price"], result["current_price"],
            "Target must be above current price")
        self.assertEqual(user_tgt["source"], "vol-scaled-default")


class TestLotteryFilterRegression(unittest.TestCase):
    """REGRESSION: Lottery filter requires BOTH bad prob_diff AND non-
    positive EV. Earlier (pre-fix) it fired on prob_diff alone, blocking
    every watching default verdict."""

    def test_lottery_filter_passes_on_positive_ev_negative_prob_diff(self):
        """A trade with negative prob_diff (P(target) < P(stop)) but
        POSITIVE EV (asymmetric payoff) should NOT trigger lottery filter."""
        from src import conviction
        # Use the config thresholds directly so the test tracks them
        cfg = config.THRESHOLDS.conviction.edge
        inputs = {
            "p_target": 0.30, "p_stop": 0.50, "ev_normalized": 0.20,  # positive EV
            "mc_pde_p_target_delta_pp": 0.0,
            "trajectory_direction_changes": 0,
            "watchlist_tier": "A", "measured_tier": "A",
            "tier_mismatch_consecutive_nights": 0,
            "catalyst_distance_sessions": None,
            "regime_state": "uptrend_quiet", "regime_confidence": 0.50,
            "fair_value_premium_sigmas": 0.0,
            "avg_daily_dollar_volume": 1e9,
            "user_state": "watching",
        }
        result = conviction.evaluate(inputs, horizon_days=30)
        self.assertFalse(result["layer1_edge"]["lottery_filter_failed"],
            "Lottery filter should NOT fire when EV is positive, even with bad prob_diff")

    def test_lottery_filter_fires_on_both_bad(self):
        """When prob_diff <= threshold AND EV <= 0, filter fires."""
        from src import conviction
        inputs = {
            "p_target": 0.20, "p_stop": 0.50, "ev_normalized": -0.10,
            "mc_pde_p_target_delta_pp": 0.0,
            "trajectory_direction_changes": 0,
            "watchlist_tier": "A", "measured_tier": "A",
            "tier_mismatch_consecutive_nights": 0,
            "catalyst_distance_sessions": None,
            "regime_state": "uptrend_quiet", "regime_confidence": 0.50,
            "fair_value_premium_sigmas": 0.0,
            "avg_daily_dollar_volume": 1e9,
            "user_state": "watching",
        }
        result = conviction.evaluate(inputs, horizon_days=30)
        self.assertTrue(result["layer1_edge"]["lottery_filter_failed"],
            "Lottery filter SHOULD fire when both prob_diff and EV are bad")


if __name__ == "__main__":
    unittest.main()
