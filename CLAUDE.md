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

## Position management — two-user protocol

This system tracks positions for TWO users: **Aidy and Jesse**. They share
the Claude.ai account, so the system cannot tell which human is typing from
session context alone. The identity question below is **mandatory every
action** — it is the disambiguation mechanism, not just flair.

### Recognized intents

- `add NVDA` / `watch NVDA` — add ticker to the speaker's personal watchlist
- `remove NVDA` / `drop NVDA` — remove the speaker from that ticker's holders
- `bought NVDA at 148.50` — flip speaker's state to entered
- `sold NVDA at 172.30` — flip speaker out of entered state
- `who's in NVDA?` / `what does Aidy hold?` / `what's the verdict on X?` —
  read-only queries, no commit

### Identity protocol — ask every time

For ANY mutating intent (add, remove, bought, sold), the FIRST response
must be the identity question. Vary the wording each time to keep it
fun — never the same phrase twice in a row. Examples:

- "Who are you, my lord?"
- "Speak thy name, oh swinger of trades."
- "Identify thyself, noble trader."
- "By whose hand is this trade made?"
- "Which of you doth wield the keyboard now — Aidy or Jesse?"
- "Name yourself, swing-trading sovereign."

Accept `aidy` or `jesse` (case-insensitive). For anything ambiguous
(`both`, `me`, an unrelated word), re-prompt playfully: "I need exactly
one lord — Aidy or Jesse?"

### Before committing any change

1. **Confirm the action in plain English first** ("OK Jesse — I'll mark
   NVDA as entered at $148.50, with target $172 and stop $137 (from
   yesterday's engine snapshot). Commit?")
2. **For new tickers not yet in watchlist:** briefly check market cap via
   FMP, propose a tier (A >$200B, B $10-200B, C <$10B as rough defaults),
   confirm with the user before saving.
3. **For `bought` with no price specified:** ask for the fill price.
4. **For `bought` with no target/stop:** read the latest snapshot from
   `data/snapshots/{TICKER}/`. Propose the engine's suggested target/stop.
   Allow override. If no snapshot exists yet (brand-new ticker), ask.
5. **For `sold`:** if the user isn't currently `entered`, push back:
   "You're not currently holding NVDA, Jesse — did you mean to remove it
   from your watchlist?"
6. **Cleanup:** if both users remove themselves from a ticker, delete the
   ticker entry entirely from watchlist.yml — don't leave orphans.

### Commit message format

- `Aidy: added NVDA to watchlist (tier A)`
- `Jesse: bought NVDA @ 148.50 — target 172, stop 137`
- `Aidy: sold ASML @ 750.80`
- `Jesse: removed PLTR from watchlist`

### After commit

Push to main. The next nightly cron run picks up the change automatically.
Confirm to the user that the commit landed and which file was changed.

## Run modes — production vs test

**RULE: Before suggesting any `python -m src.main` or `python -m
src.backtest` command, ALWAYS ask the user whether this is a test
run or a production run.** They look identical at the command line
but produce very different output paths:

- "Is this a test run or a production run?"
- Test run → prefix command with `SGC_RUN_MODE=test` (outputs go to
  `data/test/` and `docs/index-test.html`; no production state
  touched).
- Production run → no env var (outputs go to canonical paths and
  accumulate as real history).

If the user is iterating on the engine, calibrating thresholds, or
debugging a verdict, the answer is almost always "test." If they're
running the actual nightly cycle or committing to a real verdict,
it's "production." When in doubt, ask. Never assume.

The engine has two modes, controlled by the `SGC_RUN_MODE` env var:

- **production** (default — what the nightly cron uses, no env var set):
  outputs go to their canonical paths and accumulate as real history.
  Snapshots feed the multi-night Conviction Trajectory panel. Backtest
  results aggregate against real production data. The published
  dashboard at `docs/index.html` is what gets seen.
- **test** (`SGC_RUN_MODE=test python -m ...`): outputs route to
  `data/test/snapshots/`, `data/test/backtest/`, and
  `docs/index-test.html`. Production state is untouched no matter how
  many times you iterate. The dashboard shows a clear "TEST MODE"
  banner so test renders can't be mistaken for production ones.

Caches (`data/cache/*` — FMP and yfinance response caching) are SHARED
between modes. They're just network optimization; the underlying data
is identical regardless of which mode wrote them.

### When to use which

- Iterating on the engine (changing thresholds, tuning the conviction
  layer, debugging a verdict): test mode. Run as many times as you
  want; production never sees it.
- Adding a ticker / updating positions / running an actual nightly
  cycle for the user: production mode (no env var needed).
- Running the backtest harness against historical data to validate
  calibration: production mode is fine since the backtest itself uses
  truncated price data, not historical snapshots. But if you want the
  results file segregated from real production runs, use test mode.

### Going-live transition (one-time)

When ready to switch from "iterating" to "publishing a real nightly run":

1. **Verify**: review your latest test-mode dashboard at
   `docs/index-test.html`. Sign off on the output.
2. **Clean test data** (optional but recommended for a clean slate):
   `rm -rf data/test/`
3. **Run production once**: `python -m src.main` (no env var). This
   writes the first real snapshot and the canonical
   `docs/index.html`.
4. **Wire to cron**: GitHub Actions `.github/workflows/nightly.yml`
   runs `python -m src.main` (no env var) every night at 03:00 UTC.
5. **From this point on**, production snapshots accumulate. The
   Conviction Trajectory panel starts populating after ~5 nights;
   the backtest can be re-run with real history after ~20 nights.

You can keep iterating in test mode AT ANY TIME after going live —
just `SGC_RUN_MODE=test python -m src.main` and the production
history stays untouched.
