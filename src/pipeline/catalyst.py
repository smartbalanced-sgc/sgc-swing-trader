"""Step 3 — Catalyst detection.

Scans for scheduled catalysts (next earnings date, ex-dividend date)
and the recent qualitative-context signals around a ticker (news flow,
analyst rating changes, current price-target consensus, short
interest). Returns the catalyst payload the dashboard's Catalyst panel
renders, plus the proximity/in-horizon flags the conviction engine
consumes for Layer-3 veto logic.

Why "catalyst" is its own pipeline step (vs folded into data or
verdict): swing-trade timing turns on *event distance*. A 40% setup
that becomes ENTER tomorrow flips to WAIT if earnings is 2 sessions
away — that's the proximity_haircut path. The same 40% setup with no
event for 60+ sessions is just a regular ENTER. The catalyst step is
where that distance gets computed once and reused everywhere.

Resilience model — investor-grade graceful degradation:

  Each sub-fetch (earnings calendar, news, analyst grades, price-target
  consensus, short interest) is wrapped in try/except. A failure in
  ONE sub-fetch records an entry under `errors` but does NOT fail the
  whole step. The dashboard shows whatever data was successfully
  fetched; missing pieces render as "data unavailable today (reason)".

  This matches the pattern established in
  src/data_sources/short_interest.py — never silently return bad data,
  never crash the run, always show provenance.

  When `status == "ok"` the snapshot record has every key the dashboard
  expects (with None/empty for fields whose sub-fetch failed). The
  conviction engine reads `proximity_haircut` and `in_horizon_30d` /
  `in_horizon_60d` flags which are always set deterministically from
  whatever scheduled catalyst was found (or computed as "none" if no
  upcoming event was found within the forward_window_days lookahead).

Output schema (matches dashboard expectations in src/dashboard.py
`_render_catalyst` and `_catalyst_headline`):

    {
      "status": "ok",
      "ticker": "NVDA",
      "as_of": "2026-05-19",
      "fetched_at": ISO timestamp,
      "source": "live" | "cache:fresh",
      "errors": [{"sub_fetch": "news", "message": "..."}, ...],

      # --- consumed by dashboard ---
      "next_event": {"type": "earnings", "date": "2026-05-21"} | None,
      "distance_sessions": int | None,
      "options_implied_move_pct": float | None,    # placeholder None for v1
      "historical_reactions": [{"date", "type", "reaction_pct"}, ...],
      "news_bullets": [str, ...],
      "analyst_revisions": {"count", "avg_pt", "trend"} | {},
      "engine_recommendation": str,

      # --- consumed by Layer-3 conviction veto + proximity haircut ---
      "proximity_haircut": float,    # 0.0 if no near catalyst
      "in_horizon_30d": bool,
      "in_horizon_60d": bool,

      # --- supporting data shown in extended view ---
      "short_interest": { ... result from short_interest.fetch ... },
      "price_target_consensus": {"median": float, "high": float, ...} | None,
    }

CLI smoke test (requires FMP_API_KEY exported):

    python -m src.pipeline.catalyst NVDA
    python -m src.pipeline.catalyst NVDA AMAT IONQ
    python -m src.pipeline.catalyst NVDA --refresh   # bypass cache
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

import pandas_market_calendars as mcal

from src import config
from src.data_sources import fmp, short_interest

logger = logging.getLogger(__name__)

# NYSE calendar — same one data.py uses. Loaded once at import.
NYSE_CAL = mcal.get_calendar("XNYS")

CACHE_DIR = config.DATA_DIR / "cache" / "catalyst"

# Thresholds from config/thresholds.yml
_T = config.THRESHOLDS.catalyst
FORWARD_WINDOW_DAYS = _T.forward_window_days
HISTORY_LOOKBACK_QUARTERS = _T.history_lookback_quarters
NEWS_LOOKBACK_DAYS = _T.news_lookback_days
ANALYST_LOOKBACK_DAYS = _T.analyst_lookback_days
CACHE_TTL_HOURS = _T.cache_ttl_hours

# Conviction-layer haircut bands (Layer-3 vetoes section in YAML)
_PROXIMITY_BANDS = config.THRESHOLDS.conviction.vetoes.catalyst.proximity_haircut_bands

# Horizon lengths for in_horizon flags
HORIZON_30D = config.THRESHOLDS.horizons.primary_days
HORIZON_60D = config.THRESHOLDS.horizons.secondary_days

# Dashboard render config
NEWS_HEADLINES_MAX = config.THRESHOLDS.dashboard.news_headlines_max
EARNINGS_REACTIONS_LOOKBACK = config.THRESHOLDS.dashboard.earnings_reactions_lookback


# ---------- public API ----------


def detect(ticker: str, price_data: dict, tier: str | None = None, refresh: bool = False) -> dict:
    """Detect catalysts for a ticker and return the dashboard-ready payload.

    Args:
        ticker: ticker symbol (uppercase).
        price_data: output of src.pipeline.data.fetch(ticker). Needed
            for the historical earnings-reaction computation and the
            float-shares fallback for short interest.
        tier: anchor tier from watchlist ("A" | "B" | "C"). Currently
            informational — passed through for future tier-specific
            catalyst-override logic (e.g. FDA dates for biotech Tier C).
        refresh: bypass the disk cache and force a fresh fetch.

    Returns a payload dict matching the schema in the module docstring.
    `status` is always "ok" — sub-fetch failures populate `errors` but
    don't fail the step (the dashboard renders gracefully around gaps).
    """
    cached = None if refresh else _load_cache(ticker)
    if cached and _cache_is_fresh(cached):
        cached["source"] = "cache:fresh"
        return cached

    payload: dict = {
        "status": "ok",
        "ticker": ticker,
        "as_of": price_data.get("as_of"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "live",
        "errors": [],
        # Defaults populated below; preset to None/empty so the schema is
        # complete even if every sub-fetch fails.
        "next_event": None,
        "distance_sessions": None,
        "options_implied_move_pct": None,  # v1: placeholder; yfinance options later
        "historical_reactions": [],
        "news_bullets": [],
        "analyst_revisions": {},
        "engine_recommendation": "",
        "proximity_haircut": 0.0,
        "in_horizon_30d": False,
        "in_horizon_60d": False,
        "short_interest": None,
        "price_target_consensus": None,
    }

    # --- Sub-fetch 1: upcoming earnings + historical reactions ---
    earnings_events = _safe(payload, "earnings", _fetch_earnings_events, ticker)
    if earnings_events:
        upcoming = _upcoming_earnings(earnings_events)
        if upcoming:
            payload["next_event"] = {"type": "earnings", "date": upcoming}
            payload["distance_sessions"] = _sessions_until(upcoming)
        payload["historical_reactions"] = _historical_reactions(
            earnings_events, price_data.get("prices", [])
        )

    # --- Sub-fetch 1b: options-implied move (only if upcoming earnings)
    # The market's expected event-day move expressed as the ATM
    # straddle price / spot, for the option expiry nearest after the
    # earnings date. Used as a SECOND variance source for the MC
    # earnings-jump overlay (blended 50/50 with the empirical bootstrap)
    # so the jump distribution reflects both the historical reaction
    # pattern AND the market's current variance forecast.
    if payload["next_event"] is not None:
        current_price = (price_data.get("profile") or {}).get("price")
        if not current_price:
            prices = price_data.get("prices") or []
            current_price = (prices[-1].get("adj_close") or prices[-1].get("close")) if prices else None
        if current_price:
            implied = _safe(
                payload,
                "options_implied_move",
                lambda: _fetch_options_implied_move(
                    ticker, payload["next_event"]["date"], float(current_price)
                ),
            )
            if implied is not None:
                payload["options_implied_move_pct"] = implied["implied_move_pct"]
                payload["options_implied_move_detail"] = implied

    # --- Sub-fetch 2: recent news headlines ---
    news = _safe(payload, "news", _fetch_news, ticker)
    if news:
        payload["news_bullets"] = _news_bullets(news, NEWS_HEADLINES_MAX)

    # --- Sub-fetch 3: analyst data (3 endpoints) ---
    # - `grades`: per-firm rating actions (mostly reiterations on Starter)
    # - `grades-historical`: time-series consensus snapshots (the real
    #   signal — "58 of 62 analysts are bullish, +2 vs last month")
    # - `price-target-consensus`: current median/high/low PT
    grades = _safe(payload, "analyst_grades", _fetch_grades, ticker)
    consensus = _safe(payload, "analyst_consensus", _fetch_grades_consensus, ticker)
    pt = _safe(payload, "price_target_consensus", _fetch_price_target_consensus, ticker)
    if pt:
        payload["price_target_consensus"] = pt
    if grades is not None or consensus is not None:
        payload["analyst_revisions"] = _analyst_revisions_summary(
            grades or [], consensus or [], pt
        )

    # --- Sub-fetch 4: short interest (own multi-layer failover) ---
    float_shares = (price_data.get("profile") or {}).get("shares_outstanding")
    payload["short_interest"] = _safe(
        payload,
        "short_interest",
        lambda: short_interest.fetch(ticker, current_float_shares=float_shares),
    )

    # --- Derived: conviction-layer flags ---
    distance = payload["distance_sessions"]
    if distance is not None:
        payload["proximity_haircut"] = _proximity_haircut(distance)
        payload["in_horizon_30d"] = distance <= HORIZON_30D
        payload["in_horizon_60d"] = distance <= HORIZON_60D

    # --- Engine recommendation paragraph (plain English) ---
    payload["engine_recommendation"] = _build_engine_recommendation(payload, tier)

    _save_cache(ticker, payload)
    return payload


# ---------- safe-call helper ----------


def _safe(payload: dict, sub_fetch_name: str, fn, *args, **kwargs):
    """Run a sub-fetch with try/except. On failure: log to payload['errors']
    and return None. On success: return the result. Never raises."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — broad catch is the point
        payload["errors"].append(
            {
                "sub_fetch": sub_fetch_name,
                "message": str(e),
                "exception_type": type(e).__name__,
            }
        )
        logger.warning(f"catalyst sub-fetch '{sub_fetch_name}' failed: {e}")
        return None


