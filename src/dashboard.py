"""Dashboard HTML generation.

See docs/V1_SPEC.md §6. Renders a single static HTML file at
docs/index.html with the summary table, per-ticker deep sections (regime,
catalyst, volatility, fair value, MC, PDE, conviction trajectory, dual
verdict panel), and footer. Charts are server-side SVG.

Stub for now; filled in once the pipeline produces outputs to render.
"""


def render(run_payload: dict) -> str:
    raise NotImplementedError
