"""Step 1 — Data fetch & sanity.

See docs/V1_SPEC.md §2. Pulls price history, volume, and fundamentals from
FMP (Financial Modeling Prep — our data vendor). Validates freshness,
completeness, and the absence of obvious corporate-action corruption.

This is the next step to implement.
"""


def fetch(ticker: str) -> dict:
    raise NotImplementedError
