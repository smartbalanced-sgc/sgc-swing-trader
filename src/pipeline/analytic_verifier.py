"""Step 7 — Analytic verifier (second-method cross-check).

See docs/V1_SPEC.md §2. Solves the Fokker-Planck partial differential
equation by Crank-Nicolson finite-difference — same inputs as the Monte
Carlo ensemble (step 6), same outputs (P(target), P(stop), expected
value), completely different math (deterministic, not random).

If steps 6 and 7 agree within tolerance, the signal is trustworthy; if
they disagree, the dashboard flags the disagreement prominently and the
signal is treated as low-confidence.
"""


def verify(ticker: str, vol_path: dict, regime: dict, horizon: int) -> dict:
    raise NotImplementedError
