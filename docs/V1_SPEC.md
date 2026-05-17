# SGC Swing Trader — v1 Specification

**Status:** Locked design, pre-build
**Date:** 2026-05-17
**Companion documents:** FOUNDING_CHARTER.md (why), CLAUDE.md (operating
rules + position-management protocol)

This document captures every locked design decision for v1. The founding
charter establishes the why and the principles; this spec establishes the
what and the structural choices. The building conversation works from this
spec — if a question isn't answered here, the answer is either inherited
from FOUNDING_CHARTER.md or needs to be raised before code is written.

---

## 1. Purpose

A nightly-running engine that produces, for each ticker on a small static
watchlist, a full deep-dive report comparable to the classic SGC Dip
Engine's per-ticker output — but tailored for swing-trading semantics
(multi-day to multi-week holds, with separate signal logic for stocks
you're watching vs stocks you've already entered).

**Two users — Aidy and Jesse — share the system.** Each maintains their
own holdings; the engine produces a **single combined report** showing
both. Position state is managed via conversational web Claude Code
sessions (see §8), not manual YAML editing.

**The system's job:** conviction + timing, per user. What each of them
should buy, when to enter, when to hold/trim/exit.

**The system is NOT:** a portfolio manager. No position sizing, no
portfolio-level constraints, no order placement. Aidy and Jesse each
deploy capital manually on Trading 212.

---

## 2. Architecture overview

Mirrors the classic SGC Dip Engine's proven 8-step pipeline + 4-gate
orchestration pattern. Each ticker passes through the same 8 steps; the
4 gates determine which tickers warrant the full deep dive (gate 1 — basic
data sanity), which warrant the deep statistical work (gate 2 — passed
catalyst or universe filter), which produce a verdict (gate 3 — completed
all computations cleanly), and which surface to the dashboard with
priority flags (gate 4 — verdict is actionable today).

### The 8 steps (per ticker, per nightly run)

1. **Data fetch & sanity** — pull price history, volume, fundamentals from
   FMP (Financial Modeling Prep, our data vendor). Validate freshness,
   completeness, no obvious corporate-action corruption.
2. **Regime detection** — classify the current volatility/trend regime
   (uptrend-quiet, uptrend-noisy, downtrend, sideways, crisis) using a
   Hidden Markov Model — a statistical model that infers which of several
   hidden states a process is in based on observed price/volume — with
   Bayesian priors weighted per group (see §4).
3. **Catalyst detection** — scan for known scheduled catalysts (earnings,
   FDA dates, ex-div dates) and recent news flow. Output: catalyst-distance
   score and catalyst-magnitude estimate.
4. **Volatility forecast** — GARCH(1,1) — a statistical model that captures
   how today's volatility depends on recent volatility — fit to daily
   returns. Produces a forward path of expected daily volatility over the
   horizon. Group B and C tickers get wider confidence bands.
5. **Fair value estimate** — multi-method fair value triangulation
   (multiples vs sector, discounted cash flow where input quality is
   sufficient, recent comparable transactions). Output: fair value range +
   current discount/premium.
6. **Monte Carlo ensemble** — 50,000 simulated price paths over the horizon
   using the GARCH vol path and regime-conditioned drift. Outputs:
   probability of hitting target first, probability of hitting stop first,
   expected value, distribution of horizon-end prices.
7. **Analytic verifier** — Fokker-Planck partial differential equation
   solved by Crank-Nicolson finite-difference. Same inputs as step 6, same
   outputs, completely different math (deterministic, not random). Used as
   a cross-check: if step 6 and step 7 agree within tolerance, signal is
   trustworthy; if they disagree, dashboard flags the disagreement
   prominently and the signal is treated as low-confidence.
8. **Verdict synthesis (per user)** — combines outputs of steps 2-7 into
   a scorecard. The synthesis step **branches per user**: each user with
   state on this ticker gets their own verdict, in the appropriate verdict
   mode (§5).

### Dual horizon

Every ticker is analyzed at BOTH a 30-day primary horizon and a 60-day
secondary horizon, in parallel. The dashboard displays both. A signal that
agrees across horizons is stronger than a horizon-specific signal.

---

## 3. Universe & state model

