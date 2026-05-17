"""Step 6 — Monte Carlo ensemble.

See docs/V1_SPEC.md §2. Simulates 50,000 price paths per (ticker, horizon)
using the GARCH vol path from step 4 and regime-conditioned drift from
step 2. Tier C uses fat-tailed return distributions (extreme moves happen
more often than a normal bell curve predicts, matching small-cap reality).

Outputs per horizon: probability of hitting target first, probability of
hitting stop first, expected value, and the full distribution of
horizon-end prices.
"""


def simulate(ticker: str, vol_path: dict, regime: dict, tier: str, horizon: int) -> dict:
    raise NotImplementedError
