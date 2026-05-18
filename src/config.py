"""Thresholds, paths, and constants for the swing-trader engine.

Centralizing magic numbers here so the building conversation can tune them
in one place. See docs/V1_SPEC.md §13 for the list of values that are still
open questions.
"""

from pathlib import Path

# Repo paths
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
WATCHLIST_PATH = DATA_DIR / "watchlist.yml"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
DOCS_DIR = REPO_ROOT / "docs"
DASHBOARD_PATH = DOCS_DIR / "index.html"

# Recognized users (shared Claude.ai account; see CLAUDE.md)
USERS = ("aidy", "jesse")

# Horizons analyzed every run (calendar days)
HORIZONS = (30, 60)

# Monte Carlo paths per (ticker, horizon)
MC_PATHS = 50_000


# Measurement-driven tier classifier (V1_SPEC §4.2) — advisory bands.
# "Worst-of" rule: each property scores A/B/C independently and the
# ticker's measured tier is the most conservative (toward C) of the four.
# All four properties use the trailing 90 trading-day window where
# applicable. Plain-English meanings of each metric:
#   - vol_annualized:  typical year-over-year price swing as a fraction
#                      of price, computed from daily log-returns × √252
#   - vol_of_vol:      how much the rolling 20-day vol itself swings
#                      around (std-dev of 20d rolling vol over 90d)
#   - adv_usd:         20-day average daily dollar volume = mean(close ×
#                      volume), in raw US dollars (NOT millions)
#   - history_days:    number of clean daily bars available after sanity

TIER_CLASSIFIER = {
    "vol_window_days": 90,
    "vol_of_vol_inner_window": 20,
    "adv_window_days": 20,
    # Upper bounds — value <= bound goes into that tier. Last entry is C
    # catch-all (effectively +inf for "worse than B").
    "vol_annualized_bounds":   {"A": 0.35,    "B": 0.65,    "C": float("inf")},
    "vol_of_vol_bounds":       {"A": 0.15,    "B": 0.30,    "C": float("inf")},
    # ADV bounds are reversed (higher liquidity is better, so A needs
    # the HIGHEST ADV). Interpretation: ADV >= bound stays in that tier.
    "adv_usd_lower_bounds":    {"A": 500e6,   "B": 50e6,    "C": 0.0},
    "history_days_lower_bounds": {"A": 750,   "B": 250,     "C": 0},
}