**Universe:** US-listed equities only. Static watchlist for v1 in
`data/watchlist.yml`. The universe is the **union** of both users'
tickers — if either user is watching or entered, the ticker is analyzed.
Weekly manual refresh of the watchlist (via the conversational interface).

**Watchlist schema (per ticker):**

```yaml
TICKER:
  tier: A | B | C              # group (A mega-cap / B mid-cap growth / C small-cap catalyst)
  notes: "free text"           # ticker-level commentary, shared
  holders:
    aidy:                      # if user absent from holders → not tracking
      state: watching          # or "entered"
      # The following fields exist ONLY when state == entered:
      entry: 148.20
      target: 172.00
      stop: 137.00
      entered_date: 2026-05-15
    jesse:
      state: watching
```

**Semantics:**
- A user absent from `holders` = not tracking this ticker
- `state: watching` = on that user's personal watchlist, not yet entered
- `state: entered` = currently holding, with that user's own entry/target/stop
- If both users remove themselves from a ticker, the entire ticker entry
  is deleted from watchlist.yml

**State transitions** happen through web Claude Code sessions per the
protocol in CLAUDE.md (summarized in §8 below). Commits are attributed to
the acting user in the commit message ("Aidy: bought NVDA @ 148.50 —
target 172, stop 137"). Git history is the per-user trade journal.

---

## 4. Group framework

Three groups, each warranting differentiated statistical treatment. Group
membership is set in the watchlist YAML (`tier:` field) and determines
which priors, weights, and guards the pipeline applies.

### Group A — Mega-cap quality / index core
- **Characteristics:** stable volatility, deep liquidity, broad
  participation, long clean data history.
- **Treatment:** standard pipeline, standard priors, tighter target/stop
  multiples (smaller average daily moves), GARCH fits cleanly.
- **Examples:** AAPL, MSFT, NVDA, GOOGL, JPM, BRK.B
- **v1 representative:** **NVDA**

### Group B — Mid-cap growth / sector-driven
- **Characteristics:** elevated volatility, real catalyst sensitivity,
  decent liquidity, faster regime switching.
- **Treatment:** catalyst-detector weighted higher in scorecard, wider
  Bayesian priors on the regime detector (these names switch regimes
  faster), GARCH confidence intervals widened to account for higher
  volatility-of-volatility — how much the daily move-size itself swings
  around.
- **Examples:** ANET, SMCI, CRWD, PLTR, MRVL
- **v1 representative:** **ANET**

### Group C — Small-cap catalyst-driven
- **Characteristics:** high volatility, thin tape (few shares trading per
  day, so big trades can move price), catalyst-or-bust.
- **Treatment:** entry to the universe requires the catalyst-override path
  per the founding charter (bypasses standing thresholds, subject to
  bare-minimum liquidity guards — minimum daily dollar volume, minimum
  days-since-IPO). MC priors re-weighted toward fat-tailed distributions
  ("fat-tailed" means extreme moves happen more often than a normal bell
  curve predicts, which matches small-cap reality). Dashboard surfaces a
  prominent low-liquidity warning on every Group C ticker.
- **v1 representative:** **SNDK** — doubles as the regression test: our
  v1 output for SNDK must structurally match the existing dip-engine
  SNDK analysis (with swing-trader tweaks: dual horizon, two-mode verdict,
  conviction trajectory, per-user verdict).

**Tier inference for new tickers** (when added via the conversational
interface): Claude looks up the ticker's market cap and proposes a tier
based on rough thresholds (A: >$200B, B: $10-200B, C: <$10B). The user
can override. Exact threshold values are an open question for the
building conversation (§13).

---

## 5. Verdict modes

Verdicts are generated **per user, per ticker, per horizon**. The
underlying analysis (regime, vol, fair value, MC, PDE) runs once per
ticker per night; only the verdict synthesis step branches per user.

For each (user, ticker) pair, the verdict mode depends on that user's
state field:

### state == watching → ENTER / WAIT / SKIP *(should I enter today?)*

- **ENTER:** signal scorecard exceeds entry threshold, MC and PDE agree,
  catalyst window is open or absent, regime supports the direction.
- **WAIT:** signal is close to entry threshold but not over; verdict
  includes the specific gating factor ("WAIT — vol still elevated post
  earnings, re-check in 3 sessions") so the user knows what would unlock
  it.
- **SKIP:** signal is materially below entry threshold or one or more hard
  gates fail (data quality, catalyst risk too high, regime unfavorable).

### state == entered → HOLD / TRIM / EXIT *(should I stay in, given my fill?)*

- **HOLD:** conviction unchanged or rising, target/stop levels still
  valid, regime unchanged.
- **TRIM:** conviction decaying but signal not fully reversed; or price
  approaching target with diminishing expected upside.
- **EXIT:** stop level approached or breached, OR conviction fully
  reversed, OR catalyst event has passed and post-event signal
  deteriorated, OR target hit.

For entered names, the math reframes from "should I enter today at the
current price" to "given my actual fill at entry, what's the expected
value of holding from here." Different question, different answer — and
each user's answer uses their own entry/target/stop.

### state absent → no verdict

The dashboard cell shows a dash for that user on that ticker.

---

## 6. Dashboard specification

**Critical principle:** the dashboard mirrors the classic SGC Dip Engine's
per-ticker output depth. Every ticker gets a full deep-dive report — NOT
just a verdict badge. The swing-trader tweaks (dual horizon, two-mode
verdict, conviction trajectory, MC↔PDE agreement check, dual-user
verdicts) are added on top of, not in place of, the deep report.

### Page structure

1. **Header / metadata band** — run timestamp, watchlist version (git
   SHA), data freshness, system status.
2. **Top summary table** — one row per ticker, with separate state and
   verdict cells for each user:

   ```
   | Ticker | Group | Aidy state    | Aidy 30d/60d  | Jesse state    | Jesse 30d/60d | Flags        |
   |--------|-------|---------------|---------------|----------------|---------------|--------------|
   | NVDA   | A     | entered @148  | HOLD / HOLD   | watching       | ENTER / WAIT  |              |
   | ANET   | B     | —             | —             | entered @312   | TRIM / HOLD   | catalyst 3d  |
   | SNDK   | C     | watching      | WAIT / WAIT   | watching       | SKIP / WAIT   | low liquidity|
   ```

   A dash means that user isn't tracking the ticker.

3. **Per-ticker deep section** — for each ticker, a full report
   containing:
   - Thesis paragraph (plain English, regenerated each run from current
     scorecard)
   - Regime panel (current regime + probability + duration in regime)
   - Catalyst panel (next scheduled event, distance, expected magnitude,
     recent news headlines if any)
   - Volatility panel (GARCH forecast curve, current realized vol, current
     implied vol if available)
   - Fair value panel (estimate range, current premium/discount, methods
     used)
   - Monte Carlo panel (P(target), P(stop), EV, horizon-end price
     distribution histogram) — **for both 30d and 60d**
   - Analytic verifier panel (PDE-derived P(target), P(stop), EV) — **for
     both 30d and 60d**, with green/red agreement indicator
   - Conviction trajectory panel (last N nightly snapshots' conviction
     scores plotted, trend annotation)
   - **Dual verdict panel** — Aidy and Jesse side-by-side, each with their
     own verdict, plain-English rationale, and copy-paste YAML snippet for
     state change if applicable. If only one user is tracking the ticker,
     only that user's panel renders.
4. **Footer** — disclaimer, system version, build SHA, link to repo.

### Output format

Single static HTML file written to `docs/index.html`, deployed via GitHub
Pages. Designed to be readable on mobile (you'll check from phones in the
morning) but unconstrained by mobile in terms of depth — scrolling is
fine. Inline charts use SVG generated server-side (no client-side JS
dependencies for charting; keeps the page fast and offline-readable).

---

## 7. Snapshot persistence & conviction trajectory

Each nightly run, for each ticker, writes a JSON snapshot to
`data/snapshots/{TICKER}/{YYYY-MM-DD}.json` containing every numeric
output of the pipeline (regime probabilities, catalyst score, GARCH
vol-forecast path, fair value, MC outputs, PDE outputs, scorecard
components, per-user verdicts). Snapshots are ticker-level, not
user-level — the per-user verdicts are stored as fields within the same
ticker snapshot.

The next run loads recent snapshots for context:
- **Bayesian smoothing of conviction:** today's raw conviction score is
  blended with a smoothed prior built from the last N days, weighted by
  the regime — "Bayesian smoothing" means updating yesterday's belief
  with today's evidence rather than overwriting it.
- **Trajectory annotation:** dashboard shows whether conviction is
  rising, stable, or decaying.
- **Verdict stability:** verdicts that flip-flop day-to-day are flagged
  as low-confidence; verdicts that hold steady across multiple snapshots
  are flagged as high-confidence.

Snapshots are committed to the repo. Storage cost is trivial (~few KB per
ticker per day) and gives a permanent audit trail of every signal the
system ever produced — invaluable for backtesting and post-hoc review.

---

## 8. State communication — conversational interface

State changes happen by talking to Claude in a web Claude Code session
attached to this repo. **There is no manual YAML editing in the normal
flow** (manual editing remains possible as a fallback for emergencies).

**Workflow:**
1. User opens claude.ai/code → starts/continues the shared session in
   this repo
2. Types short intent: `add NVDA` / `bought NVDA at 148.50` / `sold ASML`
   / `who's in NVDA?` / etc.
3. Claude asks **"who are you, my lord?"** (or a varied playful
   equivalent — see CLAUDE.md). **Aidy and Jesse share the Claude
   account, so this question is mandatory every action — it is the only
   way the system can tell who is speaking.**
4. User replies `aidy` or `jesse` (case-insensitive)
5. Claude confirms the action in plain English, fetches any missing
   details (price, target, stop, tier) per the rules in CLAUDE.md
6. On confirmation, Claude edits `data/watchlist.yml`, commits with a
   user-attributed message, and pushes to main
7. Next nightly cron run picks up the new state

**Recognized intents:** add/watch, remove/drop, bought, sold, plus
read-only queries (who's holding what, what's the current verdict on X).

**Detailed protocol** — including identity-question wording variation,
intent recognition rules, tier inference for new tickers, target/stop
defaults from snapshots, commit message format, and edge-case handling —
lives in **CLAUDE.md**, so every web session inherits the rules
automatically.

---

## 9. Deployment

GitHub Actions cron, mirroring the classic SGC Dip Engine's workflow:
- **Schedule:** nightly at a time TBD, after US market close + sufficient
  buffer for end-of-day data to be available on FMP (likely ~03:00 UTC).
- **Runner:** ubuntu-latest, single job.
- **Steps:** checkout → set up Python → install deps → validate
  watchlist.yml schema → run engine → commit snapshots & dashboard →
  push to main.
- **Secrets:** FMP_API_KEY stored as GitHub Actions secret.
- **GitHub Pages:** serves `docs/` from main branch.

Run cost: small (single ubuntu-latest job, ~2-5 minutes per run at v1
scale, well within free-tier limits).

---

## 10. Repo scaffold

Charter §12 layout with practical adjustments:

```
sgc-swing-trader/
├── .github/workflows/
│   └── nightly.yml                  # cron + run + commit + deploy
├── data/
│   ├── watchlist.yml                # single source of truth for state
│   └── snapshots/{TICKER}/          # one JSON per ticker per nightly run
├── docs/
│   ├── index.html                   # generated dashboard
│   ├── V1_SPEC.md                   # this document
│   └── FOUNDING_CHARTER.md          # may move from root for cleanliness
├── src/
│   ├── main.py                      # entrypoint
│   ├── pipeline/                    # the 8 steps as separate modules
│   │   ├── data.py
│   │   ├── regime.py
│   │   ├── catalyst.py
│   │   ├── volatility.py
│   │   ├── fair_value.py
│   │   ├── monte_carlo.py
│   │   ├── analytic_verifier.py     # the PDE second-method
│   │   └── verdict.py
│   ├── groups.py                    # group definitions + per-group weights
│   ├── snapshot.py                  # snapshot read/write + conviction smoother
│   ├── dashboard.py                 # HTML generation
│   └── config.py                    # thresholds, paths, constants
├── tools/
│   └── validate_watchlist.py        # schema validator (run by cron)
├── tests/                           # NOT scaffolded in v1; added as needed
├── research/                        # NOT scaffolded in v1; added when backtest harness arrives
├── CLAUDE.md                        # operating rules + position-management protocol
├── README.md
└── requirements.txt
```

Adjustments vs charter §12: `tests/` and `research/` not pre-created —
they appear when there's something to put in them. `tools/` added for
the schema validator.

---

## 11. v1 ticker selection & initial state

| Ticker | Group | Why this one |
|---|---|---|
| **NVDA** | A — mega-cap quality | High enough vol to be interesting, deep liquidity, mature data history, genuinely actionable AI infra leader |
| **ANET** | B — mid-cap growth | Sector momentum (AI networking), real earnings catalysts, mid-cap to test catalyst weighting, liquid enough to pass guards |
| **SNDK** | C — small-cap catalyst | Post-spinoff catalyst, small-cap to test override path, **and** doubles as regression test against the dip-engine SNDK analysis |

**Initial state at v1 launch:** both Aidy and Jesse start as `watching`
on all three tickers. No entered positions on day one. Real entries
arrive organically through the conversational interface as trades happen.

Seed `data/watchlist.yml`:

```yaml
NVDA:
  tier: A
  notes: "AI infra leader"
  holders:
    aidy: {state: watching}
    jesse: {state: watching}

ANET:
  tier: B
  notes: "AI networking, sector momentum"
  holders:
    aidy: {state: watching}
    jesse: {state: watching}

SNDK:
  tier: C
  notes: "Post-spinoff catalyst, regression test vs dip-engine reference"
  holders:
    aidy: {state: watching}
    jesse: {state: watching}
```

---

## 12. Out of scope for v1

- **Position sizing.** Charter §4. Each user decides size manually on
  Trading 212.
- **Order placement.** No broker integration.
- **Universe expansion beyond static watchlist.** v2 candidate: catalyst
  scanner that proposes new tickers.
- **Non-US equities.** Charter §2 — deferred.
- **Sacred files / pre-canned positions.** Deferred to post-MVP.
- **Backtest harness.** `research/` directory appears when this work
  starts.
- **CLI helper for state mutations.** Conversational interface is the v1
  flow; manual YAML editing remains as emergency fallback.
- **GitHub Issues / web form / Trading 212 API for state.** All v2+
  candidates.
- **Per-user notification system** (e.g., email/SMS when verdict changes
  for your held positions). v2 candidate.
- **Per-user override of group treatments** (e.g., Aidy uses Group B
  treatment for a ticker Jesse uses Group A treatment for). v3+
  candidate; group is a ticker property, not a user property.

---

## 13. Known open questions for the building conversation

- Exact thresholds for ENTER vs WAIT (the scorecard cutoff)
- Exact tolerance for MC↔PDE agreement flag (1pp on probabilities? 5% on
  EV?)
- Snapshot retention policy (keep forever, or prune to last 90 days?)
- Chart library choice (matplotlib SVG output? hand-rolled SVG? other?)
- Catalyst data source (FMP earnings calendar? second source for FDA
  dates?)
- Plain-English thesis generation method (template-driven, or small LLM
  call?)
- Exact market-cap thresholds for tier inference (A/B/C boundaries)
- Breadth of read-only query intents in the conversational interface
- Concurrent-edit handling: v1 assumes rare collisions self-heal via
  pull-rebase-retry, but if collisions prove common we may need a simple
  lock file in `data/`

---

## 14. Definition of done for v1

The v1 build is "done" when:
- Nightly cron runs cleanly without manual intervention for 5 consecutive
  nights
- Dashboard renders all 3 tickers with full per-ticker reports
- MC and PDE both run and agree (or flag disagreement) for all 3 tickers
- SNDK's v1 output structurally matches the dip-engine reference (with
  swing-trader tweaks) — manual review by Jesse + Aidy
- Conversational interface handles all five core intents end-to-end:
  add, remove, bought, sold, query — including the identity question,
  tier inference for new tickers, snapshot-derived target/stop defaults,
  and user-attributed commit messages
- Per-user verdicts render correctly on the dashboard for at least one
  ticker where Aidy and Jesse have different states
- Snapshot persistence works; conviction trajectory appears correctly
  after a few nights of accumulated data
- Dashboard is readable on phone

This is the bar for "MVP working." Polish, additional tickers, backtest
harness, sacred files, and v1.x enhancements come after.
