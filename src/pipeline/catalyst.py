"""Step 3 — Catalyst detection.

See docs/V1_SPEC.md §2. Scans for known scheduled catalysts (earnings, FDA
dates, ex-div dates) and recent news flow. Outputs a catalyst-distance
score (how many sessions away) and catalyst-magnitude estimate (expected
percent move).
"""


def detect(ticker: str, tier: str) -> dict:
    raise NotImplementedError