# ---------- FMP fetchers ----------


def _fetch_earnings_events(ticker: str) -> list[dict]:
    """Fetch earnings events for this ticker via the per-symbol
    `/stable/earnings?symbol=X` endpoint.

    Why not `/stable/earnings-calendar`: the cross-ticker calendar caps
    at 4000 events per call (confirmed by FMP support 2026-05-19),
    sorted date-descending. With our 360-day-historical-plus-90-day-
    forward window the daily US earnings universe overflows the cap and
    NVDA's events fall off the end (probe_catalyst_data.py 2026-05-19
    reproduced this — 0 NVDA matches in a 4000-event response).

    `/stable/earnings?symbol=X` is the per-symbol alternative FMP
    support recommended ("Earnings Report"). Returns ~100 events
    spanning all available history + upcoming, no pagination, no cap.
    Filter to the relevant window locally.
    """
    body = fmp.get("earnings", ticker)
    if not isinstance(body, list):
        return []
    # Local windowing — keep events within the historical lookback +
    # forward lookahead so downstream code doesn't choke on ancient bars.
    historical_cutoff = (date.today() - timedelta(days=HISTORY_LOOKBACK_QUARTERS * 95)).isoformat()
    forward_cutoff = (date.today() + timedelta(days=FORWARD_WINDOW_DAYS)).isoformat()
    return [
        e for e in body
        if (e.get("date") or "")[:10] >= historical_cutoff
        and (e.get("date") or "")[:10] <= forward_cutoff
    ]


