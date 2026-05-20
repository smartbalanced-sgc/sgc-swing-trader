"""Conviction trajectory — multi-night view of how a ticker's conviction
score has evolved.

The dashboard's `_render_trajectory` panel reads `snap["trajectory"]`
with this schema:

    {
        "status":         "ok" | "pending",
        "nightly_scores": [float, ...],   # oldest first; INCLUDES today
        "annotation":     "rising" | "stable" | "decaying" | "unstable",
        "summary":        "plain-English single sentence",
    }

Why this lives in its own module: it consumes prior snapshots from
`data/snapshots/{TICKER}/*.json` and produces a derived block. Keeping
it out of `main.py` and `verdict.py` makes the multi-night logic
testable in isolation against synthetic snapshot fixtures.

What score we track: the 30-day-horizon final_score, averaged across
tracking users. The 30d horizon is the primary swing-trade window; the
60d horizon is the longer-term frame and would drift the sparkline.
When users diverge (one entered, one watching) we average them: the
trajectory's job is to show whether the *signal* is durable, not to
tell each user a personal story (the per-horizon breakdown does that).
"""

from __future__ import annotations

from src import config, snapshot

# How far back the sparkline reads. Twenty trading days ≈ 4 weeks, the
# window over which a thesis either stays intact or breaks. Older nights
# are out of scope for "is today's read durable" — they belong to a
# different market regime.
_LOOKBACK_NIGHTS = 20

# Classification thresholds. Tuned so a slow drift over 5+ nights reads
# as rising/decaying, while genuine noise reads as stable/unstable.
_RISING_DELTA = 0.05           # latest exceeds first by this much → rising
_DECAYING_DELTA = -0.05        # latest below first by this much → decaying
_UNSTABLE_DIRECTION_CHANGES = 3  # 3+ sign flips in last 5 nights → unstable
_UNSTABLE_LOOKBACK = 5         # window for direction-change count


def build(ticker: str, current_snap: dict) -> dict:
    """Build the trajectory block for today's snapshot.

    Reads the last _LOOKBACK_NIGHTS prior snapshots, appends today's
    score, classifies the pattern, and returns the trajectory dict.
    """
    history = snapshot.load_recent(ticker, n_days=_LOOKBACK_NIGHTS - 1)
    nightly_scores: list[float] = []
    for prior in history:
        score = _extract_30d_avg_score(prior)
        if score is not None:
            nightly_scores.append(score)

    today_score = _extract_30d_avg_score(current_snap)
    if today_score is None:
        return {"status": "pending", "reason": "today's snapshot has no 30d conviction"}
    nightly_scores.append(today_score)

    annotation, summary = _classify(nightly_scores)
    return {
        "status": "ok",
        "nightly_scores": nightly_scores,
        "annotation": annotation,
        "summary": summary,
    }


def _extract_30d_avg_score(snap: dict) -> float | None:
    """Average the 30-day final_score across users in this snapshot.
    Returns None if conviction wasn't produced or the 30d horizon is
    missing (snapshots from data-sanity-failed runs)."""
    conv = (snap.get("conviction") or {})
    if conv.get("status") != "ok":
        return None
    horizon = (conv.get("horizons") or {}).get(30) or (conv.get("horizons") or {}).get("30")
    if not horizon:
        return None
    user_scores = []
    for user, user_block in horizon.items():
        breakdown = (user_block or {}).get("breakdown") or {}
        score = breakdown.get("final_score")
        if isinstance(score, (int, float)):
            user_scores.append(float(score))
    if not user_scores:
        return None
    return sum(user_scores) / len(user_scores)


def _classify(scores: list[float]) -> tuple[str, str]:
    """Map a list of nightly scores to (annotation, plain-English summary)."""
    n = len(scores)
    if n < 3:
        return (
            "stable",
            f"{n} night(s) recorded — sparkline will gain meaning at 3+ nights of accumulated history."
            if n > 1
            else "First production night — the sparkline will start drawing on the next run.",
        )

    # Direction changes in the last _UNSTABLE_LOOKBACK nights.
    tail = scores[-_UNSTABLE_LOOKBACK:]
    deltas = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
    sign_flips = sum(
        1
        for i in range(len(deltas) - 1)
        if (deltas[i] > 0) != (deltas[i + 1] > 0) and deltas[i] != 0 and deltas[i + 1] != 0
    )
    if sign_flips >= _UNSTABLE_DIRECTION_CHANGES:
        return (
            "unstable",
            f"Score has flipped direction {sign_flips} times in the last {len(tail)} nights — "
            f"signal is at a regime boundary. Layer-2 trajectory haircut fires.",
        )

    drift = scores[-1] - scores[0]
    if drift >= _RISING_DELTA:
        return (
            "rising",
            f"Score rose from {scores[0]:.2f} to {scores[-1]:.2f} over {n} nights "
            f"(+{drift:.2f}) — directional confirmation.",
        )
    if drift <= _DECAYING_DELTA:
        return (
            "decaying",
            f"Score fell from {scores[0]:.2f} to {scores[-1]:.2f} over {n} nights "
            f"({drift:+.2f}) — thesis weakening.",
        )
    return (
        "stable",
        f"Score has held near {scores[-1]:.2f} across {n} nights "
        f"(range {min(scores):.2f}–{max(scores):.2f}) — durable read.",
    )
