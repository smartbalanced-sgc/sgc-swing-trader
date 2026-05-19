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

    # --- Sub-fetch 2: recent news headlines ---
    news = _safe(payload, "news", _fetch_news, ticker)
    if news:
        payload["news_bullets"] = _news_bullets(news, NEWS_HEADLINES_MAX)

    # --- Sub-fetch 3: analyst rating changes + price-target consensus ---
    grades = _safe(payload, "analyst_grades", _fetch_grades, ticker)
    pt = _safe(payload, "price_target_consensus", _fetch_price_target_consensus, ticker)
    if pt:
        payload["price_target_consensus"] = pt
    if grades is not None:
        payload["analyst_revisions"] = _analyst_revisions_summary(grades, pt)

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


# FMP Starter caps earnings-calendar HISTORICAL lookback at 1 year
# (confirmed by FMP support 2026-05-19; exceeding it returns a 402,
# not an empty result). 360 days = safe margin under 365 that still
# covers 4 quarterly earnings events — sufficient for the
# EARNINGS_REACTIONS_LOOKBACK=4 historical-reaction panel. Forward
# lookback is uncapped on Starter and uses the full FORWARD_WINDOW_DAYS.
_HISTORICAL_LOOKBACK_DAYS_CAP = 360


def _fetch_earnings_events(ticker: str) -> list[dict]:
    """Fetch earnings events for this ticker spanning past 360 days
    through future FORWARD_WINDOW_DAYS. FMP's `earnings-calendar`
    endpoint returns every company's events in the window; filter to
    this ticker locally. (Filtering server-side via `symbol=` works on
    some FMP plans but not consistently — local filter is the safe path.)

    Per FMP support, the FROM date must be within 1 year of today on
    Starter; we cap at 360 days to leave margin.
    """
    historical_days = min(HISTORY_LOOKBACK_QUARTERS * 95, _HISTORICAL_LOOKBACK_DAYS_CAP)
    from_date = date.today() - timedelta(days=historical_days)
    to_date = date.today() + timedelta(days=FORWARD_WINDOW_DAYS)
    body = fmp.get(
        "earnings-calendar",
        symbol=None,
        from_=from_date.isoformat(),
        to=to_date.isoformat(),
    )
    if not isinstance(body, list):
        return []
    return [e for e in body if (e.get("symbol") or "").upper() == ticker.upper()]


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


# Bullish-ness rank for analyst grade labels — used as the fallback
# classifier when FMP's response doesn't include an explicit action
# field (or uses a field name we don't recognize). Rank-up = upgrade,
# rank-down = downgrade, same rank or unknown = "held". Covers the
# variants we see in FMP's grade strings across firms (Goldman uses
# "Buy", Morgan Stanley "Overweight", Wells "Outperform", etc.).
_GRADE_RANKS: dict[str, int] = {
    "strong sell": 0, "sell": 0,
    "underperform": 1, "underweight": 1, "reduce": 1, "negative": 1,
    "hold": 2, "neutral": 2, "market perform": 2, "equal weight": 2,
    "sector perform": 2, "peer perform": 2, "in-line": 2, "in line": 2,
    "outperform": 3, "overweight": 3, "accumulate": 3, "sector outperform": 3,
    "long-term buy": 3, "moderate buy": 3, "positive": 3,
    "buy": 4, "strong buy": 4, "conviction buy": 4,
}


def _grade_rank(grade) -> int | None:
    """Map an analyst grade string to its bullish-ness rank, or None
    if we don't recognize it."""
    if not grade:
        return None
    return _GRADE_RANKS.get(str(grade).strip().lower())


