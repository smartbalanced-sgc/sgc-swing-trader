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

### Earnings — use the per-symbol endpoint, NOT the calendar

**Use:** `earnings?symbol=TICKER`

This is FMP's per-symbol "Earnings Report" endpoint. Returns ~100
events per US ticker (full available history + upcoming), one ticker
per call, no pagination cap. Response shape:

```json
{
  "symbol": "NVDA", "date": "2026-05-20",
  "epsActual": null, "epsEstimated": 1.76,
  "revenueActual": null, "revenueEstimated": 78423370000,
  "lastUpdated": "2026-05-18"
}
```

**Do NOT use `earnings-calendar` (cross-ticker calendar).** It's
available on Starter but has properties that make it unusable for our
per-ticker need (confirmed by FMP support and verified empirically
via `tools/probe_catalyst_data.py` 2026-05-19):

1. **4000-event cap per call** — overflowing windows silently drop
   events from the start of the date range. Our 360+90 day window for
   ~6500 US-listed tickers overflows the cap; NVDA's events were
   missing entirely from the response.
2. **90-day maximum date range** per call — broader queries return a
   402 (which is what we hit on 2026-05-18 with a 450-day window).
3. **Historical lookback capped at 1 year** on Starter.

If we ever need a cross-ticker calendar (e.g., a market-wide "what's
reporting tomorrow" sweep), use pagination via `&page=0,1,2,...` and
split the date range into ≤90-day chunks.

**Caveat (both endpoints):** BMO/AMC timing field was removed due to
instability. For reaction-percentage computation, use *next-trading-day
close vs prior-day close* (correct regardless of timing). FMP expects
to restore timing via an Earnings Transcript endpoint in a future
release.

### Analyst data — use BOTH `grades-historical` and `grades`

The strong signal is **the consensus snapshot trend** (`grades-historical`),
not the per-firm action stream (`grades`). On Starter the per-firm
stream is dominated by reiterations (verified empirically: 1109/1109
NVDA entries and 690/690 AMAT entries on `/grades` had
`action: "maintain"` 2026-05-19 — no upgrades or downgrades visible).
FMP support confirmed there is NO per-symbol upgrades/downgrades
endpoint on Starter — the bulk one exists but is Premium-only.

**`grades-historical?symbol=TICKER`** — the consensus snapshot trend
(the signal we actually want):

```json
{
  "date": "2026-05-01", "symbol": "NVDA",
  "analystRatingsStrongBuy": 10, "analystRatingsBuy": 48,
  "analystRatingsHold": 3, "analystRatingsSell": 1,
  "analystRatingsStrongSell": 0
}
```

Returns ~85 monthly snapshots. We use [0] for the current snapshot and
binary-search for the snapshot from 30 days ago to compute a
bullishness MoM delta ("more bullish (+1.2pp vs 30d ago)"). See
`_build_consensus_trend` in `src/pipeline/catalyst.py`.

**`grades?symbol=TICKER`** — per-firm rating actions (kept for the
coverage-intensity proxy):

```json
{
  "date": "2026-05-18", "symbol": "NVDA",
  "gradingCompany": "Wedbush",
  "previousGrade": "Outperform", "newGrade": "Outperform",
  "action": "maintain"
}
```

On Starter this is almost entirely `action: "maintain"` (no upgrades
or downgrades visible). We use the count as a "how many desks weighed
in this month" proxy — high count = high attention. The action enum
is essentially "maintain" everywhere on Starter, so we don't try to
extract upgrade/downgrade signal from it.

**Caveat:** No price target attached to each change. Use
`price-target-consensus` (below) for the current consensus PT
separately.

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

**Resolution: `src/data_sources/short_interest.py`** implements a
four-layer failover chain. yfinance is the primary source (free, all
US-listed equities), hardened for reliable operation on shared CI
infrastructure where Yahoo's anti-bot detection is otherwise hostile.

The hardening + failover strategy:

| Layer | Source | When it fires | What you see |
|---|---|---|---|
| 1a | Fresh disk cache (≤24h old) | Hit before Yahoo. Short interest publishes biweekly so this is generous | `source: cache:fresh` |
| 1b | yfinance with `curl_cffi` (Chrome TLS impersonation), 3-attempt retry with jittered exponential backoff (10s/30s/90s), inter-ticker spacing | Cache miss or stale | `source: yfinance:fresh` |
| 2 | Stale cache (up to 14 days old) | yfinance fails despite retries | `source: cache:stale`, `stale_days: N` shown in UI |
| 3 | FMP float-proxy qualitative label | No cache + yfinance still fails. Requires `current_float_shares` from FMP profile | `source: fmp:float-proxy`, `float_proxy_label: Low/Medium/High` |
| 4 | Marked unavailable with failure reason | All layers exhausted | `source: unavailable`, dashboard renders "data unavailable today (reason)" |

**Why this works on GitHub Actions** (where yfinance normally fails):

1. **`curl_cffi` impersonates Chrome's TLS fingerprint** — Yahoo's JA3-hash check sees us as a real browser, not a CI scraper
2. **24-hour cache means most cron runs don't hit Yahoo at all** — short interest publishes biweekly, so re-fetching same-day is wasted
3. **Retry with jittered backoff** — transient 429s recover; jitter prevents synchronized retries when multiple workers are throttled
4. **Inter-ticker delay (5s ± 20% jitter)** — avoids the burst pattern that triggers rate-limiters
5. **03:00 UTC cron schedule** — quietest globally; already configured in `.github/workflows/nightly.yml`

**Why this is investor-grade even when yfinance dies**: the failover
chain degrades gracefully. Stale data (Layer 2) is still useful for a
biweekly metric. Qualitative float-proxy (Layer 3) preserves the
squeeze-risk signal. Layer 4 marks the gap honestly rather than
crashing or silently returning bad data.

Public API:
```python
from src.data_sources.short_interest import fetch, fetch_batch
result = fetch("NVDA", current_float_shares=24_500_000_000)
batch = fetch_batch(["NVDA", "AMAT", "IONQ"], float_shares_lookup={"NVDA": 24.5e9, ...})
```

Smoke test:
```bash
python -m src.data_sources.short_interest NVDA AMAT IONQ
python -m src.data_sources.short_interest NVDA --no-cache  # force fresh yfinance call
```

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
