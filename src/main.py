"""Engine entrypoint — invoked by the nightly cron.

Orchestrates the 8-step pipeline (docs/V1_SPEC.md §2) for every ticker in
the watchlist, writes per-ticker snapshots, renders the dashboard.

Stub for now; pipeline steps come online one at a time.
"""


def main() -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
