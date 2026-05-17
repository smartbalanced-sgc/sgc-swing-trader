# SGC Swing Trader — Founding Charter

> **Read this first. Every session. No exceptions.**
> This document is the lossless context handover from the parent project (`sgc-dip-engine`) and the SNDK swing-trade tool that preceded this repo. It exists because this repo is fully isolated — the new Claude session cannot read the parent repo via MCP. Everything you need to design intelligently is here.

- **Last updated:** 2026-05-17
- **Author of this charter:** Claude Opus 4.7 (previous session, with Jesse)
- **Intended reader:** Future Claude sessions opened against this repo + Jesse himself

---

## 1. Purpose & Scope

### 1.1 What this repo is

`sgc-swing-trader` is an **independent system** for identifying and managing **swing trades** (multi-day to multi-week holds) intended to **lock in tactical gains** alongside Jesse's primary long-term compounding portfolio.

It is a **sister system** to `sgc-dip-engine`, not a fork, not a submodule, not a branch. It has its own:
- Repository
- Lifecycle
- Release cadence
- Config
- Backtest data
- Risk model

### 1.2 What this repo is NOT

- **NOT a replacement** for the dip engine. The dip engine is the 22-year compounding workhorse; this is a tactical overlay.
- **NOT a high-frequency trading system.** No intraday, no day trading, no options scalping. Holding period: days to weeks.
- **NOT a portfolio system.** It generates trade ideas with conviction scores; Jesse decides allocation manually.
- **NOT to be merged into the dip engine.** Distinct lifecycles. If a swing trader feature later proves useful to dip engine, it's copied/adapted, not merged.

### 1.3 Primary use case

Jesse holds a long-term ISA portfolio (managed by the dip engine). Occasionally the market presents **high-conviction swing opportunities** — names whose risk/reward is asymmetric over a 1-6 week window. Current example: SNDK underwater swing, MU +8% in 4 days. The swing trader helps identify these *prospectively* (not just diagnose them after entry) and gives a structured exit framework.

### 1.4 Secondary use case

