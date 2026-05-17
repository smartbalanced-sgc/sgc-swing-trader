# SGC Swing Trader — Agent Operating Rules

## Communication

**No jargon, ever — without plain-English explanation in the same breath.**

Every technical term, acronym, or finance/trading concept must be explained in
plain English the first time it appears in any response (and again if it's been
a while). This applies to:

- Finance/trading terms (e.g., ATR, beta, drawdown, Kelly, slippage, basis point)
- Statistical terms (e.g., sigma, dispersion, drift, Monte Carlo)
- Engineering/infra terms when used in a finance context
- Any abbreviation or symbol that isn't universally known

Format: introduce the plain-English meaning first or alongside, not as a
footnote. Example:
- BAD: "We'll use ATR% > 2.0% as the filter."
- GOOD: "We'll use ATR% > 2.0% as the filter — ATR% means 'average daily
  movement as a share of price', so 2.0% means the stock typically moves at
  least 2% of its price in a normal day."

This rule applies in chat, in code comments, in commit messages, and in any
written artifact produced for this project.

## Universe & filtering principles

- US-listed equities only (FMP Starter plan covers US; non-US deferred until
  there is a deliberate reason to add it).
- Small-cap and mid-cap names are NOT excluded by default size/liquidity
  filters when a real catalyst is present. Standing-universe filters apply to
  the daily background scan; catalyst-driven candidates can bypass standing
  thresholds via the catalyst-override path, subject only to bare-minimum
  liquidity guards.
- Universe refresh: weekly.

## Scope discipline

- This system's job is conviction + timing (what to buy, when to enter, when
  to lift / recalibrate / exit). It is NOT a portfolio manager. Position
  sizing is out of scope — the user handles deployment on Trading 212.
- Don't reintroduce removed scope (sizing, share-count math, portfolio-level
  enforcement) without explicit re-authorization.
