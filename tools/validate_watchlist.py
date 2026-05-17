"""Schema validator for data/watchlist.yml.

Run by the nightly cron before the engine starts, and locally before
committing watchlist edits. Validates the schema documented in
docs/V1_SPEC.md §3. Exits non-zero on any violation so cron fails loudly
rather than running the engine on bad state.

Usage:
    python tools/validate_watchlist.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.yml"

VALID_TIERS = {"A", "B", "C"}
VALID_USERS = {"aidy", "jesse"}
VALID_STATES = {"watching", "entered"}
ENTERED_REQUIRED_FIELDS = {"state", "entry", "target", "stop", "entered_date"}


def validate(path: Path) -> list[str]:
    errors: list[str] = []

    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"watchlist not found at {path}"]
    except yaml.YAMLError as e:
        return [f"watchlist is not valid YAML: {e}"]

    if not isinstance(data, dict):
        return ["watchlist top level must be a mapping of TICKER -> entry"]

    for ticker, entry in data.items():
        loc = f"{ticker}"

        if not isinstance(ticker, str) or not ticker.isupper() or not ticker.isalnum():
            errors.append(f"{loc}: ticker must be UPPERCASE alphanumeric")

        if not isinstance(entry, dict):
            errors.append(f"{loc}: entry must be a mapping")
            continue

        tier = entry.get("tier")
        if tier not in VALID_TIERS:
            errors.append(f"{loc}.tier: must be one of {sorted(VALID_TIERS)}, got {tier!r}")

        notes = entry.get("notes")
        if not isinstance(notes, str):
            errors.append(f"{loc}.notes: must be a string")

        holders = entry.get("holders")
        if not isinstance(holders, dict) or not holders:
            errors.append(f"{loc}.holders: must be a non-empty mapping")
            continue

        for user, holding in holders.items():
            uloc = f"{loc}.holders.{user}"

            if user not in VALID_USERS:
                errors.append(f"{uloc}: user must be one of {sorted(VALID_USERS)}")
                continue

            if not isinstance(holding, dict):
                errors.append(f"{uloc}: holding must be a mapping")
                continue

            state = holding.get("state")
            if state not in VALID_STATES:
                errors.append(f"{uloc}.state: must be one of {sorted(VALID_STATES)}, got {state!r}")
                continue

            if state == "entered":
                missing = ENTERED_REQUIRED_FIELDS - set(holding.keys())
                if missing:
                    errors.append(f"{uloc}: entered state requires fields {sorted(missing)}")

                for num_field in ("entry", "target", "stop"):
                    val = holding.get(num_field)
                    if val is not None and not isinstance(val, (int, float)):
                        errors.append(f"{uloc}.{num_field}: must be numeric, got {type(val).__name__}")

    return errors


def main() -> int:
    errors = validate(WATCHLIST_PATH)
    if errors:
        print(f"watchlist validation FAILED ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"watchlist OK ({WATCHLIST_PATH})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
