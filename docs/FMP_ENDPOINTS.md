# FMP endpoint registry — what's available on Starter

**Plan:** Financial Modeling Prep Starter
**Confirmed:** 2026-05-18 via FMP support chat
**Coverage:** US-listed equities only (`.L`, `.MI`, `.GB` tickers are gated — use
Eulerpool for those if needed, per FOUNDING_CHARTER §2.1.4)

All endpoints follow the canonical pattern (charter §A.1):

```
https://financialmodelingprep.com/stable/{endpoint}?symbol={TICKER}&apikey={FMP_API_KEY}&...
```

402 responses ("not on your plan") should be cached by the data layer and
not re-attempted in subsequent runs (charter §A.1 `FMP_402` set).

---

## Step 1 — Data fetch & sanity (already wired in `src/pipeline/data.py`)

| Data | Endpoint | Used for |
|---|---|---|
| Real-time quote | `quote?symbol=` | Current price, VIX, market context |
| Daily OHLCV (5y) | `historical-price-eod/full?symbol=&from=YYYY-MM-DD` | Price history, all downstream math |
| Company profile | `profile?symbol=` | Sector, industry, market cap, shares outstanding, exchange |
| Sector performance history | `historical-sector-performance?sector=&from=&to=` | Regime context, sector momentum |

---

## Step 3 — Catalyst detection (next to be built)

### Earnings calendar

```
earnings-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD
```

- Use a 2-year backward window to capture last 8 quarters.
- Response includes `date` per earnings event.
- **Caveat:** BMO/AMC timing field was removed due to instability. For
  reaction-percentage computation, use *next-trading-day close vs prior-day
  close* (correct regardless of timing). FMP expects to restore timing
  via an Earnings Transcript endpoint in a future release.

### Analyst rating changes (last 30+ days)

```
grades?symbol=TICKER
```

Response fields per change:
- `date` — date of the rating change
- `gradingCompany` — brokerage firm name (Goldman, Morgan Stanley, etc.)
- `action` — `upgrade` | `downgrade` | `maintained`
- `previousGrade`, `newGrade` — old and new rating labels

**Caveat:** No price target attached to each change. To pair "Goldman
upgraded" with "new PT $290" in the narrative, render two separate
elements: the rating change from `grades`, and the current consensus PT
from `price-target-consensus` (below).

### Current analyst price-target consensus

```
price-target-consensus?symbol=TICKER
```

Fields:
- `targetMedian`, `targetHigh`, `targetLow`, `targetConsensus`
- Calculated from price targets in the last 6 months (latest per firm).

### Analyst-target counts (for the "12 analysts" detail)

```
price-target-summary?symbol=TICKER
```

Fields:
- `lastMonthCount`, `lastQuarterCount`, `lastYearCount`, `allTimeCount`

### News headlines per ticker

```
news/stock?symbols=TICKER&from=YYYY-MM-DD&to=YYYY-MM-DD
```

Or for the broader market-wide feed:

```
news/stock-latest?page=0&limit=20
```

Fields per item: publication date, headline, source name, snippet, article URL.

**Caveat:** Historical news on Starter is limited to **the last 1 year**.
For step-3 catalyst narrative bullets we only need the last 14-30 days,
so this limit is irrelevant for our use case.

### Insider trading (already on Starter per charter)

```
insider-trading/search?symbol=TICKER
```

**Caveat:** Use `/search` variant with local P+S filtering, NOT
`insider-trading-statistics` (returns empty per charter §2.1.4).

### Short interest — NOT AVAILABLE on Starter

FMP support confirmed (2026-05-18): short interest data is not offered
on Starter at all (not just gated — not in the API).

**Workaround:** use `yfinance` library (free). Fields:
- `info['sharesShort']` — current short shares
- `info['shortPercentOfFloat']` — short interest as % of float
- `info['shortRatio']` — days-to-cover
- `info['sharesShortPriorMonth']` — for trend comparison

This matches the existing multi-source pattern (charter §3.3 already
uses yfinance for options IV).

---

## Step 5 — Fair value (multi-method triangulation)

### Annual income statement (5y history)

```
income-statement?symbol=TICKER&period=annual&limit=5
```

Fields:
- `revenue` — top-line
- `operatingIncome` — for operating margin trend
- `netIncome` — for trailing P/E, EPS growth
- `epsDiluted` — for EPS computations

### Annual cash flow statement (5y history)

```
cash-flow-statement?symbol=TICKER&period=annual&limit=5
```

Fields:
- `operatingCashFlow`
- `capitalExpenditure` (note: this IS capex; sign convention may be negative)

**Derived:** FCF = `operatingCashFlow + capitalExpenditure` (capex is
typically reported as a negative outflow in cash-flow statements).

### Forward EPS estimate (annual consensus)

```
analyst-estimates?symbol=TICKER&period=annual
```

Fields:
- `epsAvg` — consensus forward EPS for the next fiscal year
- `numAnalystsEps` — number of contributing analysts (for confidence)

**Caveat:** On Starter, only `period=annual` is supported. Quarterly
forward estimates are gated. For v1 the annual figure is sufficient for
a forward-P/E sanity check against the DCF; quarterly would only enable
trajectory plots which are v1.x scope.

### Sector peers list

```
stock-peers?symbol=TICKER
```

Returns ~5 peer tickers on the same exchange, in the same sector, with
similar market cap. Includes ticker, company name, current price, market
cap.

Bulk variant (single call returns peers for many tickers):

```
stock-peers-bulk
```

Used for relative-multiples comparison — "AMAT forward P/E vs peer
median forward P/E."

---

## Step 8 — LLM thesis synthesis

The LLM uses news + qualitative context. Sources:

- **FMP `news/stock`** (above) — primary structured news source on
  Starter
- **Claude `web_search` tool** — fallback / supplement for deeper
  research, per FOUNDING_CHARTER §3.5. The dip engine uses this for
  qualitative synthesis since FMP `press-releases` is 402-gated.

---

## What's still on the wish list (v1.x candidates)

- **Short interest** (G) — via yfinance for v1; consider Quiver
  Quantitative ($30/mo Hobbyist) in v1.x for off-exchange short volume
  + alt-data signals.
- **Quarterly forward EPS** (E) — gated on Starter; would enable
  trajectory plots.
- **Earnings BMO/AMC timing** (F) — FMP expects to restore via
  Transcript endpoint; not blocking.
- **Per-rating-change price target** (D) — not in `grades` endpoint;
  could be derived by joining `grades` with timestamped
  `price-target-consensus` snapshots if we persist snapshots over time.

---

## How to update this document

When FMP changes a behavior, or a new endpoint becomes available, or we
discover a caveat that's not here:

1. Edit this file directly.
2. Commit with a message starting `FMP endpoint registry:` so the
   history is greppable.
3. If a code path in `src/pipeline/` depends on a now-changed endpoint,
   update the code and reference this doc's commit SHA in the code
   commit message.
