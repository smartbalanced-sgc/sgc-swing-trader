"""Engine entrypoint — invoked by the nightly cron.

Orchestrates the 8-step pipeline (docs/V1_SPEC.md §2) for every ticker
in the watchlist, runs the §4.2 measurement-driven tier classifier,
writes per-ticker snapshots, and renders the dashboard.

**Walking skeleton.** Pipeline steps that are not yet implemented are
caught (NotImplementedError) and recorded in the snapshot with
status='pending'. This is intentional — the engine runs end-to-end from
day one, and the dashboard surfaces what's implemented vs what's still
pending. As each pipeline step (regime, catalyst, volatility, fair
value, MC, PDE, verdict) comes online its 'pending' slot in the
snapshot is replaced by real output and its dashboard panel becomes
live.

CLI:
    python -m src.main                    # run full nightly pass
    python -m src.main --ticker NVDA      # single-ticker dry run
    python -m src.main --no-write         # don't write snapshot/dashboard files
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, timezone

import yaml

from src import config, dashboard, snapshot, tier_classifier
from src.pipeline import (
    analytic_verifier,
    catalyst,
    data,
    fair_value,
    monte_carlo,
    regime,
    targets,
    verdict,
    volatility,
)


def run(tickers: list[str] | None = None, write: bool = True) -> dict:
    """Run one nightly cycle. Returns the run payload (also the input
    to the dashboard renderer)."""
    run_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_date = date.today().isoformat()

    watchlist = _load_watchlist()
    target_tickers = tickers if tickers else list(watchlist.keys())

    ticker_snapshots: dict[str, dict] = {}
    run_errors: list[dict] = []

    for ticker in target_tickers:
        entry = watchlist.get(ticker)
        if entry is None:
            run_errors.append({"ticker": ticker, "stage": "lookup", "message": "not in watchlist"})
            continue
        try:
            snap = _process_one(ticker, entry, run_date)
        except Exception as e:  # noqa: BLE001 - a single-ticker crash must not kill the run
            run_errors.append(
                {
                    "ticker": ticker,
                    "stage": "process",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                }
            )
            continue

        ticker_snapshots[ticker] = snap
        if write:
            snapshot.write(snap)

    payload = {
        "run_date": run_date,
        "run_started_at": run_started_at,
        "run_finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "watchlist": watchlist,
        "tickers": ticker_snapshots,
        "errors": run_errors,
    }

    if write:
        html = dashboard.render(payload)
        config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DASHBOARD_PATH.write_text(html)

    return payload


# ---------- per-ticker pipeline ----------


def _process_one(ticker: str, watchlist_entry: dict, run_date: str) -> dict:
    """Run the pipeline for one ticker. Returns the snapshot dict."""
    anchor_tier = watchlist_entry.get("tier")
    snap: dict = {
        "ticker": ticker,
        "run_date": run_date,
        "tier_anchor": anchor_tier,
        "notes": watchlist_entry.get("notes"),
    }

    # Step 1 — data fetch & sanity. Hard dependency for every later step.
    price_data = data.fetch(ticker)
    snap["as_of"] = price_data.get("as_of")
    snap["data"] = {
        "status": "fail" if price_data["sanity"]["overall"] == "fail" else "ok",
        "sanity": price_data["sanity"],
        "profile": price_data["profile"],
        "bar_count": len(price_data["prices"]),
        "fetched_at": price_data.get("fetched_at"),
    }

    if snap["data"]["status"] == "fail":
        # Sanity failure → skip math, mark everything else pending.
        snap.update(_pending_block("data sanity failed — math skipped this run"))
        return snap

    # §4.2 measurement-driven tier classifier (advisory).
    snap["tier_classifier"] = _run_safely(
        "tier_classifier",
        lambda: _classify_with_anchor(price_data, anchor_tier),
    )

    # Pipeline steps 2-7 — all currently stubs; the try/except records
    # 'pending' for any that raise NotImplementedError.
    snap["regime"] = _run_safely(
        "regime",
        lambda: regime.detect(ticker, price_data, anchor_tier),
    )
    snap["catalyst"] = _run_safely(
        "catalyst",
        lambda: catalyst.detect(ticker, price_data, anchor_tier),
    )
    snap["volatility"] = _run_safely(
        "volatility",
        lambda: volatility.forecast(ticker, price_data, anchor_tier),
    )
    snap["fair_value"] = _run_safely(
        "fair_value",
        lambda: fair_value.estimate(ticker, price_data),
    )

    # Targets — per-user target/stop derivation. Runs after volatility
    # (vol-scaled defaults need the GARCH 30d forecast) and before MC
    # (which tests first-passage against these levels). For `entered`
    # users targets come from watchlist; for `watching` users they're
    # vol-scaled defaults from src/pipeline/targets.py.
    # MC needs raw price data to compute RSI and 60d high for the
    # price_levels panel — stash on snap under a private key the
    # snapshot writer drops before serialization.
    snap["_price_data"] = price_data
    snap["targets"] = _run_safely(
        "targets",
        lambda: targets.derive(ticker, watchlist_entry, snap),
    )

    snap["monte_carlo"] = _run_safely(
        "monte_carlo",
        lambda: monte_carlo.simulate(ticker, snap, run_date),
    )
    # Unpack MC's price_levels and daily_path sub-blocks into their
    # own top-level snap keys (where the dashboard renderers look).
    # Done with .pop() so the duplicated data doesn't double the
    # serialized snapshot size.
    if snap["monte_carlo"].get("status") == "ok":
        snap["price_levels"] = snap["monte_carlo"].pop("price_levels", {"status": "pending"})
        snap["daily_path"] = snap["monte_carlo"].pop("daily_path", {"status": "pending"})
    else:
        snap["price_levels"] = {"status": "pending", "reason": "monte_carlo not ok"}
        snap["daily_path"] = {"status": "pending", "reason": "monte_carlo not ok"}

    snap["analytic_verifier"] = _run_safely(
        "analytic_verifier",
        lambda: analytic_verifier.verify(ticker, snap),
    )

    # Step 8 — per-user verdicts. Branches over watchlist holders.
    holders = watchlist_entry.get("holders") or {}
    snap["verdict"] = {}
    for user, state in holders.items():
        snap["verdict"][user] = _run_safely(
            f"verdict.{user}",
            lambda u=user, s=state: verdict.synthesize(ticker, snap, u, s),
        )

    # Drop the private price-data pass-through before snapshot
    # serialization (keeps snapshot files small).
    snap.pop("_price_data", None)

    return snap


def _classify_with_anchor(price_data: dict, anchor_tier: str | None) -> dict:
    result = tier_classifier.classify(price_data)
    result["comparison"] = tier_classifier.compare_to_anchor(result, anchor_tier)
    result["status"] = "ok"
    return result


def _run_safely(stage_name: str, fn) -> dict:
    """Call a pipeline-step function and wrap the result.

    - NotImplementedError → {'status': 'pending', ...}
    - Any other exception → {'status': 'fail', ...} with the message
    - Success returns the result dict with 'status': 'ok' (if the
      function didn't already set one).
    """
    try:
        out = fn()
    except NotImplementedError:
        return {"status": "pending", "reason": f"{stage_name} not yet implemented"}
    except Exception as e:  # noqa: BLE001
        return {
            "status": "fail",
            "reason": str(e),
            "traceback": traceback.format_exc(),
        }
    if isinstance(out, dict):
        out.setdefault("status", "ok")
        return out
    return {"status": "ok", "value": out}


def _pending_block(reason: str) -> dict:
    return {
        "tier_classifier": {"status": "pending", "reason": reason},
        "regime": {"status": "pending", "reason": reason},
        "catalyst": {"status": "pending", "reason": reason},
        "volatility": {"status": "pending", "reason": reason},
        "fair_value": {"status": "pending", "reason": reason},
        "targets": {"status": "pending", "reason": reason},
        "monte_carlo": {"status": "pending", "reason": reason},
        "price_levels": {"status": "pending", "reason": reason},
        "daily_path": {"status": "pending", "reason": reason},
        "analytic_verifier": {"status": "pending", "reason": reason},
        "verdict": {},
    }


def _load_watchlist() -> dict:
    with config.WATCHLIST_PATH.open() as f:
        return yaml.safe_load(f) or {}


# ---------- CLI ----------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one nightly engine cycle.")
    parser.add_argument(
        "--ticker",
        action="append",
        help="restrict the run to one or more tickers (repeatable). Default: full watchlist.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="dry run — don't write snapshots or dashboard to disk",
    )
    args = parser.parse_args()

    tickers = [t.upper() for t in args.ticker] if args.ticker else None
    payload = run(tickers=tickers, write=not args.no_write)

    n_ok = sum(
        1 for s in payload["tickers"].values() if s.get("data", {}).get("status") == "ok"
    )
    n_total = len(payload["tickers"])
    print(
        f"engine run complete: {n_ok}/{n_total} tickers fetched cleanly, "
        f"{len(payload['errors'])} run-level error(s)"
    )
    if payload["errors"]:
        for err in payload["errors"]:
            print(f"  ERROR  {err['ticker']:<6} stage={err['stage']}  {err['message']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
