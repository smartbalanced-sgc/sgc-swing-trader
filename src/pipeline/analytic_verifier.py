"""Step 7 — Analytic verifier (second-method cross-check).

See docs/V1_SPEC.md §2. Solves the Fokker-Planck partial differential
equation by Crank-Nicolson finite-difference — same inputs as the Monte
Carlo ensemble (step 6), same outputs (P(target), P(stop), expected
value), completely different math (deterministic, not random).

If steps 6 and 7 agree within tolerance, the signal is trustworthy; if
they disagree, the dashboard flags the disagreement prominently and the
signal is treated as low-confidence.
"""


def verify(ticker: str, snap: dict) -> dict:
    """Reads upstream outputs (vol_path, regime) from the in-progress
    snapshot and solves the Fokker-Planck PDE for both horizons. Returns
    {"30d": {...}, "60d": {...}} with the same per-horizon keys as the
    Monte Carlo simulator so the dashboard can run a side-by-side
    agreement check."""
    raise NotImplementedError
