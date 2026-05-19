"""Shared HTTP helper for FMP (Financial Modeling Prep) — our primary
data vendor for US-listed equities.

All FMP calls go through this module to keep the canonical
`/stable/{endpoint}?symbol=…&apikey=…` pattern, error handling, and
plan-gating consistent across the codebase. Per FOUNDING_CHARTER §A.1
the legacy `/api/v3/` pattern returns 403 for Starter-plan keys and
must not be used.

Public API:
    from src.data_sources import fmp
    body = fmp.get("profile", "NVDA")
    body = fmp.get("news/stock", symbols="NVDA", from_="2026-04-19", to="2026-05-19")

Why an explicit `symbol=` arg vs kwargs: most endpoints take exactly
one symbol and this is the hot path. Endpoints that take other shapes
(e.g. `earnings-calendar` with from/to, no symbol) pass `symbol=None`
and use kwargs only.
"""

from __future__ import annotations

import os
import re

import requests

from src import config

FMP_BASE = "https://financialmodelingprep.com/stable"
FETCH_TIMEOUT_SEC = config.THRESHOLDS.engine.fetch_timeout_sec

# Matches `apikey=...` in any string (URL, error message, log line).
# Used to redact the API key from anything we might print or raise —
# defense in depth, because a chat transcript or log file with a leaked
# key can be expensive (rate-limit abuse, billing).
_APIKEY_REGEX = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


def redact(s: object) -> str:
    """Replace `apikey=<value>` with `apikey=REDACTED` in any string.
    Safe to call on non-strings (returns repr)."""
    if not isinstance(s, str):
        s = str(s)
    return _APIKEY_REGEX.sub(r"\1REDACTED", s)


class FMPPlanGatedError(RuntimeError):
    """Raised when FMP returns 402 — endpoint not on the current plan.
    Callers should catch this and skip the endpoint, not retry. The
    data layer caches 402-gated endpoints per FOUNDING_CHARTER §A.1
    so we don't waste calls re-hitting them every run."""


def get(endpoint: str, symbol: str | None = None, **extra) -> object:
    """GET /stable/{endpoint}?symbol={SYMBOL}&apikey={KEY}&...

    Args:
        endpoint: path under /stable/ (e.g. "profile", "news/stock").
        symbol: ticker for endpoints that take one. Pass None for
            calendar/aggregate endpoints (earnings-calendar, etc.)
            which use other params instead.
        **extra: any additional query params. Use trailing underscore
            for Python reserved words: pass `from_="2026-01-01"` to
            send `from=2026-01-01`.

    Returns parsed JSON body (typically list[dict] or dict).

    Raises:
        RuntimeError: if FMP_API_KEY env var is not set.
        FMPPlanGatedError: on 402 (endpoint not on current plan).
        RuntimeError: on 403 (endpoint name wrong / key invalid).
        requests.HTTPError: for other non-200s.
    """
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FMP_API_KEY not set in environment. Export it locally "
            "(`export FMP_API_KEY=...`) or set it as a GitHub Actions "
            "secret for cron runs."
        )

    params: dict[str, object] = {"apikey": api_key}
    if symbol is not None:
        params["symbol"] = symbol
    for k, v in extra.items():
        if v is None:
            continue
        # Strip trailing underscore so Python's `from_` becomes FMP's `from`.
        key = k.rstrip("_")
        params[key] = v

    url = f"{FMP_BASE}/{endpoint}"
    resp = requests.get(url, params=params, timeout=FETCH_TIMEOUT_SEC)

    if resp.status_code == 402:
        raise FMPPlanGatedError(
            f"FMP /{endpoint} returned 402 — not available on your plan. "
            "Per FOUNDING_CHARTER §A.1: do NOT call this endpoint on Starter."
        )
    if resp.status_code == 403:
        raise RuntimeError(
            f"FMP /{endpoint} returned 403 — endpoint name may be wrong, "
            f"or key invalid. Run `python tools/probe_catalyst_data.py NVDA` "
            f"to test endpoint names empirically."
        )
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # requests' default HTTPError message includes the request URL,
        # which contains the apikey query param. Redact before re-raising
        # so the key can't leak into logs, exception traces, or chat
        # transcripts.
        raise requests.HTTPError(redact(str(e)), response=e.response) from None
    return resp.json()