**Discovery.** The dip engine watches ~39 tickers Jesse has pre-vetted. The swing trader can scan a broader universe (S&P 500, Nasdaq 100, sector ETFs' top holdings) to surface candidates Jesse hasn't considered.

---

## 2. Historical Context — What Came Before

### 2.1 The dip engine (`sgc-dip-engine`)

**Repo:** `smartbalanced-sgc/sgc-dip-engine`
**Purpose:** Long-term portfolio management for 22-year compounding via monthly DCA into a Trading 212 ISA.
**Schedule:** GitHub Actions cron at 9:30 PM UTC daily.
**Dashboard:** GitHub Pages at `https://smartbalanced-sgc.github.io/sgc-dip-engine/`

#### 2.1.1 Dip engine architecture (file-level map)

```
src/
  main.py                    # Entry point, orchestrates daily run
  config/config.yaml         # SACRED — portfolio weights, thresholds
  data_fetcher.py            # FMP API wrapper + caching
  regime_classifier.py       # NORMAL/MOMENTUM/SQUEEZE_RISK/OVERSOLD_REVERSAL/BREAKDOWN
  hmm_regime.py              # bull/sideways/drawdown (drives MC drift/vol)
  monte_carlo.py             # 50k-path GBM, 500-day close history
  garch_model.py             # GARCH(1,1) volatility forecasting
  macro_regime.py            # VIX + SPY trend → risk_on/neutral/risk_off
  validators.py              # Data quality gates
  execution_logic.py         # BUY/WAIT signal modulation by regime
  dashboard_generator.py     # HTML output to docs/index.html
research/                    # Backtest harness (yfinance-based)
docs/handover/               # Lossless session-handover docs
tools/
  swing_analyzer.py          # MC sister tool (older, simpler)
  swing_analyzer_analytic.py # God-mode v4 multi-target conviction scanner
```

#### 2.1.2 Two distinct regime concepts (CRITICAL — do not conflate)

- **`hmm_regime.py`** → `bull` / `sideways` / `drawdown`. Used by `monte_carlo.py` to set drift and volatility multipliers per regime.
- **`regime_classifier.py`** → `NORMAL` / `MOMENTUM` / `SQUEEZE_RISK` / `OVERSOLD_REVERSAL` / `BREAKDOWN`. Used by `execution_logic.py` to modulate BUY/WAIT signals.

The new system may borrow or fork either, but should not assume they are the same concept.

#### 2.1.3 Macro regime (lift this verbatim if useful)

```python
# src/macro_regime.py
def classify_macro_regime(indicators):
    vix = indicators['vix']
    spy_trend = indicators['spy_trend']
    if vix > 25 or spy_trend < -0.03:
        return 'risk_off'
    elif vix < 15 and spy_trend > 0.02:
        return 'risk_on'
    return 'neutral'

def get_macro_adjustments(regime):
    return {
        'risk_on':  {'corr_mult': 0.8, 'vol_mult': 0.9},
        'neutral':  {'corr_mult': 1.0, 'vol_mult': 1.0},
        'risk_off': {'corr_mult': 1.3, 'vol_mult': 1.4}
    }.get(regime, {'corr_mult': 1.0, 'vol_mult': 1.0})
```

Fetched via FMP `quote` endpoint for `^VIX` and `SPY` (uses `priceAvg50` for trend).

#### 2.1.4 Data sources (production constraints)

- **FMP Starter plan only.** Endpoints requiring higher tiers return 402; do NOT call them. Known 402s: `press-releases`, anything under `.L` / `.GB` / `.MI` tickers (use Eulerpool for `LDO.MI`).
- **`grades-consensus`** (NOT `upgrades-downgrades-consensus`) for analyst grades.
- **`insider-trading/search`** with local P+S filtering (NOT `insider-trading-statistics` — returns empty).
- **`historical-price-eod/full`** (NOT `historical-price-full`).
- **`historical-sector-performance`** requires `sector` param + `from`/`to` dates, else returns stale 2024 data.
- **Anthropic SDK:** lazy init only inside `get_client()`. Module-level init causes import-time crashes.
- **yfinance dividend yield is in percent, not decimal** — a documented correction exists in the dip engine code.

#### 2.1.5 Sacred decisions (locked in dip engine — informational here)

- Portfolio Constitution v7.1 governs stock selection
- Grinold-Kroner correction: `Buyback = 0%` in Step 3 (already embedded in EPS growth)
- Non-GAAP EPS for AVGO, CEG, VST
- MU uses 30/50/20 cyclical probability weighting
- Monte Carlo uses 500 days of **close-to-close** history. Intraday/premarket explicitly rejected May 13, 2026.
- Backtest uses close prices (do not change — invalidates history)

These are dip-engine canon, not swing-trader canon. The new system can adopt different defaults if justified.

### 2.2 The SNDK swing analyzer (`tools/swing_analyzer_analytic.py`)

A god-mode v4 tool built inside the dip engine repo over ~50 hours of iteration. It exists to answer one question every day: **should Jesse hold, trim, or exit his SNDK swing position?**

#### 2.2.1 v4 framework (LOCKED — see `docs/handover/SNDK_SWING_TOOL.md` in parent repo)

- **Multi-target conviction scan** (not EV-cushion math) — scans 5 targets above current price, reports the highest target where conviction ≥ 65%.
- **Two anchors:** `X_safe` (CI lower bound ≥ threshold) and `X_aggressive` (point estimate ≥ threshold). NO `X_stretch`.
- **65% conviction threshold** — locked, not 60%, not 70%.
- **Sigma triangulation** across 5 anchors: GARCH spot + realized vol (30/60/90d) + yfinance options IV (liquidity-gated).
- **9 drift signals:** historical drift, analyst PT, sector momentum, macro regime, insider activity, AI synthesis, short interest, peer relative strength, sector decoupling.
- **Bayesian posterior smoothing** across daily runs (uniform prior → posterior, posterior becomes next day's prior).
- **AI synthesis** via Claude Opus 4.7 + web_search tool with quality gates (PRIMARY > REPUTABLE > SPECULATIVE > NONE_FOUND; LOW conf halves weight; NONE_FOUND drops signal).
- **Path-dependent metrics** shown separately, never synthesized into a single score: max drawdown, time-to-target distribution, panic-touch probability.
- **Reliability components** shown raw — never collapsed into a "trust score."
- **Hysteresis warning** on suspicious verdict flips between runs.

#### 2.2.2 Why this matters for the new repo

Read the LOCKED design decisions but **do not blindly copy them**. The SNDK tool is a **single-position exit decision tool**. The new system is broader — discovery, entry, sizing, exit. Different decisions may be appropriate at different stages of the pipeline (see Section 4).

#### 2.2.3 EV math: open question for the new system

A critical design tension surfaced in the SNDK build:

- **EV-cushion framework was EXPLICITLY REMOVED** from the SNDK tool because it didn't match Jesse's exit decision rule. Jesse decides exit by **target conviction**, not by expected value.
- **However**, this rejection was **scope-specific** (single-position underwater swing exit decision). For the new system, EV math may very well be the right tool for:
  - **Discovery ranking:** "of 500 candidates, which have the highest expected return?"
  - **Entry timing:** "is the entry price a positive-EV bet vs alternatives?"
  - **Position sizing:** Kelly criterion or fractional-Kelly off expected returns and edge
  - **Portfolio allocation:** budget across multiple swing candidates

The new session should **start open-minded** on EV. Don't carry forward an "EV is bad" bias from the SNDK context. Each pipeline stage gets its own framework decision (Section 4).

---

## 3. Core Frameworks Available to Inherit

These are tools the SNDK build proved out. The new session may use, adapt, or reject any of them.

### 3.1 Monte Carlo simulation
- 50,000-path GBM as default, 500-day close-to-close calibration
- Drift = recent mean log return; vol = GARCH(1,1) spot or realized vol
- Macro multipliers per regime (Section 2.1.3)
- Use for: probability of touching target, path-dependent metrics

### 3.2 GARCH(1,1) volatility
- Spot vol forecast for ≤30-day horizons
- α+β heuristic for persistence
- yfinance options IV as liquidity-gated cross-check

### 3.3 Sigma triangulation
- 5 anchors: GARCH + 30d/60d/90d realized + options IV
- Take median or weighted mean; report dispersion as input to reliability

### 3.4 Bayesian posterior smoothing
- Treat each daily run's conviction as evidence; smooth across days
- Prior on day 1 = uniform; posterior on day N = prior on day N+1
- **Known artifact:** std narrows on same data across same-day re-runs. Documented in SNDK handover; mitigated by daily snapshot persistence.

### 3.5 AI synthesis (Claude + web_search)
- Opus 4.7 with `web_search` tool
- Source quality hierarchy: PRIMARY > REPUTABLE > SPECULATIVE > NONE_FOUND
- Quality gates: NONE_FOUND drops signal; SPECULATIVE+single source halves weight; LOW conf halves weight
- Cost-aware: cache results, don't re-query on same-day re-runs

### 3.6 Multi-target conviction scan
- Scan N targets; report highest where conviction ≥ threshold
- Two anchors: safe (CI lower ≥ threshold) and aggressive (point ≥ threshold)
- Lifted from SNDK; appropriate where the decision is "which target to aim for"

### 3.7 Closed-form reflection-principle formulas
- P(touch) for GBM with known μ, σ, T
- Avoids MC noise when an analytic answer exists

### 3.8 Earnings calendar integration
- FMP earnings-calendar endpoint
- Probability band shown separately when earnings falls in horizon
- Volatility scaling for earnings-window simulations

---

## 4. Framework Choice Per Pipeline Stage (Prescriptive)

The new system has multiple stages, and the **right framework differs per stage**. This is prescriptive guidance, not a rigid mandate — the new session should pressure-test each choice.

| Stage | Likely right framework | Why |
|---|---|---|
| **Discovery** (universe → candidates) | EV ranking + factor screens | EV math IS appropriate here — you're choosing among many alternatives. Rank by E[return] × P(success). |
| **Entry timing** (candidate → entered) | MC + drift signals + EV vs benchmark | "Is this a positive-EV bet vs cash / SPY today?" |
| **Position sizing** (entered → $ amount) | Fractional Kelly off edge + variance | Kelly criterion appropriate; cap at fractional (¼ or ⅓ Kelly) for ruin avoidance. |
| **Hold/trim/exit** (position → action) | Multi-target conviction scan (SNDK v4 style) | SNDK proved EV math UNHELPFUL here — Jesse decides by target conviction. Reuse this. |
| **Portfolio overlay** (multi-position) | Correlation-aware risk budget | Macro regime multipliers; don't run uncorrelated MC per position. |

**Key insight:** the SNDK rejection of EV math was about **exit decisions on a single underwater position**. It does NOT generalize to discovery, sizing, or entry timing. Stay open-minded.

---

## 5. Recommended Pipeline Architecture (State Machine)

```
[Universe]
   ↓ factor screens + EV rank
[Candidates] (~50)
   ↓ deeper MC + drift signals + sigma triangulation
[Watchlist] (~10)
   ↓ entry-trigger rules + Bayesian conviction
[Entered Positions]
   ↓ daily check: hold / trim / exit (multi-target conviction scan)
[Exited Positions] → realized P&L → backtest update
```

Each transition is a separate decision with its own framework. Don't try to build one model that does everything.

---

## 6. Six Design Tensions (with Recommendations)

### 6.1 Universe size: narrow (curated) vs broad (full S&P 500)?
**Recommended:** Start broad for discovery, but apply aggressive factor screens (liquidity, market cap, sector diversity) to get to ~50 candidates fast. Narrow universes miss opportunities; broad universes without screens drown in noise.

### 6.2 Hold period: fixed (e.g. 4 weeks) vs adaptive?
**Recommended:** Adaptive, but with **explicit max hold** (e.g. 8 weeks). Use conviction decay or target hit as primary exit triggers; max hold as safety valve.

### 6.3 Signal source: pure quant vs AI-augmented?
**Recommended:** Hybrid. Quant for discovery and sizing (cheap, deterministic, backtestable). AI for synthesis on shortlist only (expensive, valuable for catalysts and qualitative).

### 6.4 Risk model: per-position vs portfolio-level?
**Recommended:** Both, but **portfolio-level dominates**. A swing book of 5 uncorrelated names has very different risk than 5 mega-cap tech names. Macro regime multipliers must scale correlations.

### 6.5 Backtest fidelity: close-only vs OHLC?
**Recommended:** OHLC for swing trader (intraday matters more here than for monthly DCA). The dip engine's close-only rule is dip-engine-specific.

### 6.6 Cost model: assume frictionless vs realistic?
**Recommended:** Realistic from day 1. Trading 212 fees, FX conversion (Jesse trades USD names from GBP ISA), spread, slippage. Frictionless backtests of swing strategies are systematically optimistic.

---

## 7. Anti-Patterns (Inherited from SNDK Build — ~50 hours of pain)

1. **Paternalistic threshold bumps.** When Jesse says "65% threshold," don't quietly add a safety margin "just to be safe." Use exactly what he says.
2. **Audit-loop pattern.** Don't iterate the same design 8 times. Propose, get approval, lock, move on.
3. **Silently disabled features.** If a check is broken, fix it or remove it — never let it silently swallow exceptions.
4. **Triple-backtick code fences inside heredocs/strings being delivered to Jesse.** Breaks paste. Use list-of-strings joined with `\n`, OR deliver as a file.
5. **Untested endpoint assumptions.** TEST FMP endpoints via curl. Don't assume from docs.
6. **Sector endpoint multi-exchange bug.** Always filter by exchange.
7. **Module-level Anthropic init.** Lazy init only.
8. **yfinance dividend-yield percent/decimal confusion.** Always verify the unit.
9. **Conflating regime concepts.** HMM regime ≠ classification regime. They serve different downstream logic.
10. **Synthesizing reliability components into a single "trust score."** Show them raw.
11. **Same-day Bayesian re-runs without snapshot persistence.** Std narrows artificially. Persist daily snapshot.
12. **Merging exploratory branches to main.** Especially for time-bounded tools (like SNDK exit). Delete-don't-merge.
13. **Building elaborate workflows when a simple prompt would do.** Overengineering is the default failure mode.
14. **Pretending to understand.** If you don't get it, say so. Honest pushback > sycophancy.
15. **Skipping the 3-conversation protocol** (Section 9) and jumping to code.

---

## 8. Design Principles

- **Minimum complexity for the current task only.** Don't build for hypotheticals.
- **Quality and conviction prevail.** No volume-for-volume's-sake metrics.
- **Surgical diffs preferred** over full-file rewrites for small changes.
- **Wave-by-wave approval** — Jesse approves each wave before the next begins.
- **One change at a time.**
- **End every complete response with `#End`** so Jesse knows you didn't get truncated.
- **Annotation Mandate:** every logical check cites the design section it implements.
- **Caveman responses by default.** Clear, concise, surgical.

---

## 9. Approval Gates (NEVER VIOLATE)

Before ANY of the following, get explicit approval from Jesse:

1. Any `git commit` — show diff first, get "go", then commit
2. Any `git push` — run local tests first, get "push it", then push
3. Any change to >3 files in one shot
4. Any new dependency in `requirements.txt`
5. Any change to `.github/workflows/`
6. Any change that touches the future "sacred" files (TBD by the new session; lock them as you build)

**"Accept edits" mode:** explicitly turn OFF for this repo. SGC swing trades involve real money.

**Phrase patterns:**
- "Stop" / "Wait" → halt immediately
- "Restate" → repeat the task in 1-2 sentences before acting
- "Caveman" → terse, surgical
- "Don't be a yes-man" → pushback expected

---

## 10. The 3-Conversation Pre-Code Protocol

Before writing ANY code in this repo, the new session must complete THREE conversations with Jesse:

### Conversation 1: Scope & Use Case Refinement
- What exactly are the swing trades? (timeframe, target return, max loss)
- What's the universe?
- How does this interact with the dip engine portfolio? (do swing positions ever overlap with dip engine names?)
- What's the success metric? (Sharpe, total return, hit rate, drawdown)
- What's the failure mode that scares Jesse most?

### Conversation 2: Pipeline Stage Decisions
- For each stage (discovery, entry, sizing, exit, portfolio), what framework?
- Where does EV math help? Where does it hurt?
- What gets lifted from the SNDK tool? What gets rebuilt?
- What gets lifted from the dip engine? What gets rebuilt?
- AI synthesis: in or out? At which stages?

### Conversation 3: Architecture & Sacred Decisions
- Repo layout
- Config structure
- Sacred files (locked once chosen)
- Backtest data strategy
- Deployment: local-only, GitHub Actions cron, or webhook?
- First feature to ship (must be minimal — proof of plumbing, not full system)

Only after all three conversations + Jesse's explicit "go build" should code be written.

---

## 11. Stakes & Operating Context

- Jesse is a UK retail investor running real money
- Primary portfolio: 22-year compounding via monthly DCA into Trading 212 ISA (managed by dip engine)
- Swing trades are tactical overlay on top of primary portfolio
- **Real money. Wrong code = lost money.** Approval gates are non-negotiable.
- Jesse runs code on his MacBook locally (`/Users/jesse/sgc/sgc-swing-trader` expected path)
- Python 3.10
- Env vars in `~/.zshrc`: `FMP_API_KEY`, `ANTHROPIC_API_KEY`
- GitHub Secrets needed (if Actions used): same as above

---

## 12. Repo Scaffold (Recommended Starting Point)

```
sgc-swing-trader/
  CLAUDE.md                       # mirror this charter's rules, condensed
  README.md                       # what this is, how to run it
  FOUNDING_CHARTER.md             # THIS DOC
  requirements.txt                # start minimal: requests, pandas, numpy
  src/
    main.py                       # entry point (not yet built)
    config/
      config.yaml                 # sacred once locked
    data_fetcher.py               # FMP wrapper (lift from dip engine)
  docs/
    handover/
      01_SESSION_CONTEXT.md       # update each session
      02_BUILD_HISTORY.md         # append each session
      03_DECISIONS.md             # locked design decisions
  research/                       # backtest harness
  tests/
  .github/workflows/              # CI; cron later
  .gitignore                      # standard Python + data/*.csv
```

Do not create files speculatively. Build only what conversation 1-3 mandates.

---

## 13. Appendix A — Code Patterns Worth Lifting (Verbatim)

### A.1 FMP fetch with caching + 402 early-return

```python
import os, time, json, requests
from pathlib import Path

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
FMP_KEY = os.environ["FMP_API_KEY"]
FMP_402 = set()  # endpoints/symbols known to 402 — skip

def fmp_get(endpoint, symbol_or_path, **params):
    key = f"{endpoint}:{symbol_or_path}"
    if key in FMP_402:
        return None
    cache_file = CACHE_DIR / f"{endpoint}_{symbol_or_path}.json".replace("/", "_")
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 3600:
        return json.loads(cache_file.read_text())
    url = f"https://financialmodelingprep.com/stable/{endpoint}"
    p = {"symbol": symbol_or_path, "apikey": FMP_KEY, **params}
    r = requests.get(url, params=p, timeout=30)
    if r.status_code == 402:
        FMP_402.add(key)
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    cache_file.write_text(json.dumps(data))
    return data
```

### A.2 Macro regime (lift verbatim from dip engine §2.1.3)

### A.3 GARCH(1,1) spot vol pattern (lift from dip engine `garch_model.py` — read parent repo to see actual impl)

### A.4 Lazy Anthropic client init

```python
_client = None
def get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client
```

---

## 14. Appendix B — Key References

- Parent repo: `https://github.com/smartbalanced-sgc/sgc-dip-engine` (Jesse has access; new Claude session does NOT via MCP)
- Dip engine dashboard: `https://smartbalanced-sgc.github.io/sgc-dip-engine/`
- SNDK tool source: `tools/swing_analyzer_analytic.py` in parent repo (~2656 lines)
- SNDK handover doc: `docs/handover/SNDK_SWING_TOOL.md` in parent repo
- FMP docs: `https://site.financialmodelingprep.com/developer/docs`
- yfinance: `https://github.com/ranaroussi/yfinance`
- Anthropic SDK: `https://github.com/anthropics/anthropic-sdk-python`

---

## 15. First-Session Bootstrap Prompt (for Jesse to send to new Claude)

When Jesse opens claude.ai/code on this repo for the first time, the first message to send is:

> Read `FOUNDING_CHARTER.md` in full. Confirm you understand:
> 1. This repo is independent — does NOT merge to dip engine.
> 2. You must complete the 3-conversation protocol before writing any code.
> 3. Approval gates apply.
> 4. The SNDK rejection of EV math was scope-specific; stay open-minded for this system.
>
> Then start Conversation 1: Scope & Use Case Refinement. Ask me the questions from Section 10.

---

#End
