"""FMP endpoint probe — empirically test which /stable/ endpoint names
work for your API key.

The dip-engine canonical pattern (FOUNDING_CHARTER.md §A.1) tells us the
URL shape but not the exact endpoint names for every data type we need.
Rather than guess from outdated docs, this script tries the candidates we
care about (profile, historical prices) plus a known-good baseline
(`quote`, per charter §3) so you can see the HTTP status of each and
pick the winners.

Usage (from repo root, with FMP_API_KEY exported):
    python tools/probe_fmp.py             # uses NVDA as the test ticker
    python tools/probe_fmp.py ANET        # probe a different ticker

Output: one line per candidate endpoint with the HTTP status, the first
~120 chars of the response body, and a quick interpretation.
"""

from __future__ import annotations

import os
import sys

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15

# (endpoint_path, extra_query_params_dict, what_it_should_return)
CANDIDATES: list[tuple[str, dict, str]] = [
    ("quote", {}, "baseline — known to work on Starter per charter"),
    ("profile", {}, "company profile (sector, market cap, etc.)"),
    ("company-profile", {}, "alt profile endpoint name"),
    ("historical-price-eod/full", {"from": "2024-01-01"}, "daily OHLCV (full)"),
    ("historical-price-eod/light", {"from": "2024-01-01"}, "daily date+close (lighter payload)"),
    ("historical-price-full", {"from": "2024-01-01"}, "legacy historical name (might still work on stable)"),
    ("historical-chart/1day", {"from": "2024-01-01"}, "intraday-style 1-day bars"),
]


def interpret(status: int) -> str:
    return {
        200: "OK",
        401: "auth failed (bad key)",
        402: "endpoint not on your plan",
        403: "forbidden — likely wrong endpoint name or deprecated",
        404: "endpoint does not exist",
    }.get(status, "unexpected")


def main() -> int:
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        print("ERROR: FMP_API_KEY not set in environment.", file=sys.stderr)
        return 2

    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"
    print(f"Probing FMP /stable/ endpoints with ticker={ticker}\n")
    print(f"  {'status':<8} {'endpoint':<32} {'interpretation':<32} body_preview")
    print(f"  {'-'*7:<8} {'-'*31:<32} {'-'*31:<32} {'-'*40}")

    winners: list[str] = []
    for endpoint, extra, _purpose in CANDIDATES:
        params = {"symbol": ticker, "apikey": api_key, **extra}
        url = f"{BASE}/{endpoint}"
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            preview = r.text[:120].replace("\n", " ").replace("\r", " ")
            status = r.status_code
        except requests.RequestException as e:
            preview = f"REQUEST FAILED: {e}"
            status = 0

        marker = " <-- WORKS" if status == 200 else ""
        print(f"  [{status:<5}] {endpoint:<32} {interpret(status):<32} {preview}{marker}")
        if status == 200:
            winners.append(endpoint)

    print()
    if winners:
        print(f"Working endpoints: {winners}")
        print("Send this list back to Claude and we'll wire data.py to the winners.")
    else:
        print("No endpoint returned 200 — double-check FMP_API_KEY is valid and")
        print("that your plan includes basic equity data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
