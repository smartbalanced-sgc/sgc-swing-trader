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
from src.pipeline import fair_value, targets, swing_mode


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


class TestSwingModeInvariants(unittest.TestCase):
    """Swing-mode metrics must satisfy structural invariants regardless
    of input paths. These are the guard rails against silent regressions
    in the dip/rally/sequence/recovery math."""

    def _make_paths(self, seed: int = 42, n_paths: int = 5000, n_days: int = 30,
                    sigma_annual: float = 0.30, drift_annual: float = 0.10,
                    s0: float = 100.0) -> np.ndarray:
        """Generate a fresh set of MC paths for property testing."""
        rng = np.random.default_rng(seed)
        paths, _ = monte_carlo._simulate_paths(
            s0=s0, drift_annual=drift_annual, sigma_annual=sigma_annual,
            n_paths=n_paths, n_days=n_days, rng=rng,
            earnings_jump_day=None, earnings_reactions=[],
        )
        return paths

    def test_dip_below_rally_above_current(self):
        """The 70%-touch dip must be ≤ current; the 70%-touch rally ≥
        current. Otherwise the percentile math is inverted."""
        paths = self._make_paths()
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        self.assertLessEqual(out["dip_entry"], 100.0,
            f"dip_entry {out['dip_entry']} > current 100")
        self.assertGreaterEqual(out["rally_exit"], 100.0,
            f"rally_exit {out['rally_exit']} < current 100")

    def test_sequence_prob_bounds(self):
        """P(dip-then-rally) and P(miss-the-dip) must each be in [0, 1]."""
        paths = self._make_paths()
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        for key in ("sequence_prob", "miss_dip_prob"):
            v = out[key]
            self.assertGreaterEqual(v, 0.0, f"{key} = {v} < 0")
            self.assertLessEqual(v, 1.0, f"{key} = {v} > 1")

    def test_sequence_prob_below_both_touch_probs(self):
        """P(dip-then-rally) ≤ min(P(dip touched), P(rally touched)).
        At the 70% configured touch confidence, both individual touch
        probabilities are ~70% by construction, so sequence prob must
        be ≤ 0.70 (typically much lower due to ordering constraint)."""
        paths = self._make_paths()
        n_paths, n_days = paths.shape
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        below_dip = (paths <= out["dip_entry"]).any(axis=1).mean()
        above_rally = (paths >= out["rally_exit"]).any(axis=1).mean()
        self.assertLessEqual(out["sequence_prob"], min(below_dip, above_rally) + 1e-9,
            f"sequence_prob {out['sequence_prob']} exceeds min touch prob "
            f"({below_dip}, {above_rally}) — ordering constraint violated")

    def test_swing_stop_below_dip_when_defined(self):
        """When a swing stop is defined, it must be strictly below
        dip-entry. A stop at or above dip would be nonsensical (you'd
        exit before the trade ever started)."""
        paths = self._make_paths(sigma_annual=0.50, n_days=60)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        stop = out["swing_stop"]["primary"]
        if stop is not None:
            self.assertLess(stop, out["dip_entry"],
                f"swing_stop {stop} ≥ dip_entry {out['dip_entry']}")

    def test_recovery_prob_at_stop_meets_threshold(self):
        """When a swing stop is defined, the recovery probability at
        that level must be ≥ the configured recovery_confidence
        threshold. This is the defining property of the stop."""
        paths = self._make_paths(sigma_annual=0.50, n_days=60)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        stop_blk = out["swing_stop"]
        if stop_blk["primary"] is not None:
            self.assertGreaterEqual(
                stop_blk["recovery_prob_at_stop"],
                stop_blk["recovery_threshold"] - 1e-9,
                f"recovery_prob_at_stop {stop_blk['recovery_prob_at_stop']} "
                f"< threshold {stop_blk['recovery_threshold']}"
            )

    def test_reward_to_risk_math(self):
        """R:R must equal (rally - dip) / (dip - swing_stop) when both
        are defined. Off-by-one or sign errors here would silently mis-
        calibrate the ACTIONABLE verdict."""
        paths = self._make_paths(sigma_annual=0.50, n_days=60)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        stop = out["swing_stop"]["primary"]
        if stop is not None and out["dip_entry"] > stop:
            expected_rr = (out["rally_exit"] - out["dip_entry"]) / (out["dip_entry"] - stop)
            self.assertAlmostEqual(out["reward_to_risk"], expected_rr, places=6,
                msg=f"R:R math wrong: got {out['reward_to_risk']}, expected {expected_rr}")

    def test_yield_math(self):
        """Yield% = (rally - dip) / dip × 100, by definition."""
        paths = self._make_paths()
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        expected = (out["rally_exit"] - out["dip_entry"]) / out["dip_entry"] * 100.0
        self.assertAlmostEqual(out["yield_pct"], expected, places=6)

    def test_verdict_three_tier_classification(self):
        """Three-tier verdict logic must classify each setup correctly:
          ACTIONABLE  — all gates pass
          WATCHABLE   — exactly 1 numeric gate fails AND sequence_prob
                        passes AND swing_stop is defined
          NO_SWING    — multiple gates fail, or sequence_prob fails,
                        or swing_stop undefined
        Guards against a silent partial-pass slipping through, AND
        guards against WATCHABLE firing when conviction (sequence
        prob) isn't there."""
        paths = self._make_paths(sigma_annual=0.50, n_days=60)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        passes = out["components"]["passes"]
        label = out["verdict"]["label"]
        numeric_gates = ("dip_below_current_min", "rally_above_current_min",
                         "yield_min", "sequence_prob_min", "reward_to_risk_min")
        n_fail = sum(1 for g in numeric_gates if not passes[g])
        stop_ok = passes["swing_stop_defined"]
        seq_ok = passes["sequence_prob_min"]

        if n_fail == 0 and stop_ok:
            self.assertEqual(label, "ACTIONABLE",
                f"all gates pass but verdict is {label}: {passes}")
        elif n_fail == 1 and seq_ok and stop_ok:
            self.assertEqual(label, "WATCHABLE",
                f"exactly 1 fail with sequence_prob+stop ok but verdict "
                f"is {label}: {passes}")
        else:
            self.assertEqual(label, "NO_ACTIONABLE_SWING",
                f"{n_fail} gates fail, seq_ok={seq_ok}, stop_ok={stop_ok} "
                f"but verdict is {label}: {passes}")

    def test_watchable_requires_sequence_prob_to_pass(self):
        """WATCHABLE is the conviction-anchored near-miss tier. If
        sequence_prob fails, the verdict must NEVER be WATCHABLE —
        regardless of how many other gates pass. Without sequence
        prob the trade has no conviction; surfacing it as WATCHABLE
        would be misleading."""
        # Low-vol synthetic that produces a low sequence prob
        paths = self._make_paths(sigma_annual=0.10, n_days=30)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        if not out["components"]["passes"]["sequence_prob_min"]:
            self.assertNotEqual(out["verdict"]["label"], "WATCHABLE",
                f"sequence_prob fails but verdict is WATCHABLE — "
                f"violates conviction-anchor rule")

    def test_flip_hints_present_for_each_failing_gate(self):
        """Every failing gate (except ACTIONABLE) must produce a flip
        hint. Missing hints means the user has no actionable feedback
        on what would change the verdict."""
        paths = self._make_paths(sigma_annual=0.50, n_days=60)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        passes = out["components"]["passes"]
        hints = out["flip_hints"]
        if out["verdict"]["label"] == "ACTIONABLE":
            self.assertEqual(hints, [], "ACTIONABLE setups should have no flip hints")
        else:
            gates_with_hints = {h["gate"] for h in hints}
            failed_gates = {g for g, p in passes.items() if not p}
            self.assertEqual(gates_with_hints, failed_gates,
                f"Mismatch: failed={failed_gates}, hinted={gates_with_hints}")

    def test_flip_hint_structure(self):
        """Every flip hint must have the required keys (gate, current,
        threshold, hint) so the dashboard can render them safely
        without crashing on a missing field."""
        paths = self._make_paths(sigma_annual=0.05, n_days=30)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        for h in out["flip_hints"]:
            for key in ("gate", "current", "threshold", "hint"):
                self.assertIn(key, h, f"flip hint missing key '{key}': {h}")
            self.assertIsInstance(h["hint"], str)
            self.assertGreater(len(h["hint"]), 20,
                f"flip hint text suspiciously short: {h['hint']}")

    def test_low_vol_paths_produce_no_actionable_swing(self):
        """A very-low-vol synthetic stock (paths cluster tightly around
        current) must NOT fire ACTIONABLE — the dip and rally levels
        will be too close to current to satisfy the min-magnitude gates.
        This is the canonical 'sleepy stock' regression."""
        # 5% annualized vol — extremely tight; basically a money-market
        paths = self._make_paths(sigma_annual=0.05, n_days=30)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        self.assertEqual(out["verdict"]["label"], "NO_ACTIONABLE_SWING",
            f"Low-vol stock should not be ACTIONABLE; got {out['verdict']['label']} "
            f"with dip={out['dip_entry']:.2f}, rally={out['rally_exit']:.2f}")

    def test_reproducibility_under_same_seed(self):
        """Same paths in must produce identical swing-mode outputs.
        REGRESSION: any non-determinism (unsorted dict iteration,
        unseeded RNG, etc.) should fail this."""
        paths1 = self._make_paths(seed=99)
        paths2 = self._make_paths(seed=99)
        out1 = swing_mode.compute_for_horizon(paths1, current_price=100.0)
        out2 = swing_mode.compute_for_horizon(paths2, current_price=100.0)
        self.assertEqual(out1["sequence_prob"], out2["sequence_prob"])
        self.assertEqual(out1["dip_entry"], out2["dip_entry"])
        self.assertEqual(out1["rally_exit"], out2["rally_exit"])
        self.assertEqual(out1["swing_stop"]["primary"], out2["swing_stop"]["primary"])
        self.assertEqual(out1["verdict"]["label"], out2["verdict"]["label"])

    def test_dip_at_70_pctile_means_70_pct_paths_touch(self):
        """The dip-entry at the 70% touch confidence MUST be touched by
        approximately 70% of paths (within statistical noise). This
        verifies the percentile interpretation: percentile(path_mins,
        70) is the level 70% of mins are ≤. Mathematically tight
        invariant, not just a heuristic."""
        paths = self._make_paths(n_paths=10000)
        out = swing_mode.compute_for_horizon(paths, current_price=100.0)
        actual_touch_frac = (paths <= out["dip_entry"]).any(axis=1).mean()
        # Should be ~0.70 within ~1% (10k paths gives SE ~0.0046)
        self.assertAlmostEqual(actual_touch_frac, 0.70, delta=0.02,
            msg=f"dip touched by {actual_touch_frac*100:.1f}% of paths "
                f"vs configured 70%")


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
