"""Paths and calibratable thresholds.

All thresholds live in config/thresholds.yml — code reads them via the
THRESHOLDS object exposed here. Nothing in the codebase is allowed to
hard-code a numeric threshold; if a value needs calibration, add it to
the YAML.

Plain English of why YAML: thresholds get tuned often (calibration is
half the work). Pushing them to a single config file means tuning is a
one-line YAML edit + a one-line commit-message explaining the rationale,
not a code change scattered across modules.

Access pattern:
    from src import config
    config.THRESHOLDS.conviction.edge.ev_weight        # → 0.70
    config.THRESHOLDS.tier_classifier.vol_window_days  # → 90
    config.HORIZONS                                    # → (30, 60)
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import yaml

# ---------- run mode (production vs test) ----------
#
# Two modes:
#   - test (DEFAULT): outputs go to data/test/ and a separate dashboard
#     file. Production history stays untouched no matter how many times
#     you iterate. Test is the SAFE DEFAULT - it's harmless to run
#     accidentally; production is opt-in.
#   - production: outputs go to canonical paths and accumulate as
#     "real" history. The Conviction Trajectory panel, backtest
#     aggregates, and the published dashboard render all read from
#     these paths. Activate ONLY when you're committing to the
#     output being part of your audit trail.
#
# Activate production mode with:
#     SGC_RUN_MODE=production python -m src.main
#     SGC_RUN_MODE=production python -m src.backtest ...
#
# Default behavior (no env var, or any value other than "production"):
# test mode. This is intentional — accidentally running test mode is
# zero-cost (files just go to data/test/); accidentally running
# production mode pollutes the history that the trajectory panel
# treats as authoritative.
#
# Caches (data/cache/*) are SHARED across modes - they're just FMP/
# yfinance response caching for performance, the underlying data is
# the same regardless of which mode wrote them.

RUN_MODE = os.environ.get("SGC_RUN_MODE", "test").lower().strip()
IS_TEST_MODE = RUN_MODE != "production"

# ---------- repo paths (NOT calibratable; structural) ----------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
# Universe definition lives at root; it's shared across modes.
WATCHLIST_PATH = DATA_DIR / "watchlist.yml"
DOCS_DIR = REPO_ROOT / "docs"
CONFIG_DIR = REPO_ROOT / "config"
THRESHOLDS_PATH = CONFIG_DIR / "thresholds.yml"

# Mode-aware output paths. Test-mode runs route everything that
# *accumulates as history* into data/test/, so production runs that
# come later see a clean slate.
if IS_TEST_MODE:
    _MODE_DIR = DATA_DIR / "test"
    SNAPSHOTS_DIR = _MODE_DIR / "snapshots"
    BACKTEST_DIR = _MODE_DIR / "backtest"
    DASHBOARD_PATH = DOCS_DIR / "index-test.html"
else:
    SNAPSHOTS_DIR = DATA_DIR / "snapshots"
    BACKTEST_DIR = DATA_DIR / "backtest"
    DASHBOARD_PATH = DOCS_DIR / "index.html"

# Recognized users (shared Claude.ai account; see CLAUDE.md)
USERS = ("aidy", "jesse")


# ---------- YAML threshold loading ----------


def _to_namespace(obj):
    """Recursively convert nested dicts to SimpleNamespace so callers
    can use dotted access (config.THRESHOLDS.conviction.edge.ev_weight)
    instead of nested-dict indexing. Lists pass through; primitives
    pass through."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def _load_thresholds() -> SimpleNamespace:
    if not THRESHOLDS_PATH.exists():
        raise RuntimeError(
            f"thresholds file not found at {THRESHOLDS_PATH} — "
            "this file is required; check the repo layout"
        )
    with THRESHOLDS_PATH.open() as f:
        raw = yaml.safe_load(f) or {}
    return _to_namespace(raw)


THRESHOLDS = _load_thresholds()


# ---------- convenience aliases for hot-path values ----------
#
# Most code can use THRESHOLDS.* directly. These aliases exist for
# values referenced often enough that the dotted path is noisy.

HORIZONS = (THRESHOLDS.horizons.primary_days, THRESHOLDS.horizons.secondary_days)
MC_PATHS = THRESHOLDS.engine.mc_paths
