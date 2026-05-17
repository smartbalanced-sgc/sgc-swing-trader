"""Step 5 — Fair value estimate.

See docs/V1_SPEC.md §2. Multi-method fair-value triangulation: multiples
vs sector, discounted cash flow where input quality is sufficient, recent
comparable transactions. Output is a fair value range and the current
premium-or-discount the market price is trading at relative to that range.
"""


def estimate(ticker: str, fundamentals: dict) -> dict:
    raise NotImplementedError
