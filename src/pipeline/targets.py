"""Per-user target/stop derivation.

For every user holding (or watching) a ticker, decide what target and
stop price the Monte Carlo first-passage simulation should test
against. Two sources:

  - **User-specified** (state == "entered"): the user told us their
    target and stop in the `bought` flow; we use those directly.
    Source label: "user-specified".
  - **Vol-scaled default** (state == "watching"): we derive a
    target/stop pair from the GARCH 30d vol forecast, with asymmetric
    multipliers (target at +2 sigma, stop at -1.5 sigma) that bake
    the swing-trader's "risk less than you stand to gain" principle
    into the default. Source label: "vol-scaled-default".

Why this is a separate pipeline step (not folded into Monte Carlo):

  - Targets are a per-user concept; MC paths are per-ticker. Mixing
    them inside the MC module would blur the abstraction.
  - The dashboard's Action panel needs targets in their own snap
    block so it can render them even if MC fails or is pending.
  - Future enhancements (technical support/resistance, fair-value-
    anchored levels, ATR bands) all belong here, not in MC.

Output schema:

    {
        "status": "ok",
        "current_price": float,
        "users": {
            "aidy": {
                "target_price": float,
                "stop_price": float,
                "target_pct": float,         # +0.092 = +9.2% from current
                "stop_pct": float,           # -0.069 = -6.9% from current
                "source": "user-specified" | "vol-scaled-default",
            },
            ...
        },
        "scaling": {
            "target_sigma_multiplier": float,
            "stop_sigma_multiplier": float,
            "scaling_horizon_days": int,
            "vol_annualized": float,         # vol used for derivation
        },
        "fetched_at": ISO timestamp,
    }

CLI smoke test:

    python -m src.pipeline.targets NVDA AMAT IONQ
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import traceback
from datetime import datetime, timezone

from src import config

logger = logging.getLogger(__name__)

_T = config.THRESHOLDS.targets
_TARGET_MULT = _T.target_sigma_multiplier
_STOP_MULT = _T.stop_sigma_multiplier
_SCALING_HORIZON_DAYS = _T.scaling_horizon_days

TRADING_DAYS_PER_YEAR = 252


# ---------- public API ----------


def derive(ticker: str, watchlist_entry: dict, snap: dict) -> dict:
    """Derive per-user target/stop pairs for a ticker.

    Args:
        ticker: ticker symbol (informational; not used in math).
        watchlist_entry: the ticker's entry from watchlist.yml — used
            for `holders` map and per-holder `state` + (for entered
            users) target/stop.
        snap: the in-progress snapshot dict. Must contain
            snap['data']['profile'] (or current price source) and
            snap['volatility']['forecast_30d_pct'] for the vol-scaled
            defaults to work.

    Returns the payload described in the module docstring. `status` is
    always "ok" unless current_price or volatility are missing — in
    which case status="fail" with reason and no per-user entries.
    """
    del ticker  # informational only
    current_price = _extract_current_price(snap)
    if current_price is None or current_price <= 0:
        return {
            "status": "fail",
            "reason": "current_price unavailable from snap['data']",
        }

    vol_block = snap.get("volatility") or {}
    if vol_block.get("status") != "ok":
        return {
            "status": "fail",
            "reason": "volatility forecast unavailable; can't derive vol-scaled defaults",
        }

    sigma_annual = vol_block.get("forecast_30d_pct")
    if sigma_annual is None or sigma_annual <= 0:
        return {
            "status": "fail",
            "reason": f"invalid 30d vol forecast: {sigma_annual}",
        }

    holders = (watchlist_entry or {}).get("holders") or {}
    users_out: dict[str, dict] = {}
    for user, holder in holders.items():
        users_out[user] = _derive_for_user(user, holder, current_price, sigma_annual)

    return {
        "status": "ok",
        "current_price": current_price,
        "users": users_out,
        "scaling": {
            "target_sigma_multiplier": _TARGET_MULT,
            "stop_sigma_multiplier": _STOP_MULT,
            "scaling_horizon_days": _SCALING_HORIZON_DAYS,
            "vol_annualized": sigma_annual,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- per-user logic ----------


def _derive_for_user(
    user: str, holder: dict, current_price: float, sigma_annual: float
) -> dict:
    """Return target/stop for a single user holding entry."""
    state = (holder or {}).get("state", "watching")
    if state == "entered":
        # User specified target/stop in the `bought` flow
        user_target = holder.get("target")
        user_stop = holder.get("stop")
        if user_target is not None and user_stop is not None:
            return _user_pair(current_price, float(user_target), float(user_stop))
        # Entered but missing target/stop in watchlist — fall back to
        # vol-scaled defaults rather than fail (the catch-22: the user
        # is in the position, we have to give them SOME read).
        logger.warning(
            f"{user} is entered but watchlist has no target/stop; using vol-scaled defaults"
        )

    # watching state (or entered-without-levels fallback)
    return _vol_scaled_pair(current_price, sigma_annual)


def _user_pair(current: float, target: float, stop: float) -> dict:
    return {
        "target_price": target,
        "stop_price": stop,
        "target_pct": (target - current) / current,
        "stop_pct": (stop - current) / current,
        "source": "user-specified",
    }


def _vol_scaled_pair(current: float, sigma_annual: float) -> dict:
    """Compute vol-scaled target/stop using the 30d sigma forecast.

    target = current * exp(+m * sigma * sqrt(horizon_yr))
    stop   = current * exp(-k * sigma * sqrt(horizon_yr))

    Multiplicative (geometric Brownian motion natural) so the same
    sigma multipliers produce symmetric-in-log-space bands regardless
    of price level. m > k for asymmetric reward:risk.
    """
    horizon_years = _SCALING_HORIZON_DAYS / TRADING_DAYS_PER_YEAR
    sigma_horizon = sigma_annual * math.sqrt(horizon_years)
    target = current * math.exp(_TARGET_MULT * sigma_horizon)
    stop = current * math.exp(-_STOP_MULT * sigma_horizon)
    return {
        "target_price": target,
        "stop_price": stop,
        "target_pct": (target - current) / current,
        "stop_pct": (stop - current) / current,
        "source": "vol-scaled-default",
    }


def _extract_current_price(snap: dict) -> float | None:
    """Pull current price from the data block. Prefer profile.price
    (FMP's quote), fall back to last close from the prices array."""
    data = snap.get("data") or {}
    profile = data.get("profile") or {}
    price = profile.get("price")
    if price is not None and price > 0:
        return float(price)
    raw = snap.get("_price_data") or {}
    prices = raw.get("prices") or []
    if prices:
        last = prices[-1].get("adj_close") or prices[-1].get("close")
        if last:
            return float(last)
    return None


# ---------- CLI smoke test ----------


def _print_summary(ticker: str, payload: dict) -> None:
    print(f"\n=== {ticker} ===")
    if payload.get("status") != "ok":
        print(f"  status: {payload.get('status')} - {payload.get('reason', '')}")
        return
    print(f"  current_price: ${payload['current_price']:.2f}")
    s = payload["scaling"]
    print(f"  scaling: vol={s['vol_annualized']*100:.1f}%/yr  horizon={s['scaling_horizon_days']}d  "
          f"target=+{s['target_sigma_multiplier']:.1f}sigma  stop=-{s['stop_sigma_multiplier']:.1f}sigma")
    for user, u in payload["users"].items():
        print(f"  {user} (source={u['source']}):")
        print(f"    target: ${u['target_price']:.2f} ({u['target_pct']*100:+.1f}%)")
        print(f"    stop:   ${u['stop_price']:.2f} ({u['stop_pct']*100:+.1f}%)")


def main() -> int:
    from src.pipeline import data as data_step, volatility as vol_step
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Targets derivation smoke test.")
    parser.add_argument("tickers", nargs="+", help="ticker(s) to derive")
    args = parser.parse_args()

    watchlist = {}
    if config.WATCHLIST_PATH.exists():
        with config.WATCHLIST_PATH.open() as f:
            watchlist = yaml.safe_load(f) or {}

    exit_code = 0
    for ticker in args.tickers:
        ticker = ticker.upper()
        entry = watchlist.get(ticker)
        if entry is None:
            print(f"\nERROR: {ticker} not in watchlist", file=sys.stderr)
            exit_code = 1
            continue
        try:
            price_data = data_step.fetch(ticker)
            vol_payload = vol_step.forecast(ticker, price_data, tier=entry.get("tier"))
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR fetching {ticker}: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue
        snap = {
            "data": {"profile": price_data.get("profile") or {}},
            "_price_data": price_data,
            "volatility": vol_payload,
        }
        payload = derive(ticker, entry, snap)
        _print_summary(ticker, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
