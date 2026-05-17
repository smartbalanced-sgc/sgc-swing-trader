"""Step 2 — Regime detection.

See docs/V1_SPEC.md §2. Classifies the current volatility/trend regime
(uptrend-quiet, uptrend-noisy, downtrend, sideways, crisis) using a Hidden
Markov Model — a statistical model that infers which of several hidden
states a process is in based on observed price/volume — with Bayesian
priors weighted per group (Tier A / B / C).
"""


def detect(ticker: str, price_data: dict, tier: str) -> dict:
    raise NotImplementedError