def _classify_grade_action(g: dict) -> str:
    """Return one of 'upgrade', 'downgrade', or 'held' for a single
    grade-change record. FMP's response shape varies — try several
    likely action-field names, then fall back to inferring from
    previousGrade vs newGrade rank comparison."""
    # Try explicit action-like fields in priority order.
    for key in ("action", "gradeAction", "gradeChange"):
        v = (g.get(key) or "").strip().lower()
        if not v:
            continue
        if "up" in v or "raise" in v:
            return "upgrade"
        if "down" in v or "cut" in v or "lower" in v:
            return "downgrade"
        if "main" in v or "hold" in v or "reiterat" in v or "init" in v:
            return "held"

    # Fallback: rank comparison between previousGrade and newGrade.
    prev_rank = _grade_rank(g.get("previousGrade"))
    new_rank = _grade_rank(g.get("newGrade"))
    if prev_rank is not None and new_rank is not None:
        if new_rank > prev_rank:
            return "upgrade"
        if new_rank < prev_rank:
            return "downgrade"
    return "held"


def _analyst_revisions_summary(grades: list[dict], pt: dict | None) -> dict:
    """Summarize rating-change activity in the last ANALYST_LOOKBACK_DAYS.

    Returns the shape the dashboard's _render_catalyst expects:
      {"count": int, "avg_pt": float | str, "trend": str}

    `avg_pt` is the current consensus median price target (snapshot, not
    derived from the rating changes — FMP doesn't attach a PT to each
    change). `trend` is plain English: "mostly upgrades", "mixed",
    "mostly downgrades".
    """
    cutoff_iso = (date.today() - timedelta(days=ANALYST_LOOKBACK_DAYS)).isoformat()
    recent = [g for g in grades if (g.get("date") or "")[:10] >= cutoff_iso]

    if not recent:
        return {
            "count": 0,
            "avg_pt": (pt or {}).get("median") if pt else "?",
            "trend": "no rating changes",
        }

    classifications = [_classify_grade_action(g) for g in recent]
    upgrades = sum(1 for c in classifications if c == "upgrade")
    downgrades = sum(1 for c in classifications if c == "downgrade")
    held = len(recent) - upgrades - downgrades

    if upgrades > downgrades * 1.5:
        trend = f"mostly upgrades ({upgrades}up / {downgrades}dn / {held}held)"
    elif downgrades > upgrades * 1.5:
        trend = f"mostly downgrades ({upgrades}up / {downgrades}dn / {held}held)"
    else:
        trend = f"mixed ({upgrades}up / {downgrades}dn / {held}held)"

    avg_pt = (pt or {}).get("median")
    return {
        "count": len(recent),
        "avg_pt": round(avg_pt, 2) if isinstance(avg_pt, (int, float)) else "?",
        "trend": trend,
    }


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
    activity, short-interest level + trend, and the proximity-haircut
    implication for ENTER decisions.
    """
    parts: list[str] = []

    ne = payload["next_event"]
    dist = payload["distance_sessions"]
    if ne and dist is not None:
        if dist <= 1:
            parts.append(
                f"**{ne['type'].title()} is {('today' if dist == 0 else 'tomorrow')}** "
                f"({ne['date']}) — Layer-3 catalyst veto is firing (proximity haircut "
                f"{payload['proximity_haircut']*100:.0f}% to confidence). Treat any "
                f"ENTER signal as a coin flip on event direction; the math doesn't "
                f"price scheduled-event variance well at this distance."
            )
        elif dist <= 5:
            parts.append(
                f"**{ne['type'].title()} in {dist} sessions** ({ne['date']}) — "
                f"proximity haircut {payload['proximity_haircut']*100:.0f}% applied to "
                f"confidence (Layer 2). ENTER is allowed but the engine's conviction "
                f"score will read lower than it would in a no-event window."
            )
        elif payload["in_horizon_30d"]:
            parts.append(
                f"{ne['type'].title()} in {dist} sessions ({ne['date']}) — inside the "
                f"30-day horizon but outside the proximity-haircut window. The 30-day "
                f"verdict will factor in event variance; the 60-day verdict treats "
                f"this event as a mid-horizon risk."
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
    if ar and ar.get("count"):
        avg_pt_str = f"${ar['avg_pt']}" if isinstance(ar.get("avg_pt"), (int, float)) else "?"
        parts.append(
            f"Analyst desk: {ar['count']} rating change(s) in the last "
            f"{ANALYST_LOOKBACK_DAYS} days — {ar['trend']}. Current consensus "
            f"price target {avg_pt_str}."
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
