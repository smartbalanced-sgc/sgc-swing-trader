"""Group framework — Tier A / B / C definitions and per-group weights.

See docs/V1_SPEC.md §4. Group membership is set in data/watchlist.yml per
ticker (the `tier:` field) and determines which priors, weights, and guards
the pipeline applies.

This module is currently a stub. Per-group weight tables get filled in as
the pipeline steps that consume them (regime, volatility, catalyst, MC)
come online.
"""

TIERS = ("A", "B", "C")
