"""Engine entrypoint — invoked manually by the user.

Orchestrates the 8-step pipeline (docs/V1_SPEC.md §2) for every ticker
in the watchlist, runs the §4.2 measurement-driven tier classifier,
writes per-ticker snapshots, and renders the dashboard.

**Manual-run model.** No cron; the user runs this on demand. Default
mode is `test` (SGC_RUN_MODE=test, set automatically when env var is
absent). Production mode (`SGC_RUN_MODE=production`) is opt-in -
required only when the user explicitly wants the output to count
toward production history.

CLI:
    python -m src.main                    # test-mode run, full watchlist
    python -m src.main --ticker NVDA      # single-ticker
    python -m src.main --no-write         # dry run, no files written
    python -m src.main --refresh          # force fresh fetch (skip cache)
    SGC_RUN_MODE=production python -m src.main   # real production run

Recommended UK local times for daily production run:
    7-8 AM UK    most convenient if reading the dashboard before market open
    11 PM UK     captures full US trading day + after-hours news

For intraday re-runs (price moved a lot, want fresh signal):
    python -m src.main --refresh
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, timezone

import yaml

from src import backtest, config, dashboard, snapshot, tier_classifier, trajectory
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
    price_data_by_ticker: dict[str, dict] = {}
    run_errors: list[dict] = []

    for ticker in target_tickers:
        entry = watchlist.get(ticker)
        if entry is None:
            run_errors.append({"ticker": ticker, "stage": "lookup", "message": "not in watchlist"})
            continue
        try:
            snap, price_data = _process_one(ticker, entry, run_date)
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
        if price_data is not None:
            price_data_by_ticker[ticker] = price_data
        if write:
            snapshot.write(snap)

    # Backtest: scores predictions from past snapshots against the
    # subsequent price action. Inline (milliseconds, no extra API
    # calls — uses the price arrays already fetched above). Until 14
    # nights of snapshots have accumulated, the panel surfaces the
    # cold-start progress; after that, hit rates fill in as each
    # horizon elapses on past predictions.
    try:
        backtest_payload = backtest.run(price_data_by_ticker)
    except Exception as e:  # noqa: BLE001
        backtest_payload = {"status": "fail", "reason": str(e)}

    payload = {
        "run_date": run_date,
        "run_started_at": run_started_at,
        "run_finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "watchlist": watchlist,
        "tickers": ticker_snapshots,
        "errors": run_errors,
        "backtest": backtest_payload,
    }

    if write:
        html = dashboard.render(payload)
        config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DASHBOARD_PATH.write_text(html)

    return payload


# ---------- per-ticker pipeline ----------


def _process_one(ticker: str, watchlist_entry: dict, run_date: str) -> tuple[dict, dict | None]:
    """Run the pipeline for one ticker. Returns (snap, price_data).

    `price_data` is the raw 5-year-history data.fetch() result, returned
    alongside the snapshot so the inline backtest can score past
    predictions against subsequent price bars without re-fetching. It's
    None when the data fetch itself failed (sanity hard-fail)."""
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
        return snap, None

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
        lambda: fair_value.estimate(ticker, price_data, tier=anchor_tier),
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
        snap["swing_mode"] = snap["monte_carlo"].pop("swing_mode", {"status": "pending"})
        snap["daily_path"] = snap["monte_carlo"].pop("daily_path", {"status": "pending"})
    else:
        snap["price_levels"] = {"status": "pending", "reason": "monte_carlo not ok"}
        snap["swing_mode"] = {"status": "pending", "reason": "monte_carlo not ok"}
        snap["daily_path"] = {"status": "pending", "reason": "monte_carlo not ok"}

    snap["analytic_verifier"] = _run_safely(
        "analytic_verifier",
        lambda: analytic_verifier.verify(ticker, snap),
    )
    # The dashboard reads `snap["cross_check"]` for the Math
    # Cross-check panel. analytic_verifier produces that shape
    # directly (just `horizons` keyed by horizon_days with mc/pde/
    # delta fields per metric); promote it so the panel renders.
    if snap["analytic_verifier"].get("status") == "ok":
        snap["cross_check"] = {
            "status": "ok",
            "horizons": snap["analytic_verifier"].get("horizons", {}),
        }
    else:
        snap["cross_check"] = {"status": "pending", "reason": "analytic_verifier not ok"}

    # Step 8 — verdict synthesis. Produces the full per-ticker
    # conviction block (horizons → users → {breakdown, targets})
    # that the dashboard's Action and Conviction panels render.
    # Per-user branching happens INSIDE verdict.synthesize() so the
    # orchestrator stays thin and the conviction block lives as one
    # coherent dict rather than a loop-built collection.
    snap["conviction"] = _run_safely(
        "verdict",
        lambda: verdict.synthesize(ticker, snap, watchlist_entry),
    )
    # Unpack the thesis (a co-product of verdict synthesis) into its
    # own top-level snap key where _render_thesis looks.
    if snap["conviction"].get("status") == "ok":
        snap["thesis"] = snap["conviction"].pop("thesis", {"status": "pending"})
    else:
        snap["thesis"] = {"status": "pending", "reason": "verdict not ok"}

    # Multi-night conviction trajectory. Reads up to the last 20
    # snapshots from disk, extracts each night's 30d final_score,
    # appends today's, and classifies the pattern as rising/stable/
    # decaying/unstable. The dashboard's _render_trajectory consumes
    # snap["trajectory"] directly. First production night returns a
    # single-point series with a friendly message; meaningful
    # classification kicks in at ~5 nights of accumulated history.
    snap["trajectory"] = _run_safely(
        "trajectory",
        lambda: trajectory.build(ticker, snap),
    )

    # Drop the private price-data pass-through before snapshot
    # serialization (keeps snapshot files small). Return it separately
    # so the inline backtest can use it without re-fetching.
    snap.pop("_price_data", None)

    return snap, price_data


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
        "cross_check": {"status": "pending", "reason": reason},
        "conviction": {"status": "pending", "reason": reason},
        "thesis": {"status": "pending", "reason": reason},
    }


def _load_watchlist() -> dict:
    with config.WATCHLIST_PATH.open() as f:
        return yaml.safe_load(f) or {}


# ---------- CLI ----------


def _force_refresh_all_caches() -> None:
    """Clear the per-ticker cache files so the next fetch hits FMP /
    yfinance fresh. Triggered by `--refresh` on the CLI. We delete
    the JSON files but keep the directory structure so subsequent
    writes don't fail on missing dirs.

    What gets cleared:
      data/cache/{TICKER}.json           - FMP prices + profile
      data/cache/catalyst/{TICKER}.json  - catalyst payload (6h TTL normally)
      data/cache/short_interest/{TICKER}.json  - yfinance (24h TTL)
      data/cache/fair_value/{TICKER}.json  - DCF + P/E + analyst PT (24h TTL)

    What stays: data/snapshots/, data/backtest/, data/test/, anything
    that's "output" rather than "cached input."
    """
    import shutil
    cache_root = config.DATA_DIR / "cache"
    if not cache_root.exists():
        return
    cleared = 0
    for item in cache_root.rglob("*.json"):
        try:
            item.unlink()
            cleared += 1
        except OSError:
            pass
    print(f"--refresh: cleared {cleared} cache file(s) from {cache_root}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one engine cycle.")
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
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "force fresh FMP fetches, ignoring the price-data cache TTL. "
            "Use when something just happened (major news, big move) and you "
            "want the absolute latest tick. Otherwise the default 4h cache "
            "TTL handles intraday re-runs gracefully."
        ),
    )
    args = parser.parse_args()

    tickers = [t.upper() for t in args.ticker] if args.ticker else None
    if args.refresh:
        _force_refresh_all_caches()
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
