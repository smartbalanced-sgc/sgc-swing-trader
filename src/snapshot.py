"""Snapshot persistence and conviction smoothing.

See docs/V1_SPEC.md §7. Each nightly run writes a JSON snapshot per ticker
at data/snapshots/{TICKER}/{YYYY-MM-DD}.json containing every numeric
output of the pipeline (regime probabilities, GARCH vol path, MC results,
PDE results, per-user verdicts, etc.).

The next run loads recent snapshots to compute Bayesian-smoothed
conviction trajectories. Stub for now; filled in once we have real
pipeline outputs to persist.
"""


def write_snapshot(ticker: str, run_date: str, payload: dict) -> None:
    raise NotImplementedError


def load_recent(ticker: str, n_days: int) -> list[dict]:
    raise NotImplementedError
