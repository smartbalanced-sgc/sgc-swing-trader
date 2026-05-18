"""Step 8 — Verdict synthesis (per user).

See docs/V1_SPEC.md §§2, 5. Combines the outputs of steps 2-7 into a
scorecard, then branches per user: each user with state on this ticker
gets their own verdict in the appropriate mode.

Modes (docs/V1_SPEC.md §5):
  - state == watching → ENTER / WAIT / SKIP (should I enter today?)
  - state == entered  → HOLD / TRIM / EXIT  (should I stay in, given my fill?)
  - state absent      → no verdict for that user

The underlying analysis runs once per ticker per night; only this synthesis
step branches per user — each user's entry/target/stop reframes the math.
"""


def synthesize(ticker: str, snap: dict, user: str, state: dict) -> dict:
    """Produce one user's verdict for one ticker at both 30d and 60d
    horizons. `state` is the user's watchlist entry (state == 'watching'
    or 'entered', with optional entry/target/stop fields when entered).
    `snap` contains the upstream analysis (regime, vol, fair_value,
    monte_carlo, analytic_verifier).

    Returns {"30d": {"verdict": ..., "rationale": ...},
             "60d": {"verdict": ..., "rationale": ...}}.
    """
    raise NotImplementedError
