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

import logging
import os
import random
import re
import time

import requests

from src import config

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"
FETCH_TIMEOUT_SEC = config.THRESHOLDS.engine.fetch_timeout_sec

# Retry config for transient failures (rate-limit + transient 5xx).
# Backtest runs hit FMP ~150+ times per ticker × N sample dates; a
# rate-limit response would crash the whole backtest without retry.
# Production runs are much lighter (~6 calls per ticker per run) but
# can still see transient 5xx after deploys or during FMP maintenance.
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY_SEC = 1.0      # initial backoff
_RETRY_MAX_DELAY_SEC = 30.0      # cap on individual sleep

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
    resp = _request_with_retry(url, params, endpoint)

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


def _request_with_retry(url: str, params: dict, endpoint: str) -> requests.Response:
    """GET with exponential-backoff retry on transient failures
    (429 rate limit + transient 5xx). 402/403 are returned without
    retry - they're permanent on this plan/endpoint and retrying
    would just burn quota.

    Backoff schedule (with ±20% jitter to avoid thundering herd):
      attempt 1: immediate
      attempt 2: ~1s
      attempt 3: ~4s
      attempt 4: ~16s
    Total worst case ~21 seconds before giving up.
    """
    last_resp = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=FETCH_TIMEOUT_SEC)
        except requests.RequestException as e:
            # Network-level failure (DNS, connection refused, timeout).
            # Treat the same as a retryable 5xx.
            if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
                raise
            delay = _backoff_delay(attempt)
            logger.warning(
                f"FMP /{endpoint} network error attempt {attempt+1}/{_RETRY_MAX_ATTEMPTS} "
                f"({type(e).__name__}); sleeping {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        last_resp = resp
        if resp.status_code not in _RETRY_STATUS_CODES:
            # Either 2xx (success), or a "definitive" error like 402/403
            # that retry wouldn't fix. Return for caller to handle.
            return resp

        if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
            # Exhausted retries; return the last response and let the
            # caller raise via raise_for_status().
            return resp

        # Respect Retry-After header if present (RFC-7231); else our
        # exponential backoff. Both 429s and 5xx may include it.
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = min(float(retry_after), _RETRY_MAX_DELAY_SEC)
            except ValueError:
                delay = _backoff_delay(attempt)
        else:
            delay = _backoff_delay(attempt)
        logger.warning(
            f"FMP /{endpoint} returned {resp.status_code} attempt {attempt+1}/"
            f"{_RETRY_MAX_ATTEMPTS}; sleeping {delay:.1f}s"
        )
        time.sleep(delay)

    return last_resp  # type: ignore[return-value]


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. attempt=0 -> ~1s, 1 -> ~4s, 2 -> ~16s."""
    base = _RETRY_BASE_DELAY_SEC * (4 ** attempt)
    jitter = base * random.uniform(-0.2, 0.2)
    return min(_RETRY_MAX_DELAY_SEC, max(0.0, base + jitter))
