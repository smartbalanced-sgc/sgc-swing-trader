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
