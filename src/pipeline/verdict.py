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


def synthesize(ticker: str, holders: dict, analysis: dict, horizon: int) -> dict:
    raise NotImplementedError
