"""Step 4 — Volatility forecast.

See docs/V1_SPEC.md §2. Fits a GARCH(1,1) model — a statistical model that
captures how today's volatility depends on recent volatility — to daily
returns. Produces a forward path of expected daily volatility over each
horizon (30d and 60d). Tier B and C tickers get wider confidence bands to
account for higher volatility-of-volatility (how much the daily move-size
itself swings around).
"""


def forecast(ticker: str, price_data: dict, tier: str) -> dict:
    raise NotImplementedError
