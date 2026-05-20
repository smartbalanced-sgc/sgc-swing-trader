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
run or a production run.** Default is TEST (safe — doesn't touch
production state). Production is opt-in and explicit.

The framing question:
- "Is this a test run or a production run?"
- Test (default) → `python -m src.main` (or `SGC_RUN_MODE=test`,
  same thing). Outputs to `data/test/` and `docs/index-test.html`.
- Production → `SGC_RUN_MODE=production python -m src.main`.
  Outputs to canonical paths and accumulate as real history.

If the user is iterating on the engine, calibrating thresholds,
debugging a verdict, or just "trying things" — it's test. If they've
explicitly said "I want this to count" or "I'm happy with this, let's
make it production" — it's production. When in doubt, default to
test. Never assume production.

### How modes differ

- **test** (DEFAULT — no env var needed, or `SGC_RUN_MODE=test`):
  outputs route to `data/test/snapshots/`, `data/test/backtest/`,
  and `docs/index-test.html`. Production state is untouched no matter
  how many times you iterate. The dashboard shows a clear "TEST MODE"
  banner so test renders can't be mistaken for production ones.
- **production** (`SGC_RUN_MODE=production`): outputs go to canonical
  paths (`data/snapshots/`, `data/backtest/`, `docs/index.html`) and
  accumulate as real history. The Conviction Trajectory panel reads
  this multi-night history; backtests aggregate against it.

Caches (`data/cache/*` — FMP and yfinance response caching) are SHARED
between modes. They're just network optimization; the underlying data
is identical regardless of which mode wrote them.

### No cron — the user runs manually

There is no scheduled GitHub Actions cron. The engine runs ONLY when
the user invokes it. This was an explicit choice: prices change
intraday, the user wants to decide WHEN to refresh the signal (e.g.
morning before US open, or after a big midday move).

### Recommended UK local run times (when in production)

- **7-8 AM UK** — most natural. Wake up, run the engine, read the
  dashboard, make decisions for the US session ahead. Captures
  yesterday's close + overnight news from Europe/Asia.
- **11 PM UK** — alternative if you want to read post-close. Captures
  the full US trading day + after-hours earnings reactions + the
  most-current news.
- **On-demand during the trading day** — when a price moves a lot
  or major news breaks, the user can `python -m src.main --refresh`
  to force a fresh fetch (overrides the 4h cache TTL).

### --refresh flag

Forces fresh FMP fetches by clearing per-ticker cache files before
the run. Use when:
- Major news just broke and you want the very latest tick.
- You're testing a code change and need to see how it behaves on
  fresh data (vs whatever was cached).
- It's been < 4 hours since the last run but you want to re-fetch
  anyway.

Without `--refresh`, the data cache has a 4-hour TTL (calibrated for
intraday re-runs — multiple runs within ~4h share cache, after that
the engine auto-refreshes).

### Going-live transition (one-time)

When the user explicitly says they're ready to publish a real run:

1. **Verify**: review the latest test-mode dashboard at
   `docs/index-test.html`. User signs off.
2. **Clean test data** (optional, for a clean slate):
   `rm -rf data/test/`
3. **Run production once**: `SGC_RUN_MODE=production python -m src.main`.
   Writes the first real snapshot and the canonical `docs/index.html`.
4. **From this point on**, every production-mode invocation
   accumulates a snapshot in `data/snapshots/{TICKER}/{date}.json`.
   The Conviction Trajectory panel populates after ~5 production
   runs; the backtest gains historical depth as snapshots accumulate.

You can keep iterating in test mode AT ANY TIME after going live —
test runs never touch production history.

## Bug-classification rules — what's a bug vs what's not

A previous audit declared the codebase "done — no more bugs to find."
That kind of declaration creates a cognitive trap: once I've said
"done," I'm biased to dismiss new findings as "calibration" or "edge
case" to avoid contradicting myself. To prevent that, the rules for
what counts as a bug are pinned here. I commit to USING these rules,
not redefining them when convenient.

### A finding IS a bug if ANY of:

- **Math produces incorrect output** (sign error, wrong convention,
  divide-by-zero, off-by-one, wrong units, data leak from future
  into past).
- **Code crashes on a reasonable input** (where "reasonable" means
  any state the watchlist + config + FMP could plausibly produce).
- **Schema mismatch between producer and consumer** (one module
  emits `horizons[h]["users"][user]`, another reads
  `horizons[h][user]`).
- **Silent failure that affects the verdict** (a sub-step fails
  but no error surfaces, and the downstream verdict changes
  without the user knowing why).
- **Behavior diverges from the documented design** (the YAML
  comment says X, the code does Y).
- **Same input produces different output across runs** (broken
  reproducibility, except where deliberately randomized).
- **Information the system has is not surfaced to the user where a
  competent swing trader would expect to find it** (e.g. dip-entry
  zone exists but never connects to the SKIP verdict).

### A finding is NOT a bug if:

- **Calibration knob value** (multiplier, threshold, lookback
  window). Move to `config/thresholds.yml`, tune with backtest
  data. Not a bug — a tuning question.
- **Marginal improvement requiring substantial new complexity**
  (Heston stochastic vol, multi-asset correlation, intraday data
  feed). Defer honestly — it's a v2, not a bug.
- **Genuinely needs accumulated data first** (multi-night
  trajectory smoothing, sustained tier-mismatch haircut).
- **Style / cosmetic** (whitespace, naming, docstring grammar
  that doesn't cause confusion).

### The default-to-bug protocol

When the user reports something that doesn't look right:

1. **Default classification**: POTENTIAL BUG.
2. The burden of proof is on ME to demonstrate it's NOT a bug
   (show the math, show the invariant, show the documented design,
   show the test that already passes).
3. If I can't demonstrate within a few minutes that it's not a bug,
   it's a bug. Fix it.
4. **NEVER** open with "that's expected" or "that's a known
   limitation" without first checking. Investigation comes BEFORE
   dismissal.

### How the user can challenge an "this isn't a bug" verdict

If I tell you "that's calibration not a bug," you can force a real
investigation by asking:
- "Show me the test that would have caught this if it were a bug."
- "Walk me through the code path where this is intentional."
- "Show me the snapshot JSON that proves the value is correct."

If I can't answer with concrete code/data, the finding gets
escalated back to BUG and fixed.

### Protection mechanisms (not just promises)

Three things in the codebase actively guard against me dismissing
real bugs:

1. **`tests/` directory** with property-based math tests and
   regression tests for every bug we've ever fixed. The tests
   don't have egos — they fail when invariants break, regardless
   of what I claimed.

2. **Runtime assertions** for invariants that should always hold
   (probabilities sum to ~1.0, prices stay positive, FCF >= 0
   when DCF runs, etc.). Loud failures beat silent ones.

3. **Snapshot JSON on disk** with every step's full state.
   You can verify any claim I make about a number by looking at
   `data/test/snapshots/{TICKER}/{date}.json` and reading the
   actual values. I cannot hide behind narrative.