def _fetch_grades_consensus(ticker: str) -> list[dict]:
    """Fetch the historical analyst-consensus snapshots for a ticker
    via `/stable/grades-historical?symbol=X`.

    Each entry is a snapshot of how many analysts held each rating on
    a given date, e.g.:
      {date, analystRatingsStrongBuy, analystRatingsBuy,
       analystRatingsHold, analystRatingsSell, analystRatingsStrongSell}

    This is a STRONGER signal than counting individual rating changes
    because FMP's per-symbol `/grades` endpoint returns predominantly
    `action: "maintain"` entries (probe 2026-05-19 confirmed 1109/1109
    NVDA entries were maintains, 690/690 AMAT — no upgrades/downgrades
    visible). Watching the consensus snapshot evolve catches the
    aggregate analyst sentiment shift that individual maintains miss.

    Returns newest-first (per FMP convention)."""
    body = fmp.get("grades-historical", ticker)
    return body if isinstance(body, list) else []


def _fetch_news(ticker: str) -> list[dict]:
    from_date = (date.today() - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    to_date = date.today().isoformat()
    body = fmp.get(
        "news/stock",
        symbol=None,
        symbols=ticker,
        from_=from_date,
        to=to_date,
    )
    return body if isinstance(body, list) else []


def _fetch_grades(ticker: str) -> list[dict]:
    body = fmp.get("grades", ticker)
    return body if isinstance(body, list) else []


def _fetch_price_target_consensus(ticker: str) -> dict | None:
    body = fmp.get("price-target-consensus", ticker)
    if isinstance(body, list) and body:
        row = body[0]
    elif isinstance(body, dict):
        row = body
    else:
        return None
    return {
        "median": row.get("targetMedian"),
        "high": row.get("targetHigh"),
        "low": row.get("targetLow"),
        "consensus": row.get("targetConsensus"),
    }


# ---------- yfinance options-chain fetcher (for implied move) ----------


def _fetch_options_implied_move(
    ticker: str, event_date_iso: str, current_price: float
) -> dict | None:
    """Compute the at-the-money straddle's implied event-day move using
    yfinance's options chain.

    Method (industry-standard "straddle-derived implied move"):
      1. Pull all option expiries, pick the EARLIEST that is on or
         after the earnings event (post-event variance is what we want
         - "what does the market think the move will be?").
      2. Pull that expiry's call + put chains.
      3. Find the ATM call and ATM put (strike nearest to current spot).
      4. Compute mid-prices ((bid+ask)/2; fall back to lastPrice if
         bid/ask missing).
      5. Straddle = atm_call_mid + atm_put_mid.
      6. Implied move (as fraction of spot) = straddle / spot.

    Caveats:
      - For tickers with no options or illiquid chains, fails gracefully
        (returns None - MC then uses pure empirical bootstrap).
      - Sanity-clipped to [1%, 50%]; outside that range we assume the
        data is broken (wide spreads, mispriced contracts, stale quotes).
      - yfinance may be rate-limited on shared CI - we use curl_cffi
        TLS impersonation when available, same as short_interest module.

    Returns dict with implied_move_pct + diagnostic fields, or None
    on any failure (caller treats None as "data unavailable, use
    fallback").
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance not installed")

    # Hardened session (matches src/data_sources/short_interest.py
    # pattern). Falls back to default requests if curl_cffi missing.
    session = None
    try:
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate="chrome131")
    except ImportError:
        pass

    y_ticker = yf.Ticker(ticker, session=session) if session else yf.Ticker(ticker)

    try:
        expiries = y_ticker.options
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"yfinance options listing failed: {e}")

    if not expiries:
        return None

    # Pick the earliest expiry on or after the earnings date.
    event_d = date.fromisoformat(event_date_iso[:10])
    target_expiry: str | None = None
    for exp in expiries:
        try:
            exp_d = date.fromisoformat(exp)
        except (ValueError, TypeError):
            continue
        if exp_d >= event_d:
            target_expiry = exp
            break
    if target_expiry is None:
        return None  # All expiries before the event - unusual but possible

    try:
        chain = y_ticker.option_chain(target_expiry)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"yfinance option_chain failed for {target_expiry}: {e}")

    calls = chain.calls
    puts = chain.puts
    if calls is None or puts is None or calls.empty or puts.empty:
        return None

    # ATM = closest strike to spot for each side.
    calls_dist = (calls["strike"] - current_price).abs()
    puts_dist = (puts["strike"] - current_price).abs()
    atm_call = calls.loc[calls_dist.idxmin()]
    atm_put = puts.loc[puts_dist.idxmin()]

    def _mid(row) -> float | None:
        bid = float(row.get("bid", 0) or 0)
        ask = float(row.get("ask", 0) or 0)
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2.0
        last = float(row.get("lastPrice", 0) or 0)
        return last if last > 0 else None

    call_mid = _mid(atm_call)
    put_mid = _mid(atm_put)
    if call_mid is None or put_mid is None:
        return None

    straddle = call_mid + put_mid
    implied_move_pct = straddle / current_price

    # Sanity range — outside [1%, 50%] is almost certainly broken data
    # (stale quotes, wide bid/ask, mispriced expiry).
    if not (0.01 <= implied_move_pct <= 0.50):
        return None

    return {
        "implied_move_pct": implied_move_pct,
        "expiry": target_expiry,
        "atm_call_strike": float(atm_call["strike"]),
        "atm_put_strike": float(atm_put["strike"]),
        "call_mid": call_mid,
        "put_mid": put_mid,
        "straddle": straddle,
        "spot": current_price,
    }


# ---------- transformations ----------


def _upcoming_earnings(events: list[dict]) -> str | None:
    """Return ISO date of the earliest earnings event >= today."""
    today = date.today()
    future = []
    for e in events:
        d_str = e.get("date")
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str[:10])
        except ValueError:
            continue
        if d >= today:
            future.append(d)
    if not future:
        return None
    return min(future).isoformat()


def _sessions_until(target_iso: str) -> int:
    """Count trading sessions from today (exclusive) to target date
    (inclusive). 0 means the event is today or earlier; 1 means next
    trading day."""
    target = date.fromisoformat(target_iso)
    today = date.today()
    if target <= today:
        return 0
    sched = NYSE_CAL.schedule(
        start_date=(today + timedelta(days=1)).isoformat(),
        end_date=target.isoformat(),
    )
    return len(sched)


def _historical_reactions(events: list[dict], prices: list[dict]) -> list[dict]:
    """For each past earnings event, compute the next-trading-day reaction
    (close_after vs close_before). FMP removed BMO/AMC timing so we use
    the price-data approach documented in docs/FMP_ENDPOINTS.md.

    Returns the most recent EARNINGS_REACTIONS_LOOKBACK reactions
    chronologically reversed (newest first), each shaped:
      {"date": "2026-02-20", "type": "earnings", "reaction_pct": +4.32}
    """
    if not prices:
        return []
    price_by_date = {p["date"]: p for p in prices}
    sorted_dates = sorted(price_by_date.keys())
    today_iso = date.today().isoformat()

    reactions: list[dict] = []
    for e in events:
        d_str = e.get("date")
        if not d_str:
            continue
        event_date = d_str[:10]
        if event_date >= today_iso:
            continue
        before = _find_trading_day_on_or_before(sorted_dates, event_date)
        after = _find_trading_day_after(sorted_dates, event_date)
        if before is None or after is None:
            continue
        close_before = price_by_date[before].get("close")
        close_after = price_by_date[after].get("close")
        if not close_before or not close_after:
            continue
        reaction_pct = (close_after - close_before) / close_before * 100
        reactions.append(
            {
                "date": event_date,
                "type": "earnings",
                "reaction_pct": round(reaction_pct, 2),
            }
        )

    reactions.sort(key=lambda r: r["date"], reverse=True)
    return reactions[:EARNINGS_REACTIONS_LOOKBACK]


def _find_trading_day_on_or_before(sorted_dates: list[str], target: str) -> str | None:
    """Bisect for greatest date <= target. O(log n)."""
    idx = bisect.bisect_right(sorted_dates, target) - 1
    return sorted_dates[idx] if idx >= 0 else None


def _find_trading_day_after(sorted_dates: list[str], target: str) -> str | None:
    """Bisect for least date > target. O(log n)."""
    idx = bisect.bisect_right(sorted_dates, target)
    return sorted_dates[idx] if idx < len(sorted_dates) else None


def _news_bullets(items: list[dict], limit: int) -> list[str]:
    """Convert FMP news rows to short headline bullets (newest first).

    Each bullet: "YYYY-MM-DD - Source: Headline". Headline is the only
    field guaranteed present; date/source rendered when available.
    """
    rows = sorted(
        (n for n in items if n.get("title")),
        key=lambda n: n.get("publishedDate") or n.get("date") or "",
        reverse=True,
    )
    out: list[str] = []
    for n in rows[:limit]:
        date_str = (n.get("publishedDate") or n.get("date") or "")[:10] or "?"
        source = n.get("site") or n.get("publisher") or "?"
        title = n.get("title") or ""
        out.append(f"{date_str} - {source}: {title}")
    return out


def _analyst_revisions_summary(
    grades: list[dict],
    consensus_history: list[dict],
    pt: dict | None,
) -> dict:
    """Summarize analyst activity using BOTH per-firm rating events and
    the time-series consensus snapshot.

    Returns the shape the dashboard's _render_catalyst expects:
      {"count": int, "avg_pt": float | str, "trend": str}

    Why a combined view: FMP's `/grades` endpoint mostly returns
    "maintain" reiterations (no upgrade/downgrade visibility on
    Starter), so counting rating changes alone hides the real story.
    The `/grades-historical` consensus snapshot — "58 strong-buy/buy,
    3 hold, 1 sell" — is the stronger signal because it captures where
    the analyst community actually stands today.

    `count` = number of grade events (any action) in the last
        ANALYST_LOOKBACK_DAYS — useful as a "coverage intensity"
        proxy (23 desks weighed in = high attention).
    `avg_pt` = current consensus median PT.
    `trend` = plain-English consensus narrative including the
        snapshot distribution AND month-over-month change.
    """
    cutoff_iso = (date.today() - timedelta(days=ANALYST_LOOKBACK_DAYS)).isoformat()
    recent_changes = [g for g in grades if (g.get("date") or "")[:10] >= cutoff_iso]

    snapshot = consensus_history[0] if consensus_history else None
    snapshot_mom = _find_consensus_snapshot_n_days_ago(
        consensus_history, days_ago=ANALYST_LOOKBACK_DAYS
    )
    trend = _build_consensus_trend(snapshot, snapshot_mom, len(recent_changes))

    avg_pt = (pt or {}).get("median")
    return {
        "count": len(recent_changes),
        "avg_pt": round(avg_pt, 2) if isinstance(avg_pt, (int, float)) else "?",
        "trend": trend,
        # Surface the raw snapshot too for the verdict step + tests
        "consensus_snapshot": snapshot,
        "consensus_snapshot_30d_ago": snapshot_mom,
    }


def _find_consensus_snapshot_n_days_ago(
    history: list[dict], days_ago: int
) -> dict | None:
    """Return the snapshot closest to `days_ago` days back, or None."""
    if not history:
        return None
    target_iso = (date.today() - timedelta(days=days_ago)).isoformat()
    # `history` is newest-first per FMP convention; find the first entry
    # at or before the target date.
    for snap in history:
        d = (snap.get("date") or "")[:10]
        if d and d <= target_iso:
            return snap
    return None


def _bullishness_score(snap: dict | None) -> float | None:
    """Score the consensus snapshot 0 (all strong-sell) to 1 (all
    strong-buy). Returns None if the snapshot is empty/missing."""
    if not snap:
        return None
    sb = snap.get("analystRatingsStrongBuy") or 0
    b = snap.get("analystRatingsBuy") or 0
    h = snap.get("analystRatingsHold") or 0
    s = snap.get("analystRatingsSell") or 0
    ss = snap.get("analystRatingsStrongSell") or 0
    total = sb + b + h + s + ss
    if total == 0:
        return None
    weighted = (sb * 4 + b * 3 + h * 2 + s * 1 + ss * 0) / (total * 4)
    return weighted


def _build_consensus_trend(
    snap: dict | None, snap_prior: dict | None, recent_event_count: int
) -> str:
    """Plain-English narrative for the trend field combining the
    snapshot distribution, MoM shift, and recent grade-event volume."""
    if not snap:
        # No snapshot — fall back to event-count narrative
        if recent_event_count == 0:
            return "no analyst activity in the last 30 days"
        return f"{recent_event_count} grade event(s) in last 30 days, consensus snapshot unavailable"

    sb = snap.get("analystRatingsStrongBuy") or 0
    b = snap.get("analystRatingsBuy") or 0
    h = snap.get("analystRatingsHold") or 0
    s = snap.get("analystRatingsSell") or 0
    ss = snap.get("analystRatingsStrongSell") or 0
    bullish = sb + b
    bearish = s + ss
    total = sb + b + h + s + ss

    if total == 0:
        return "no analyst coverage"

    bullish_pct = bullish * 100 / total
    if bullish_pct >= 80:
        tone = "strongly bullish"
    elif bullish_pct >= 60:
        tone = "bullish"
    elif bullish_pct >= 40:
        tone = "mixed"
    elif bullish_pct >= 20:
        tone = "bearish"
    else:
        tone = "strongly bearish"

    distribution = f"{bullish} buy-or-better, {h} hold, {bearish} sell-or-worse (of {total})"

    # MoM trend
    mom = ""
    cur_score = _bullishness_score(snap)
    prior_score = _bullishness_score(snap_prior)
    if cur_score is not None and prior_score is not None:
        delta_pp = (cur_score - prior_score) * 100
        if abs(delta_pp) < 1:
            mom = " — stable vs 30 days ago"
        elif delta_pp > 0:
            mom = f" — more bullish (+{delta_pp:.1f}pp vs 30d ago)"
        else:
            mom = f" — less bullish ({delta_pp:.1f}pp vs 30d ago)"

    activity = f"; {recent_event_count} grade event(s) in last 30 days" if recent_event_count else ""

    return f"{tone}: {distribution}{mom}{activity}"


# ---------- conviction-layer helpers ----------


def _proximity_haircut(distance_sessions: int) -> float:
    """Look up proximity haircut from the YAML bands (first match wins).

    Bands example:
      - {max_sessions: 1, haircut: 0.40}
      - {max_sessions: 3, haircut: 0.20}
      - {max_sessions: 5, haircut: 0.10}
    Past the last band's max_sessions, haircut is 0.0 (no penalty).
    """
    for band in _PROXIMITY_BANDS:
        if distance_sessions <= band.max_sessions:
            return band.haircut
    return 0.0


# ---------- engine recommendation paragraph ----------


def _build_engine_recommendation(payload: dict, tier: str | None) -> str:
    """Build a plain-English summary paragraph for the engine-read panel.

    Synthesizes: next event distance, recent news direction, analyst
    activity, short-interest level + trend, and the in-horizon veto
    implication for ENTER decisions.
    """
    parts: list[str] = []

    ne = payload["next_event"]
    dist = payload["distance_sessions"]
    if ne and dist is not None:
        if dist <= 1:
            parts.append(
                f"**{ne['type'].title()} is {('today' if dist == 0 else 'tomorrow')}** "
                f"({ne['date']}) — Layer-3 in-horizon catalyst veto fires for watching users "
                f"on every horizon that contains this event (ENTER → WAIT until post-print). "
                f"Treat any pre-print signal as a coin flip on event direction; the math "
                f"doesn't price scheduled-event variance well at this distance."
            )
        elif dist <= 5:
            parts.append(
                f"**{ne['type'].title()} in {dist} sessions** ({ne['date']}) — Layer-3 "
                f"in-horizon catalyst veto fires for watching users on every horizon that "
                f"contains this event (ENTER → WAIT until post-print). The empirical "
                f"earnings-jump overlay is already priced into the Monte-Carlo paths."
            )
        elif payload["in_horizon_30d"]:
            parts.append(
                f"{ne['type'].title()} in {dist} sessions ({ne['date']}) — inside the "
                f"30-day horizon, so the 30-day Layer-3 catalyst veto fires for watching "
                f"users (ENTER → WAIT until post-print). The 60-day verdict treats this "
                f"event as a mid-horizon risk and remains actionable."
            )
        elif payload["in_horizon_60d"]:
            parts.append(
                f"{ne['type'].title()} in {dist} sessions ({ne['date']}) — outside the "
                f"30-day horizon, inside the 60-day. 30-day verdict is unaffected; "
                f"60-day verdict registers the event as terminal-window variance."
            )
        else:
            parts.append(
                f"Next earnings in {dist} sessions ({ne['date']}) — well outside both "
                f"horizons, no catalyst veto active. Conviction is set by the "
                f"non-event factors (regime, fair value, technical setup)."
            )
    else:
        parts.append(
            "No upcoming earnings or scheduled catalyst within the 90-day forward "
            "lookahead. Conviction is set entirely by non-event factors (regime, "
            "fair value, technical setup) — no Layer-3 catalyst veto is active."
        )

    reactions = payload["historical_reactions"]
    if reactions:
        avg = sum(r["reaction_pct"] for r in reactions) / len(reactions)
        avg_dir = "positive" if avg > 0 else "negative"
        spreads = [abs(r["reaction_pct"]) for r in reactions]
        avg_magnitude = sum(spreads) / len(spreads)
        parts.append(
            f"Last {len(reactions)} earnings reactions averaged {avg:+.1f}% "
            f"(net {avg_dir}), with mean absolute move {avg_magnitude:.1f}%. "
            f"That's the historical magnitude prior to consult when sizing risk "
            f"around the upcoming event — implied-move estimates that diverge "
            f"sharply from this band warrant a second look."
        )

    si = payload["short_interest"] or {}
    si_source = si.get("source", "")
    if si_source.startswith("yfinance") or si_source in ("cache:fresh", "cache:stale"):
        short_pct = si.get("short_percent_of_float")
        trend_pct = si.get("trend_month_over_month_pct")
        if short_pct is not None:
            short_band = (
                "low (< 5% — minimal squeeze fuel)"
                if short_pct < 0.05
                else "moderate (5-15% — meaningful but not extreme)"
                if short_pct < 0.15
                else "elevated (15-25% — squeeze-candidate territory)"
                if short_pct < 0.25
                else "very high (> 25% — high squeeze potential AND high downside conviction)"
            )
            trend_phrase = ""
            if trend_pct is not None and abs(trend_pct) >= 5:
                trend_phrase = (
                    f" Shorts increased {trend_pct:+.0f}% month-over-month — short-side conviction is building."
                    if trend_pct > 0
                    else f" Shorts decreased {trend_pct:+.0f}% month-over-month — short-side conviction is easing."
                )
            staleness = ""
            if si_source == "cache:stale" and si.get("stale_days"):
                staleness = f" (data is {si['stale_days']} day(s) old — yfinance unavailable in this run)"
            parts.append(
                f"Short interest {short_pct*100:.2f}% of float — {short_band}.{trend_phrase}{staleness}"
            )
    elif si_source == "fmp:float-proxy":
        label = si.get("float_proxy_label", "?")
        parts.append(
            f"Short-interest data unavailable today; using FMP float-size proxy as "
            f"a fallback — float reads as **{label.lower()}**. Precise short% will "
            f"return on the next successful yfinance fetch."
        )
    elif si_source == "unavailable":
        parts.append(
            f"Short-interest data unavailable today ({si.get('failure_reason', 'unknown')}). "
            f"This is one of three known squeeze-risk signals — when it's missing, lean on "
            f"days-to-cover history and recent volume to gauge crowding."
        )

    ar = payload["analyst_revisions"]
    if ar and (ar.get("trend") or ar.get("count")):
        avg_pt_str = f"${ar['avg_pt']}" if isinstance(ar.get("avg_pt"), (int, float)) else "?"
        # `trend` is now self-contained (includes the consensus
        # distribution, MoM shift, AND recent-event count when present),
        # so no need to prefix with a separate count.
        parts.append(
            f"Analyst desk reads {ar['trend']}. Current consensus price target {avg_pt_str}."
        )

    news = payload["news_bullets"]
    if news:
        parts.append(
            f"News flow: {len(news)} headline(s) shown below covering the last "
            f"{NEWS_LOOKBACK_DAYS} days. Skim for direction and concentration — "
            f"a cluster of related stories often precedes a re-rating."
        )

    errs = payload["errors"]
    if errs:
        failed = ", ".join(sorted({e["sub_fetch"] for e in errs}))
        parts.append(
            f"_Note: the following sub-fetch(es) failed this run and are excluded from the "
            f"above narrative: {failed}. The engine continues; missing pieces will return "
            f"on the next successful fetch._"
        )

    return "\n\n".join(parts)


# ---------- cache layer ----------


def _cache_path(ticker: str):
    return CACHE_DIR / f"{ticker}.json"


def _load_cache(ticker: str) -> dict | None:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _cache_is_fresh(cached: dict) -> bool:
    ts_str = cached.get("fetched_at")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=CACHE_TTL_HOURS)
    except ValueError:
        return False


def _save_cache(ticker: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with _cache_path(ticker).open("w") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        logger.warning(f"{ticker}: failed to write catalyst cache: {e}")


# ---------- CLI smoke test ----------


def _print_summary(payload: dict) -> None:
    t = payload["ticker"]
    print(f"\n=== {t}  source={payload['source']}  ===")

    ne = payload["next_event"]
    dist = payload["distance_sessions"]
    if ne:
        print(f"  next event:        {ne['type']} on {ne['date']} ({dist} session(s) away)")
        print(f"  proximity haircut: {payload['proximity_haircut']*100:.0f}%")
        print(f"  in horizon:        30d={payload['in_horizon_30d']}  60d={payload['in_horizon_60d']}")
    else:
        print(f"  next event:        none scheduled within {FORWARD_WINDOW_DAYS}d")

    reactions = payload["historical_reactions"]
    if reactions:
        print(f"  last {len(reactions)} earnings reactions:")
        for r in reactions:
            print(f"     {r['date']}  {r['reaction_pct']:+6.2f}%")

    si = payload["short_interest"] or {}
    if si.get("source"):
        if si.get("short_percent_of_float") is not None:
            print(f"  short interest:    {si['short_percent_of_float']*100:.2f}% of float (source: {si['source']})")
        elif si.get("float_proxy_label"):
            print(f"  short interest:    [proxy] {si['float_proxy_label']} float (source: {si['source']})")
        else:
            print(f"  short interest:    unavailable (source: {si['source']})")

    pt = payload["price_target_consensus"]
    if pt:
        print(f"  PT consensus:      median ${pt.get('median')}  high ${pt.get('high')}  low ${pt.get('low')}")

    ar = payload["analyst_revisions"]
    if ar.get("count"):
        print(f"  analyst (30d):     {ar['count']} changes - {ar['trend']}")

    news = payload["news_bullets"]
    if news:
        print(f"  news ({len(news)}):")
        for bullet in news:
            print(f"     - {bullet[:120]}")

    if payload["errors"]:
        print(f"  errors:")
        for e in payload["errors"]:
            print(f"     ! {e['sub_fetch']}: {e['message']}")

    if payload["engine_recommendation"]:
        print(f"\n  engine recommendation:")
        for para in payload["engine_recommendation"].split("\n\n"):
            print(f"     {para}")


def main() -> int:
    from src.pipeline import data as data_step

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Catalyst-step smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to scan")
    parser.add_argument("--refresh", action="store_true", help="bypass catalyst-step cache")
    args = parser.parse_args()

    exit_code = 0
    for ticker in args.tickers:
        ticker = ticker.upper()
        try:
            price_data = data_step.fetch(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR fetching price data for {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        try:
            payload = detect(ticker, price_data, refresh=args.refresh)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR running catalyst step for {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        _print_summary(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
