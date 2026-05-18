"""FMP endpoint probe — empirically test which /stable/ endpoint names
work for your API key across EVERY data type the v1 build needs.

The dip-engine canonical pattern (FOUNDING_CHARTER.md §A.1) tells us the
URL shape but not the exact endpoint names. Worse, FMP renames endpoints
periodically and gates many behind premium plans without saying so in
docs. Rather than guess, this script tries the candidates we care about
for the full v1 pipeline and prints a clean matrix of what works on
your specific FMP_API_KEY.

The matrix is the SINGLE INPUT to the next phase of the build. Anything
that returns 402 ("not on your plan") is a hard constraint we need to
design around — either by upgrading the plan, switching the data source
for that one field, or descoping that field from the dashboard panel
that depends on it.

Usage (from repo root, with FMP_API_KEY exported):
    python tools/probe_fmp.py             # uses NVDA as the test ticker
    python tools/probe_fmp.py ANET        # probe a different ticker
    python tools/probe_fmp.py NVDA --json # machine-readable output

Pipeline-step dependencies (what each enables):
  Step 3 (catalyst):    earnings_calendar, news, press_releases,
                        analyst_estimates, options_implied_move (gated
                        on most plans — historical-realized fallback OK)
  Step 5 (fair value):  income_statement, balance_sheet, cash_flow,
                        key_metrics, ratios, analyst_targets
  Step 8 (LLM thesis):  news + press_releases (qualitative input)
  Step 1 (already built): profile + historical-price-eod/full

Output: per endpoint a row with HTTP status, body preview, and the
pipeline step(s) that depend on it. Plus a final summary grouped by
pipeline step (so you can immediately see which steps are unblocked
vs. partially-blocked vs. fully-blocked).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15


@dataclass
class Candidate:
    """One endpoint to probe.

    enables: which pipeline step(s) this endpoint unblocks. Used to
    bucket the final summary.
    purpose: what the endpoint is supposed to return (plain English).
    extra:   query-params to pass alongside symbol + apikey.
    """
    path: str
    enables: list
    purpose: str
    extra: dict = field(default_factory=dict)


# Probing strategy: for each data type we need, list 2-3 candidate
# endpoint names that FMP has used historically (the names drift).
# First match wins per data type — we don't need all variants to work,
# just one.

CANDIDATES = [
    # --- Already known-good baseline (proves auth is OK) -----------------
    Candidate("quote",                        ["baseline"],         "real-time quote — known-good Starter baseline"),
    Candidate("profile",                      ["baseline"],         "company profile — confirmed working in step 1"),
    Candidate("historical-price-eod/full",    ["baseline"],         "daily OHLCV — confirmed working in step 1", {"from": "2024-01-01"}),

    # --- Step 3 (catalyst detection) -------------------------------------
    # Earnings calendar — when's the next print?
    Candidate("earnings-calendar",            ["step 3"],           "next earnings dates across the market", {"from": "2026-05-01", "to": "2026-08-31"}),
    Candidate("earnings",                     ["step 3"],           "ticker-specific earnings history + next date"),
    Candidate("earning_calendar",             ["step 3"],           "alt earnings calendar name"),
    Candidate("historical-earnings",          ["step 3"],           "past earnings dates + actuals/estimates"),
    Candidate("earnings-surprises",           ["step 3"],           "past quarters: actual vs estimate (drives historical reactions table)"),

    # Dividend calendar — ex-div dates
    Candidate("dividends-calendar",           ["step 3"],           "upcoming ex-div dates across market", {"from": "2026-05-01", "to": "2026-08-31"}),
    Candidate("dividends",                    ["step 3"],           "ticker-specific dividend history"),
    Candidate("historical-dividends",         ["step 3"],           "alt dividend history name"),

    # News (often gated on Starter)
    Candidate("stock-news",                   ["step 3", "step 8"], "ticker-specific news headlines", {"from": "2026-05-01"}),
    Candidate("news/stock",                   ["step 3", "step 8"], "alt news endpoint name", {"from": "2026-05-01"}),
    Candidate("news-stock-latest",            ["step 3", "step 8"], "another alt news name"),
    Candidate("press-releases",               ["step 3", "step 8"], "official press releases (usually less gated than news)"),
    Candidate("press-releases-latest",        ["step 3", "step 8"], "alt press-release name"),

    # Analyst data — price targets, estimate revisions
    Candidate("price-target-consensus",       ["step 3", "step 5"], "current analyst PT consensus (median, high, low)"),
    Candidate("price-target-summary",         ["step 3", "step 5"], "alt price-target endpoint"),
    Candidate("price-target",                 ["step 3", "step 5"], "individual analyst targets"),
    Candidate("analyst-estimates",            ["step 3", "step 5"], "EPS/revenue estimates forward"),
    Candidate("analyst-stock-recommendations",["step 3"],           "buy/hold/sell ratings"),
    Candidate("upgrades-downgrades",          ["step 3"],           "recent analyst rating changes"),
    Candidate("upgrades-downgrades-consensus",["step 3"],           "consensus rating changes (often un-gated)"),

    # Options / implied vol (almost always premium — but probe anyway)
    Candidate("options-chain",                ["step 3"],           "options chain — needed for implied-move calc"),
    Candidate("historical-options",           ["step 3"],           "historical options — for past-event implied moves"),

    # --- Step 5 (fair value triangulation) -------------------------------
    Candidate("income-statement",             ["step 5"],           "quarterly/annual income statement", {"period": "annual"}),
    Candidate("balance-sheet-statement",      ["step 5"],           "balance sheet", {"period": "annual"}),
    Candidate("cash-flow-statement",          ["step 5"],           "cash flow statement", {"period": "annual"}),
    Candidate("key-metrics",                  ["step 5"],           "key fundamentals (P/E, P/S, ROE, etc.)", {"period": "annual"}),
    Candidate("ratios",                       ["step 5"],           "valuation ratios", {"period": "annual"}),
    Candidate("enterprise-values",            ["step 5"],           "EV history (for EV/EBITDA multiples)", {"period": "annual"}),
    Candidate("financial-growth",             ["step 5"],           "growth rates (revenue, earnings)", {"period": "annual"}),
    Candidate("dcf",                          ["step 5"],           "FMP's own DCF estimate — useful as one input"),
    Candidate("advanced-dcf",                 ["step 5"],           "alt DCF endpoint"),

    # --- Misc useful for step 3 narratives & risk panels -----------------
    Candidate("insider-trading",              ["step 3"],           "insider transactions (e.g. CFO sold $2.1M signal)"),
    Candidate("short-interest",               ["step 3"],           "short interest ratio + days-to-cover (CSCO 1.47% v17)"),
    Candidate("sec-filings",                  ["step 3"],           "recent SEC filings (8-K, 10-Q triggers)"),
    Candidate("stock-peers",                  ["step 5"],           "sector peers — for relative-multiple FV"),
]


def interpret(status):
    return {
        200: "OK",
        401: "auth failed (bad key)",
        402: "NOT ON YOUR PLAN",
        403: "forbidden (likely deprecated endpoint name)",
        404: "endpoint does not exist",
        429: "rate-limited (retry later)",
    }.get(status, f"unexpected (status={status})")


def probe_one(api_key, ticker, c):
    params = {"symbol": ticker, "apikey": api_key, **c.extra}
    url = f"{BASE}/{c.path}"
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        preview = r.text[:140].replace("\n", " ").replace("\r", " ")
        status = r.status_code
        # Some endpoints return 200 with empty list/object — flag as soft-OK
        # so we don't mistake "endpoint works but no data for this symbol"
        # for "endpoint actually returns useful data".
        if status == 200 and (r.text.strip() in ("[]", "{}") or len(r.text.strip()) < 5):
            soft = " (empty — endpoint works but no data for this symbol)"
            ok = False
        else:
            soft = ""
            ok = (status == 200)
        return {
            "endpoint": c.path,
            "enables": c.enables,
            "purpose": c.purpose,
            "status": status,
            "interpretation": interpret(status) + soft,
            "preview": preview,
            "ok": ok,
        }
    except requests.RequestException as e:
        return {
            "endpoint": c.path,
            "enables": c.enables,
            "purpose": c.purpose,
            "status": 0,
            "interpretation": f"network error: {e}",
            "preview": "",
            "ok": False,
        }


def render_human(results):
    print(f"\n{'='*120}")
    print("Per-endpoint results:")
    print(f"{'='*120}\n")
    print(f"  {'status':<7} {'endpoint':<38} {'enables':<22} {'interpretation':<48}")
    print(f"  {'-'*6:<7} {'-'*37:<38} {'-'*21:<22} {'-'*47:<48}")
    for r in results:
        marker = " ← WORKS" if r["ok"] else ""
        enables = ", ".join(r["enables"])
        print(f"  [{r['status']:<4}] {r['endpoint']:<38} {enables:<22} {r['interpretation']:<48}{marker}")

    print(f"\n{'='*120}")
    print("Summary by pipeline step:")
    print(f"{'='*120}\n")

    steps = ["baseline", "step 3", "step 5", "step 8"]
    for step in steps:
        relevant = [r for r in results if step in r["enables"]]
        if not relevant:
            continue
        ok = [r for r in relevant if r["ok"]]
        not_on_plan = [r for r in relevant if r["status"] == 402]
        deprecated = [r for r in relevant if r["status"] in (403, 404)]
        print(f"  {step.upper():<12}  {len(ok)}/{len(relevant)} endpoint(s) working")
        for r in ok:
            print(f"     ✓  {r['endpoint']:<38} {r['purpose']}")
        for r in not_on_plan:
            print(f"     $  {r['endpoint']:<38} GATED — {r['purpose']}")
        for r in deprecated:
            print(f"     ?  {r['endpoint']:<38} deprecated/wrong name — {r['purpose']}")
        print()

    # Project-level call.
    step3 = [r for r in results if "step 3" in r["enables"]]
    step5 = [r for r in results if "step 5" in r["enables"]]
    step3_ok = [r for r in step3 if r["ok"]]
    step5_ok = [r for r in step5 if r["ok"]]
    print(f"{'='*120}")
    print("Project-level read:")
    print(f"{'='*120}\n")
    if not step3_ok:
        print("  ✗ Step 3 (catalyst): NO endpoints working. Need to upgrade plan OR pick a different data source")
        print("    (Claude WebSearch as fallback for news + earnings dates is a viable Plan B).")
    elif len(step3_ok) >= 3:
        print(f"  ✓ Step 3 (catalyst): {len(step3_ok)} endpoints working — proceed with full catalyst panel.")
    else:
        print(f"  △ Step 3 (catalyst): {len(step3_ok)} endpoint(s) working — partial. Build with what's available,")
        print("    descope gated fields, document the gap.")
    if not step5_ok:
        print("  ✗ Step 5 (fair value): NO fundamentals endpoints working. Will need to descope FV panel")
        print("    to multiples-only (using profile + historical price) or upgrade plan.")
    elif len(step5_ok) >= 4:
        print(f"  ✓ Step 5 (fair value): {len(step5_ok)} endpoints working — full multi-method triangulation possible.")
    else:
        print(f"  △ Step 5 (fair value): {len(step5_ok)} endpoint(s) working — partial. Build a 1–2 method FV")
        print("    instead of 3-method. Document which methods got dropped and why.")
    print()
    print("  Paste this output back into chat and the next session will wire steps 3, 5, 8 to whatever works.")


def main():
    parser = argparse.ArgumentParser(description="Probe FMP /stable/ endpoints for v1 pipeline.")
    parser.add_argument("ticker", nargs="?", default="NVDA")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        print("ERROR: FMP_API_KEY not set in environment.", file=sys.stderr)
        return 2

    ticker = args.ticker.upper()
    if not args.json:
        print(f"Probing {len(CANDIDATES)} FMP /stable/ endpoints with ticker={ticker}...")

    results = [probe_one(api_key, ticker, c) for c in CANDIDATES]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    render_human(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
