"""Dashboard HTML renderer.

See docs/V1_SPEC.md §6. Renders a single static HTML file at
docs/index.html. No client-side JS dependencies; inline CSS only;
charts are server-side SVG.

The renderer is structured around the per-ticker card from §6.3, which
mirrors the v17 dip engine's pattern (full-depth deep-dive per ticker,
plain-English at every layer) and adds swing-trader-specific extensions
(three-layer conviction breakdown, dual-horizon, MC↔PDE agreement,
per-user verdicts, conviction trajectory sparkline, §4.2 measured-tier
check).

Each panel reads the snapshot's per-step status field and renders one
of three states:
  - ok       → full content
  - pending  → muted "pending — not yet implemented" placeholder
  - fail     → red error block with the failure message

This means new pipeline steps come online into a slot that already
exists on the dashboard; no dashboard changes needed when steps ship.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from src import config

# ---------- inline stylesheet ----------

_STYLES = """
:root {
  --bg: #f7f7f5;
  --panel: #ffffff;
  --border: #e5e5e5;
  --border-strong: #d4d4d4;
  --text: #111111;
  --text-soft: #525252;
  --muted: #737373;
  --ok: #15803d;
  --ok-bg: #f0fdf4;
  --warn: #b45309;
  --warn-bg: #fffbeb;
  --fail: #b91c1c;
  --fail-bg: #fef2f2;
  --pending: #6b7280;
  --pending-bg: #f9fafb;
  --accent: #1d4ed8;
  --shadow: 0 1px 2px rgba(0,0,0,0.04);
}
* { box-sizing: border-box; }
html, body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 0;
  line-height: 1.5;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1140px; margin: 0 auto; padding: 0 20px 80px; }
.mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; }

/* ---------- top header band ---------- */
header.band {
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 18px 0;
  margin-bottom: 22px;
}
header.band h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
header.band .subtitle { color: var(--muted); font-size: 13px; }
header.band .meta { display: flex; flex-wrap: wrap; gap: 8px 24px; margin-top: 10px; font-size: 12px; color: var(--text-soft); }
header.band .meta strong { color: var(--text); }

/* ---------- section headings ---------- */
h2.section { font-size: 14px; font-weight: 600; margin: 28px 0 10px; color: var(--text-soft); text-transform: uppercase; letter-spacing: 0.04em; }

/* ---------- top-level expandable info blocks ---------- */
details.info-block {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-bottom: 12px;
  box-shadow: var(--shadow);
}
details.info-block > summary {
  padding: 12px 16px;
  cursor: pointer;
  font-weight: 500;
  font-size: 13px;
  list-style: none;
}
details.info-block > summary::-webkit-details-marker { display: none; }
details.info-block > summary::before { content: "▸ "; color: var(--muted); }
details.info-block[open] > summary::before { content: "▾ "; }
details.info-block > .body { padding: 0 16px 16px; font-size: 13px; color: var(--text-soft); }

/* ---------- deployment summary ---------- */
.deployment {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 18px 20px;
  box-shadow: var(--shadow);
  margin-bottom: 24px;
}
.deployment h3 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-soft); margin: 0 0 12px; }
.deployment .row { display: flex; flex-wrap: wrap; gap: 8px 20px; padding: 8px 0; border-top: 1px solid var(--border); align-items: baseline; }
.deployment .row:first-of-type { border-top: 0; }
.deployment .label { font-weight: 600; min-width: 130px; }
.deployment .ticker-list { color: var(--text-soft); font-family: ui-monospace, monospace; font-size: 12.5px; }
.deployment .count { color: var(--muted); font-weight: 500; }

/* ---------- ticker card ---------- */
section.ticker-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin: 20px 0;
  box-shadow: var(--shadow);
  overflow: hidden;
}
section.ticker-card > .card-head {
  padding: 18px 22px;
  border-bottom: 1px solid var(--border);
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 22px;
}
section.ticker-card > .card-head .symbol { font-size: 22px; font-weight: 700; font-family: ui-monospace, monospace; letter-spacing: -0.02em; }
section.ticker-card > .card-head .meta { color: var(--muted); font-size: 12.5px; }
section.ticker-card > .card-body { padding: 4px 22px 22px; }

/* tier badge */
.tier-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; }
.tier-A { background: #e0f2fe; color: #0369a1; }
.tier-B { background: #fef3c7; color: #92400e; }
.tier-C { background: #fee2e2; color: #b91c1c; }

/* per-user state pill */
.user-pill { font-size: 12px; padding: 2px 8px; border-radius: 4px; background: #f4f4f5; color: var(--text-soft); }
.user-pill .name { font-weight: 600; color: var(--text); }
.user-pill.entered { background: #ecfdf5; color: #065f46; }
.user-pill.entered .name { color: #065f46; }

/* status pills */
.pill { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; font-weight: 500; letter-spacing: 0.02em; }
.pill-ok      { background: var(--ok-bg);      color: var(--ok); }
.pill-warn    { background: var(--warn-bg);    color: var(--warn); }
.pill-fail    { background: var(--fail-bg);    color: var(--fail); }
.pill-pending { background: var(--pending-bg); color: var(--pending); font-style: italic; }
.pill-info    { background: #eff6ff; color: var(--accent); }

/* verdict labels */
.verdict { display: inline-block; padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 13px; letter-spacing: 0.02em; }
.verdict-ENTER { background: #dcfce7; color: #166534; }
.verdict-HOLD  { background: #dbeafe; color: #1e40af; }
.verdict-WAIT  { background: #fef3c7; color: #92400e; }
.verdict-TRIM  { background: #fed7aa; color: #9a3412; }
.verdict-SKIP  { background: #fee2e2; color: #991b1b; }
.verdict-EXIT  { background: #fecaca; color: #7f1d1d; }
.verdict-EM    { background: #f3f4f6; color: var(--muted); }

/* sub-panels within a ticker card */
.subpanel { margin: 16px 0; padding: 14px 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
.subpanel.pending { background: var(--pending-bg); }
.subpanel > .head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.subpanel > .head .title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-soft); }
.subpanel > .head .meta { font-size: 12px; color: var(--muted); }

/* thesis paragraph */
.thesis { margin: 14px 0 4px; padding: 14px 16px; background: #fafaf9; border-left: 3px solid var(--accent); border-radius: 0 6px 6px 0; }
.thesis .label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); display: block; margin-bottom: 4px; }
.thesis p { margin: 0; line-height: 1.55; }

/* conviction breakdown */
.conviction-block { padding: 14px 16px; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; margin: 10px 0; }
.conviction-block > .horizon-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 14px; margin-bottom: 12px; }
.conviction-block > .horizon-head .horizon-label { font-size: 13px; font-weight: 600; }
.conviction-block > .horizon-head .score { font-family: ui-monospace, monospace; font-size: 13px; color: var(--text-soft); }

.layer { padding: 10px 12px; border-radius: 5px; margin: 8px 0; background: #fafafa; border: 1px solid var(--border); }
.layer .layer-head { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-soft); margin-bottom: 6px; }
.layer .layer-head .layer-result { float: right; font-weight: 600; font-family: ui-monospace, monospace; color: var(--text); }

.kv-row { display: grid; grid-template-columns: minmax(180px, 1.3fr) 1fr minmax(80px, 0.5fr); gap: 10px 14px; padding: 4px 0; font-size: 12.5px; align-items: center; }
.kv-row .k { color: var(--text-soft); }
.kv-row .v { font-family: ui-monospace, monospace; color: var(--text); }
.kv-row .v-right { text-align: right; font-family: ui-monospace, monospace; }
.kv-row.haircut-applied .v-right { color: var(--warn); font-weight: 600; }
.kv-row.haircut-passed .v-right { color: var(--ok); }
.kv-row.veto-fired { background: #fff7ed; padding: 6px 8px; border-radius: 4px; margin: 4px -8px; }
.kv-row.veto-fired .v-right { color: var(--warn); font-weight: 600; }

/* horizontal bar (for edge/contribution viz) */
.bar { display: inline-block; height: 8px; vertical-align: middle; background: #e5e5e5; border-radius: 2px; position: relative; overflow: hidden; min-width: 80px; }
.bar-fill { height: 100%; background: var(--accent); border-radius: 2px; }
.bar-fill.neutral { background: var(--muted); }
.bar-fill.positive { background: var(--ok); }
.bar-fill.negative { background: var(--fail); }

/* per-user verdict grid at the bottom of each horizon */
.user-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
.user-card { background: #fafafa; border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
.user-card .user-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.user-card .user-head .name { font-weight: 600; }
.user-card .user-head .state { font-size: 11px; color: var(--muted); }
.user-card .verdict-reason { font-size: 12px; color: var(--text-soft); margin: 4px 0 8px; }
.user-card .targets { font-size: 12.5px; color: var(--text); font-family: ui-monospace, monospace; line-height: 1.7; }
.user-card .targets .lbl { color: var(--muted); }
.user-card .copy-paste { background: #1f2937; color: #f3f4f6; padding: 6px 10px; border-radius: 4px; font-family: ui-monospace, monospace; font-size: 11.5px; margin-top: 8px; }
.user-card .copy-paste::before { content: "$ "; color: #9ca3af; }

/* catalyst narrative */
.catalyst-line { display: flex; gap: 12px; align-items: baseline; margin: 6px 0; font-size: 13px; }
.catalyst-line .icon { font-family: ui-monospace, monospace; color: var(--accent); font-weight: 600; font-size: 11px; }
.catalyst-line .label { color: var(--text-soft); min-width: 130px; }
.catalyst-line .value { font-family: ui-monospace, monospace; }
.reactions-table { width: 100%; border-collapse: collapse; margin: 6px 0 10px; font-size: 12.5px; }
.reactions-table th, .reactions-table td { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border); font-family: ui-monospace, monospace; }
.reactions-table th { background: #fafafa; font-weight: 600; color: var(--text-soft); }
.news-bullets { margin: 6px 0 0; padding-left: 18px; font-size: 12.5px; line-height: 1.6; color: var(--text-soft); }
.engine-rec { background: #f0f9ff; border-left: 3px solid var(--accent); padding: 10px 12px; margin-top: 10px; font-size: 12.5px; line-height: 1.55; border-radius: 0 5px 5px 0; }

/* dual-horizon agreement table */
.agreement-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0; }
.agreement-table th, .agreement-table td { padding: 5px 10px; text-align: left; border-bottom: 1px solid var(--border); }
.agreement-table th { background: #fafafa; font-weight: 600; color: var(--text-soft); font-size: 12px; }
.agreement-table .num { font-family: ui-monospace, monospace; text-align: right; }
.agreement-table .agree-ok    { color: var(--ok); font-weight: 600; }
.agreement-table .agree-warn  { color: var(--warn); font-weight: 600; }

/* §4.2 tier-classifier table */
.classifier-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0; }
.classifier-table th, .classifier-table td { padding: 5px 10px; border-bottom: 1px solid var(--border); }
.classifier-table th { background: #fafafa; font-weight: 600; color: var(--text-soft); font-size: 12px; text-align: left; }
.classifier-table .num { font-family: ui-monospace, monospace; text-align: right; }

/* sparkline */
.sparkline-block { display: flex; align-items: center; gap: 18px; }
.sparkline-block svg { flex: 0 0 auto; }
.sparkline-block .description { font-size: 12.5px; color: var(--text-soft); line-height: 1.5; }
.sparkline-block .trend { font-weight: 600; }
.sparkline-block .trend.rising { color: var(--ok); }
.sparkline-block .trend.decaying { color: var(--fail); }
.sparkline-block .trend.unstable { color: var(--warn); }
.sparkline-block .trend.stable { color: var(--text-soft); }

/* ---------- ticker hyperlink (T212) ---------- */
a.t212-link { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--muted); }
a.t212-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
a.t212-link::after { content: " ↗"; font-size: 0.75em; color: var(--muted); }

/* ---------- verdict glossary ---------- */
.glossary-grid { display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; font-size: 12.5px; }
.glossary-grid dt { font-weight: 600; }
.glossary-grid dd { margin: 0; color: var(--text-soft); }

/* ---------- price-levels panel ---------- */
.price-levels { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 14px 16px; margin: 14px 0; }
.price-levels .pl-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
.price-levels .pl-head .title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #0f172a; }
.price-levels .pl-head .current { font-family: ui-monospace, monospace; font-size: 13px; }
.price-levels .pl-head .current strong { font-size: 16px; }
.price-levels .pl-explainer { font-size: 12px; color: var(--text-soft); margin-bottom: 12px; line-height: 1.55; }
.price-levels .pl-horizon { padding: 10px 12px; background: white; border: 1px solid var(--border); border-radius: 5px; margin: 8px 0; }
.price-levels .pl-horizon .pl-hlabel { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-soft); margin-bottom: 8px; }
.price-levels .pl-zone { display: grid; grid-template-columns: 24px minmax(120px, max-content) 1fr max-content; gap: 6px 12px; align-items: baseline; padding: 5px 0; border-top: 1px dotted var(--border); font-family: ui-monospace, monospace; font-size: 12.5px; }
.price-levels .pl-zone:first-of-type { border-top: 0; }
.price-levels .pl-zone .pl-icon { font-family: ui-monospace, monospace; font-size: 13px; }
.price-levels .pl-zone .pl-name { font-family: -apple-system, sans-serif; color: var(--text-soft); }
.price-levels .pl-zone .pl-detail { color: var(--muted); font-family: -apple-system, sans-serif; font-size: 12px; }
.price-levels .pl-zone .pl-value { font-weight: 600; text-align: right; }
.price-levels .pl-zone.dip   .pl-icon { color: var(--fail); }
.price-levels .pl-zone.rally .pl-icon { color: var(--ok); }
.price-levels .pl-zone.stop  .pl-icon { color: var(--warn); }
.price-levels .pl-zone.target .pl-icon { color: var(--accent); }
.price-levels .pl-user-row { font-size: 11.5px; color: var(--muted); padding-left: 36px; margin-top: 4px; line-height: 1.5; }

/* ---------- plain-English explainer blocks (top of technical panels) ---------- */
.what-it-is { background: #fafaf9; border-left: 3px solid var(--accent); padding: 8px 12px; margin-bottom: 10px; font-size: 12.5px; color: var(--text-soft); line-height: 1.55; border-radius: 0 4px 4px 0; }
.what-it-is .label { font-weight: 600; color: var(--text); display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }
.what-it-means { background: #f0f9ff; border-left: 3px solid var(--accent); padding: 8px 12px; margin-top: 10px; font-size: 12.5px; color: var(--text); line-height: 1.55; border-radius: 0 4px 4px 0; }
.what-it-means .label { font-weight: 600; color: var(--accent); display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }

/* ---------- catalyst narrative — uniform typography ---------- */
.catalyst-block { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 13px; line-height: 1.55; }
.catalyst-block .cat-row { display: grid; grid-template-columns: 24px minmax(160px, max-content) 1fr; gap: 6px 14px; padding: 4px 0; align-items: baseline; }
.catalyst-block .cat-row .cat-icon { color: var(--accent); font-weight: 600; font-size: 12px; }
.catalyst-block .cat-row .cat-label { color: var(--text-soft); }
.catalyst-block .cat-row .cat-value { color: var(--text); }
.catalyst-block .cat-section-title { font-size: 12px; color: var(--text-soft); margin: 10px 0 4px; font-weight: 500; }

/* ---------- mobile-friendly table wrapper ---------- */
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 6px 0; }
.table-scroll > table { min-width: 100%; }

/* daily-path table (collapsed by default) */
.daily-path { font-size: 12.5px; width: 100%; border-collapse: collapse; }
.daily-path th, .daily-path td { padding: 3px 8px; border-bottom: 1px dotted var(--border); }
.daily-path th { background: #fafafa; font-weight: 600; color: var(--text-soft); }
.daily-path .num { font-family: ui-monospace, monospace; text-align: right; }
.daily-path .zone-dip   { background: #fef2f2; color: #991b1b; font-weight: 600; }
.daily-path .zone-rally { background: #f0fdf4; color: #166534; font-weight: 600; }

/* footer */
footer.band { margin-top: 56px; padding: 18px 0; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }
footer.band p { margin: 4px 0; }

/* responsive */
@media (max-width: 720px) {
  .kv-row { grid-template-columns: 1fr; gap: 2px; }
  .kv-row .v-right { text-align: left; }
  .user-grid { grid-template-columns: 1fr; }
  section.ticker-card > .card-head { gap: 6px 14px; }
  section.ticker-card > .card-head .symbol { font-size: 18px; }
  .wrap { padding: 0 14px 60px; }
}
"""


# ---------- shared constants ----------


# Trading 212 invest URL for ticker symbols on the dashboard. Open in a
# new tab. (NOTE: T212 URL conventions for non-US tickers will need
# tweaking when we add European names — at that point we'll branch on
# exchange/suffix.)
T212_URL_TEMPLATE = "https://www.trading212.com/trading-instruments/invest/{ticker}"


VERDICT_GLOSSARY = [
    ("ENTER", "Open a new position. Conviction is above the ENTER threshold AND no Layer-3 veto fires on this horizon. The price-levels panel above each card shows the suggested entry zone."),
    ("WAIT",  "Don't enter today, but don't dismiss the ticker either. Either (a) conviction is between the WAIT floor and the ENTER threshold, or (b) a Layer-3 veto (catalyst inside horizon, downtrend regime) is temporarily blocking ENTER. Re-evaluates each night."),
    ("SKIP",  "Don't enter — conviction is below the WAIT floor OR a hard veto (e.g. fair-value extreme, Tier-C illiquidity) fires. Re-evaluates each night but the bar to flip back to actionable is higher than for WAIT."),
    ("HOLD",  "Keep the position. Conviction is above the WAIT floor and the original thesis is intact. Default state for entered positions on a quiet, in-range day."),
    ("TRIM",  "Take partial profits — sell some of the position but not all. Triggered when fair-value premium goes ≥2σ above the engine's fair-value mean OR the regime flips from positive to negative on an entered position. Reduce exposure, don't pre-commit to a full exit."),
    ("EXIT",  "Close the entire position. Triggered when conviction falls below the WAIT floor (thesis fully weakened), the stop level is breached, OR the engine deems the trade structurally broken (regime collapse, fair-value catastrophe)."),
]


# ---------- top-level render ----------


def render(payload: dict) -> str:
    """Build the full HTML document from a run payload."""
    parts: list[str] = []

    parts.append(_render_header(payload))
    parts.append(_render_system_status(payload))
    parts.append(_render_data_quality(payload))
    parts.append(_render_backtest(payload))
    parts.append(_render_deployment(payload))
    parts.append(_render_verdict_glossary())

    parts.append("<h2 class='section'>Per-ticker deep reports</h2>")
    for ticker in payload.get("watchlist", {}).keys():
        snap = payload.get("tickers", {}).get(ticker)
        if snap is None:
            continue
        parts.append(_render_ticker_card(ticker, snap, payload["watchlist"][ticker]))

    parts.append(_render_footer(payload))

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SGC Swing Trader — {html.escape(payload.get('run_date', ''))}</title>
<style>{_STYLES}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


# ---------- top sections ----------


def _render_header(payload: dict) -> str:
    run_date = payload.get("run_date", "?")
    started = payload.get("run_started_at", "?")
    finished = payload.get("run_finished_at", "?")
    n_tick = len(payload.get("tickers", {}))
    n_err = len(payload.get("errors", []))
    market_regime = (payload.get("market_context") or {}).get("regime", "—")
    vix = (payload.get("market_context") or {}).get("vix", "—")

    err_html = f"<span class='pill pill-fail'>{n_err} run error(s)</span>" if n_err else ""

    return f"""
<header class="band">
  <div class="wrap" style="padding: 0;">
    <h1>SGC Swing Trader</h1>
    <div class="subtitle">Conviction + timing for {n_tick} ticker(s) across {", ".join(f"{h}d" for h in config.HORIZONS)} horizons. Per-user verdicts for Aidy and Jesse.</div>
    <div class="meta">
      <span>Run date: <strong>{html.escape(run_date)}</strong></span>
      <span>Started: {html.escape(started)}</span>
      <span>Finished: {html.escape(finished)}</span>
      <span>Market: <strong>{html.escape(str(market_regime))}</strong> (VIX {html.escape(str(vix))})</span>
      {err_html}
    </div>
  </div>
</header>
"""


def _render_system_status(payload: dict) -> str:
    t = config.THRESHOLDS
    return f"""
<h2 class='section'>System status &amp; live thresholds</h2>
<details class='info-block'>
  <summary>Engine settings (from config/thresholds.yml)</summary>
  <div class='body'>
    <div class='kv-row'><span class='k'>Horizons (days)</span><span class='v'>{config.HORIZONS[0]} / {config.HORIZONS[1]}</span><span></span></div>
    <div class='kv-row'><span class='k'>Monte Carlo paths per (ticker × horizon)</span><span class='v'>{config.MC_PATHS:,}</span><span></span></div>
    <div class='kv-row'><span class='k'>ENTER conviction threshold</span><span class='v'>{t.conviction.vetoes.enter_score_threshold:.2f}</span><span></span></div>
    <div class='kv-row'><span class='k'>WAIT score floor</span><span class='v'>{t.conviction.vetoes.wait_score_floor:.2f}</span><span></span></div>
    <div class='kv-row'><span class='k'>Edge weights (EV / probability)</span><span class='v'>{t.conviction.edge.ev_weight:.2f} / {t.conviction.edge.probability_weight:.2f}</span><span></span></div>
    <div class='kv-row'><span class='k'>Regime veto regimes</span><span class='v'>{", ".join(t.conviction.vetoes.regime.enter_veto_regimes)} (≥ {t.conviction.vetoes.regime.enter_veto_min_confidence*100:.0f}% confidence)</span><span></span></div>
    <div class='kv-row'><span class='k'>MC↔PDE agreement tolerance</span><span class='v'>{t.cross_check.p_target_agreement_tolerance_pp:.1f}pp on P(target)</span><span></span></div>
    <div class='kv-row'><span class='k'>§4.2 classifier vol bands (A/B/C upper)</span><span class='v'>{t.tier_classifier.vol_annualized_bounds.A:.0%} / {t.tier_classifier.vol_annualized_bounds.B:.0%} / +∞</span><span></span></div>
    <div class='kv-row' style='border-top: 1px solid var(--border); padding-top: 8px; margin-top: 6px;'><span class='k' style='font-style: italic;'>All thresholds calibratable in <span class='mono'>config/thresholds.yml</span> — tuning is a YAML edit + commit, no code changes.</span><span></span><span></span></div>
  </div>
</details>
"""


def _render_data_quality(payload: dict) -> str:
    warnings = payload.get("data_quality_warnings", [])
    n = len(warnings)
    if n == 0:
        return ""
    items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
    return f"""
<details class='info-block'>
  <summary><span class='pill pill-warn'>{n}</span> Data-quality warning(s) — click to expand</summary>
  <div class='body'>
    <ul style='margin: 0; padding-left: 18px;'>{items}</ul>
  </div>
</details>
"""


def _render_backtest(payload: dict) -> str:
    backtest = payload.get("backtest")
    if backtest is None or backtest.get("status") == "pending":
        return """
<details class='info-block'>
  <summary>Backtest — <span class='pill pill-pending'>pending</span></summary>
  <div class='body'>The backtest harness is deferred to v1.x. Once it ships, this panel will show hit rate per ticker, per-tier accuracy bands, and ROI vs naive baseline (mirroring v17 dip-engine format).</div>
</details>
"""
    # When the backtest is live this is where the v17-style hit-rate panel renders.
    return f"""
<details class='info-block' open>
  <summary>Backtest — hit rate {backtest['hit_rate_pct']:.0f}% ({backtest['hits']}/{backtest['total']})</summary>
  <div class='body'>{html.escape(backtest.get('summary', ''))}</div>
</details>
"""


def _render_deployment(payload: dict) -> str:
    """Group verdicts by label and horizon. When all tracking users agree
    on a (ticker, horizon, verdict) tuple, don't qualify by user. Only
    show per-user qualifier when verdicts diverge across users on the
    same ticker+horizon (e.g. AMAT where Aidy is watching and Jesse is
    entered)."""
    tickers = payload.get("tickers", {})
    watchlist = payload.get("watchlist", {})

    # Build:   {(label, horizon_days): [(ticker, [users_with_this_verdict])]}
    grouped: dict[tuple, dict[str, list[str]]] = {}

    for ticker, snap in tickers.items():
        per_horizon = ((snap.get("conviction") or {}).get("horizons")) or {}
        if not per_horizon:
            continue
        holders = (watchlist.get(ticker) or {}).get("holders") or {}
        for horizon_days, users_block in per_horizon.items():
            for user in config.USERS:
                if user not in holders or user not in users_block:
                    continue
                label = users_block[user]["breakdown"]["verdict_label"]
                key = (label, int(horizon_days))
                grouped.setdefault(key, {}).setdefault(ticker, []).append(user)

    label_order = ("ENTER", "HOLD", "WAIT", "TRIM", "SKIP", "EXIT", "—")
    horizon_order = sorted(set(h for (_, h) in grouped.keys()))

    rows: list[str] = []
    for horizon_days in horizon_order:
        for label in label_order:
            entries = grouped.get((label, horizon_days))
            if not entries:
                continue
            ticker_chunks = []
            for ticker, users in entries.items():
                holders = (watchlist.get(ticker) or {}).get("holders") or {}
                all_tracking = [u for u in config.USERS if u in holders]
                # If every tracking user has this same (label, horizon),
                # don't qualify — applies to both.
                if set(users) == set(all_tracking):
                    user_qual = ""
                else:
                    user_qual = f" ({', '.join(u.title() for u in users)} only)"
                ticker_link = _ticker_link(ticker)
                ticker_chunks.append(f"{ticker_link}{user_qual}")

            rows.append(
                f"<div class='row'>"
                f"<span class='label'><span class='verdict verdict-{html.escape(label)}'>{html.escape(label)} {horizon_days}d</span></span>"
                f"<span class='count'>({len(entries)})</span>"
                f"<span class='ticker-list'>{' · '.join(ticker_chunks)}</span>"
                f"</div>"
            )

    if not rows:
        rows.append("<div class='row'><span class='label'>—</span><span class='count'>No actionable verdicts this run.</span></div>")

    return f"""
<div class='deployment'>
  <h3>Today's deployment ({html.escape(payload.get('run_date', '?'))})</h3>
  {"".join(rows)}
  <div style='margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 11.5px; color: var(--muted); line-height: 1.5;'>
    Verdicts unqualified by user apply to both Aidy and Jesse (they share the watchlist universe).
    A per-user qualifier appears only when Aidy and Jesse have diverged — typically because one has bought into a position the other is still watching.
    Definitions of each label are in the glossary below.
  </div>
</div>
"""


def _render_verdict_glossary() -> str:
    items = "".join(
        f"<dt><span class='verdict verdict-{html.escape(label)}'>{html.escape(label)}</span></dt>"
        f"<dd>{html.escape(definition)}</dd>"
        for label, definition in VERDICT_GLOSSARY
    )
    return f"""
<details class='info-block'>
  <summary>What do <span class='verdict verdict-ENTER'>ENTER</span> / <span class='verdict verdict-WAIT'>WAIT</span> / <span class='verdict verdict-HOLD'>HOLD</span> / <span class='verdict verdict-TRIM'>TRIM</span> / <span class='verdict verdict-SKIP'>SKIP</span> / <span class='verdict verdict-EXIT'>EXIT</span> mean?</summary>
  <div class='body'>
    <dl class='glossary-grid'>{items}</dl>
  </div>
</details>
"""


def _ticker_link(ticker: str) -> str:
    """Hyperlink a ticker symbol to its Trading 212 invest page,
    opening in a new tab. Used in the deployment summary and in the
    per-ticker card header."""
    url = T212_URL_TEMPLATE.format(ticker=ticker)
    return (
        f"<a class='t212-link mono' href='{html.escape(url)}' "
        f"target='_blank' rel='noopener noreferrer'>{html.escape(ticker)}</a>"
    )


# ---------- per-ticker card ----------


def _render_ticker_card(ticker: str, snap: dict, watchlist_entry: dict) -> str:
    tier = snap.get("tier_anchor", "?")
    sector = ((snap.get("data") or {}).get("profile") or {}).get("sector") or "?"
    mkt_cap = ((snap.get("data") or {}).get("profile") or {}).get("market_cap")
    holders = watchlist_entry.get("holders") or {}

    user_pills = []
    for user in config.USERS:
        if user not in holders:
            continue
        state = holders[user].get("state", "watching")
        state_class = "entered" if state == "entered" else "watching"
        entry = holders[user].get("entry")
        suffix = f" @ ${entry:.2f}" if entry else ""
        user_pills.append(
            f"<span class='user-pill {state_class}'><span class='name'>{user.title()}</span>: {state}{html.escape(suffix)}</span>"
        )

    sections = [
        _render_thesis(snap),
        _render_price_levels(snap, watchlist_entry),
        _render_conviction(snap, watchlist_entry),
        _render_catalyst(snap),
        _render_regime(snap),
        _render_volatility(snap),
        _render_fair_value(snap),
        _render_cross_check(snap),
        _render_classifier(snap),
        _render_trajectory(snap),
        _render_data(snap),
        _render_daily_path(snap),
    ]

    return f"""
<section class='ticker-card' id='ticker-{html.escape(ticker)}'>
  <div class='card-head'>
    <span class='symbol'>{_ticker_link(ticker)}</span>
    <span class='tier-badge tier-{html.escape(tier)}'>Tier {html.escape(tier)}</span>
    {" ".join(user_pills)}
    <span class='meta'>{html.escape(sector)} · {_fmt_money(mkt_cap)} mkt cap · as of {html.escape(snap.get('as_of', '?'))}</span>
  </div>
  <div class='card-body'>
{"".join(sections)}
  </div>
</section>
"""


# ---------- per-ticker panels ----------


def _render_thesis(snap: dict) -> str:
    thesis = snap.get("thesis")
    if not thesis or thesis.get("status") == "pending":
        return _pending_panel("Thesis", "Plain-English synthesis of regime + catalyst + valuation + cross-check + tier. Generated each run from the conviction-engine inputs. Will appear once step 8 (verdict synthesis) is wired up.")
    return f"""
<div class='thesis'>
  <span class='label'>Thesis</span>
  <p>{html.escape(thesis['text'])}</p>
</div>
"""


def _render_price_levels(snap: dict, watchlist_entry: dict) -> str:
    """Actionable price levels panel — mirrors the v17 dip-engine pattern:
    dip entry zone + rally sell zone at the configured conviction
    percentile, plus per-user target/stop derived from the conviction
    breakdown's targets block."""
    pl = snap.get("price_levels")
    if not pl or pl.get("status") == "pending":
        return _pending_panel("Price levels — dip entry, rally sell, stop, target", "Will appear once step 6 (Monte Carlo) ships. Pattern mirrors the v17 dip engine: the dip-entry price is the level at which the configured percentile of MC paths touches on the way down; rally-sell is the same on the way up. Date ranges are ±7 trading sessions around the median touch date.")

    current = pl.get("current_price")
    rsi = pl.get("rsi")
    high_60d = pl.get("high_60d")

    pct = config.THRESHOLDS.price_levels.dip_conviction_percentile
    holders = watchlist_entry.get("holders") or {}

    horizon_blocks: list[str] = []
    for h in config.HORIZONS:
        zones = (pl.get("horizons") or {}).get(h) or (pl.get("horizons") or {}).get(str(h))
        if not zones:
            continue
        dip = zones.get("dip", {})
        rally = zones.get("rally", {})
        dip_pct = (dip.get("price", 0) - current) / current * 100 if current else 0
        rally_pct = (rally.get("price", 0) - current) / current * 100 if current else 0

        # Per-user target/stop summary lines.
        per_user_lines = []
        conviction_block = (snap.get("conviction") or {}).get("horizons", {}).get(h, {})
        for user in config.USERS:
            if user not in holders:
                continue
            state = holders[user].get("state", "watching")
            targets = (conviction_block.get(user) or {}).get("targets") or {}
            stop = targets.get("stop")
            target = targets.get("target")
            entry = holders[user].get("entry") if state == "entered" else None
            pieces = [f"<strong>{user.title()}</strong> ({state})"]
            if state == "entered" and entry:
                pieces.append(f"in @ ${entry:.2f}")
            if target:
                t_pct = (target - current) / current * 100 if current else 0
                pieces.append(f"target ${target:.2f} ({t_pct:+.1f}%)")
            if stop:
                s_pct = (stop - current) / current * 100 if current else 0
                pieces.append(f"stop ${stop:.2f} ({s_pct:+.1f}%)")
            per_user_lines.append(f"<div class='pl-user-row'>{' · '.join(pieces)}</div>")

        dip_html = "" if not dip else f"""
<div class='pl-zone dip'>
  <span class='pl-icon'>⬇</span>
  <span class='pl-name'>Dip entry zone</span>
  <span class='pl-detail'>${dip['price']:.2f} ({dip_pct:+.1f}%) · {html.escape(dip['date_range'])} · {int(pct*100)}% of MC paths touch this</span>
  <span class='pl-value'>${dip['price']:.2f}</span>
</div>
"""
        rally_html = "" if not rally else f"""
<div class='pl-zone rally'>
  <span class='pl-icon'>⬆</span>
  <span class='pl-name'>Rally sell zone</span>
  <span class='pl-detail'>${rally['price']:.2f} ({rally_pct:+.1f}%) · {html.escape(rally['date_range'])} · {int(pct*100)}% of MC paths touch this</span>
  <span class='pl-value'>${rally['price']:.2f}</span>
</div>
"""
        horizon_blocks.append(f"""
<div class='pl-horizon'>
  <div class='pl-hlabel'>{h}-day horizon</div>
  {dip_html}
  {rally_html}
  {"".join(per_user_lines)}
</div>
""")

    current_str = f"<strong>${current:.2f}</strong>" if current else "?"
    extras = []
    if rsi is not None:
        extras.append(f"RSI {rsi}")
    if high_60d is not None:
        extras.append(f"60d high ${high_60d:.2f}")
    extras_html = " · " + " · ".join(extras) if extras else ""

    return f"""
<div class='price-levels'>
  <div class='pl-head'>
    <span class='title'>Price levels — where to act</span>
    <span class='current'>Current: {current_str}{extras_html}</span>
  </div>
  <div class='pl-explainer'>
    Dip-entry and rally-sell prices are derived from {config.MC_PATHS:,} Monte Carlo paths over each horizon: the level at which <strong>{int(pct*100)}%</strong> of simulated futures touch on the way down (dip) or up (rally). Date ranges show the ±{config.THRESHOLDS.price_levels.date_range_half_width_sessions}-session window around the median touch date.
  </div>
  {"".join(horizon_blocks)}
</div>
"""


def _render_conviction(snap: dict, watchlist_entry: dict) -> str:
    block = snap.get("conviction")
    if not block or block.get("status") == "pending":
        return _pending_panel("Conviction (3-layer breakdown)", "Three-layer scoring (edge × confidence × veto-check) per horizon × per user. Will appear once steps 2/3/4/6/7 are live and verdict step 8 starts assembling.")

    holders = watchlist_entry.get("holders") or {}

    # block schema:
    #   {
    #     "status": "ok",
    #     "horizons": {
    #         30: {
    #             "users": {
    #                 "aidy":  { breakdown from conviction.evaluate(), plus targets },
    #                 "jesse": { ... },
    #             }
    #         },
    #         60: {...}
    #     }
    #   }

    horizon_blocks = []
    for h in config.HORIZONS:
        per_horizon = block["horizons"].get(h)
        if per_horizon is None:
            continue
        horizon_blocks.append(_render_one_horizon(h, per_horizon, holders))

    return f"""
<div class='subpanel'>
  <div class='head'>
    <span class='title'>Conviction (3-layer breakdown)</span>
    <span class='meta'>per horizon × per user — math anchored in <a href='#' style='color: var(--accent); text-decoration: none;'>spec §5.1</a></span>
  </div>
  {"".join(horizon_blocks)}
</div>
"""


def _render_one_horizon(horizon_days: int, per_horizon: dict, holders: dict) -> str:
    # per_horizon is keyed directly by user: {"aidy": {...}, "jesse": {...}}.
    # We display the breakdown once per user since user_state can affect
    # which vetoes fire.
    user_columns = []
    for user in config.USERS:
        if user not in per_horizon:
            continue
        u = per_horizon[user]
        breakdown = u["breakdown"]
        targets = u.get("targets", {})
        verdict_label = breakdown["verdict_label"]
        score = breakdown["final_score"]
        reason = breakdown["verdict_reason"]
        state = holders.get(user, {}).get("state", "watching")

        # Layer-by-layer rendering
        layer1 = _render_layer1(breakdown["layer1_edge"])
        layer2 = _render_layer2(breakdown["layer2_confidence"])
        layer3 = _render_layer3(breakdown["layer3_vetoes"])

        # User verdict block
        user_columns.append(f"""
<div class='conviction-block' style='margin: 12px 0;'>
  <div class='horizon-head'>
    <span class='horizon-label'>{horizon_days}d horizon — {user.title()} ({html.escape(state)})</span>
    <span class='verdict verdict-{html.escape(verdict_label)}'>{html.escape(verdict_label)}</span>
    <span class='score'>final score {score:.2f}</span>
    <span class='meta' style='color: var(--muted); font-size: 12px;'>{html.escape(reason)}</span>
  </div>
  {layer1}
  {layer2}
  {layer3}
  {_render_user_targets(user, state, targets, verdict_label)}
</div>
""")
    return "".join(user_columns)


def _render_layer1(layer1: dict) -> str:
    components = layer1.get("components", [])
    lottery = layer1.get("lottery_filter_failed", False)
    rows = []
    for c in components:
        bar_pct = min(100, max(0, c["normalized"] * 100))
        rows.append(f"""
<div class='kv-row'>
  <span class='k'>{html.escape(c['name'])}</span>
  <span class='v'>{html.escape(c['display'])} <span class='bar' style='width: 80px; vertical-align: middle; margin-left: 8px;'><span class='bar-fill positive' style='width: {bar_pct}%;'></span></span></span>
  <span class='v-right'>×{c['weight']:.2f} → +{c['contribution']:.3f}</span>
</div>
""")
    lottery_note = ""
    if lottery:
        lottery_note = "<div class='kv-row veto-fired'><span class='k'>Lottery filter</span><span class='v'>P(target) ≤ P(stop) — structurally bad trade</span><span class='v-right'>edge → 0</span></div>"
    return f"""
<div class='layer'>
  <div class='layer-head'>Layer 1 — Edge (does the math say yes?) <span class='layer-result'>edge {layer1['score']:.2f}</span></div>
  {"".join(rows)}
  {lottery_note}
</div>
"""


def _render_layer2(layer2: dict) -> str:
    rows = []
    for h in layer2.get("haircuts", []):
        applied = h["haircut"] > 0
        cls = "haircut-applied" if applied else "haircut-passed"
        right = f"−{h['haircut']*100:.0f}% haircut" if applied else "no haircut"
        rows.append(f"""
<div class='kv-row {cls}'>
  <span class='k'>{html.escape(h['name'])}</span>
  <span class='v' style='color: var(--text-soft); font-family: inherit; font-size: 12px;'>{html.escape(h['detail'])}</span>
  <span class='v-right'>{right}</span>
</div>
""")
    return f"""
<div class='layer'>
  <div class='layer-head'>Layer 2 — Confidence (should we trust the math?) <span class='layer-result'>×{layer2['multiplier']:.2f}</span></div>
  {"".join(rows)}
</div>
"""


def _render_layer3(layer3: dict) -> str:
    rows = []
    for v in layer3.get("all_checks", []):
        if v["fires"]:
            rows.append(f"""
<div class='kv-row veto-fired'>
  <span class='k'>⚠ {html.escape(v['name'])}</span>
  <span class='v' style='font-family: inherit; font-size: 12px;'>{html.escape(v['detail'])}</span>
  <span class='v-right'>{html.escape(v['effect'] or '')}</span>
</div>
""")
        else:
            rows.append(f"""
<div class='kv-row'>
  <span class='k'>{html.escape(v['name'])}</span>
  <span class='v' style='color: var(--text-soft); font-family: inherit; font-size: 12px;'>{html.escape(v['detail'])}</span>
  <span class='v-right'><span class='pill pill-ok'>no veto</span></span>
</div>
""")
    fired_count = len(layer3.get("fired", []))
    summary = f"{fired_count} veto(es) fired" if fired_count else "no vetoes fired"
    return f"""
<div class='layer'>
  <div class='layer-head'>Layer 3 — Vetoes (any reason to back off?) <span class='layer-result'>{summary}</span></div>
  {"".join(rows)}
</div>
"""


def _render_user_targets(user: str, state: str, targets: dict, verdict_label: str) -> str:
    if not targets:
        return ""
    entry = targets.get("entry") or targets.get("current_position_entry")
    target = targets.get("target")
    stop = targets.get("stop")
    copy_paste = targets.get("copy_paste") or ""

    lines = []
    if state == "entered" and entry:
        lines.append(f"<div><span class='lbl'>Current position:</span> entered @ ${entry:.2f}</div>")
        if target:
            lines.append(f"<div><span class='lbl'>Target:</span> ${target:.2f}</div>")
        if stop:
            lines.append(f"<div><span class='lbl'>Stop:</span> ${stop:.2f}</div>")
    else:
        if entry:
            lines.append(f"<div><span class='lbl'>Suggested entry:</span> ${entry}</div>")
        if target:
            lines.append(f"<div><span class='lbl'>Suggested target:</span> ${target:.2f}</div>")
        if stop:
            lines.append(f"<div><span class='lbl'>Suggested stop:</span> ${stop:.2f}</div>")
    return f"""
<div class='user-card' style='margin-top: 12px;'>
  <div class='user-head'><span class='name'>{user.title()} action</span><span class='state'>{html.escape(state)}</span></div>
  <div class='targets'>{"".join(lines)}</div>
  {f"<div class='copy-paste'>{html.escape(copy_paste)}</div>" if copy_paste else ""}
</div>
"""


def _render_catalyst(snap: dict) -> str:
    cat = snap.get("catalyst")
    if not cat or cat.get("status") == "pending":
        return _pending_panel("Catalyst narrative", "Earnings/FDA/ex-div distance, options-implied move, last-N reactions, recent news + analyst revisions. Will appear once step 3 ships.")

    distance = cat.get("distance_sessions")
    next_event = cat.get("next_event")
    implied = cat.get("options_implied_move_pct")
    reactions = cat.get("historical_reactions") or []
    news = cat.get("news_bullets") or []
    analyst = cat.get("analyst_revisions") or {}
    rec = cat.get("engine_recommendation") or ""

    rows = []
    if next_event:
        rows.append(f"""<div class='cat-row'><span class='cat-icon'>⚡</span><span class='cat-label'>Next event</span><span class='cat-value'>{html.escape(next_event['type'])} — {html.escape(next_event['date'])} ({distance} session{'s' if distance != 1 else ''} away)</span></div>""")
    else:
        rows.append(f"""<div class='cat-row'><span class='cat-icon'>·</span><span class='cat-label'>Next event</span><span class='cat-value'>None scheduled</span></div>""")
    if implied is not None:
        rows.append(f"""<div class='cat-row'><span class='cat-icon'>±</span><span class='cat-label'>Options-implied move</span><span class='cat-value'>±{implied*100:.1f}% (event-day straddle)</span></div>""")
    if analyst:
        rows.append(f"""<div class='cat-row'><span class='cat-icon'>📊</span><span class='cat-label'>Analyst revisions (14d)</span><span class='cat-value'>{analyst.get('count', '?')} updates, avg PT ${analyst.get('avg_pt', '?')} ({html.escape(analyst.get('trend', ''))})</span></div>""")

    reactions_html = ""
    if reactions:
        avg = sum(r["reaction_pct"] for r in reactions) / len(reactions)
        body = "".join(
            f"<tr><td>{html.escape(r['date'])}</td><td>{html.escape(r.get('type', 'earnings'))}</td><td class='num'>{r['reaction_pct']:+.1f}%</td></tr>"
            for r in reactions
        )
        reactions_html = f"""
<div class='cat-section-title'>Last {len(reactions)} event reactions (avg {avg:+.1f}%):</div>
<div class='table-scroll'>
  <table class='reactions-table'>
    <thead><tr><th>Date</th><th>Type</th><th style='text-align:right;'>Reaction</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>
"""

    news_html = ""
    if news:
        news_html = (
            f"<div class='cat-section-title'>Recent news ({len(news)} headlines):</div>"
            f"<ul class='news-bullets'>{''.join(f'<li>{html.escape(n)}</li>' for n in news)}</ul>"
        )

    rec_html = f"<div class='engine-rec'><strong>Engine read:</strong> {html.escape(rec)}</div>" if rec else ""

    return f"""
<div class='subpanel'>
  <div class='head'>
    <span class='title'>Catalyst narrative</span>
    <span class='meta'>step 3 — catalyst detection</span>
  </div>
  <div class='catalyst-block'>
    {"".join(rows)}
    {reactions_html}
    {news_html}
    {rec_html}
  </div>
</div>
"""


_REGIME_PLAIN_ENGLISH = {
    "uptrend_quiet":  "Sustained upward trend with normal day-to-day price swings — the calmest, most predictable bullish regime.",
    "uptrend_noisy":  "Trending upward but with elevated daily volatility — the direction is right but the path is choppy.",
    "downtrend":      "Sustained downward trend with persistent selling pressure — historically these don't reverse quickly.",
    "sideways":       "Range-bound, no clear direction — neither uptrend nor downtrend; price oscillates around a flat mean.",
    "crisis":         "Extreme volatility, breakdown of normal regime structure — typical of macro shocks or company-specific catastrophes.",
}


def _render_regime(snap: dict) -> str:
    r = snap.get("regime")
    if not r or r.get("status") == "pending":
        return _pending_panel("Regime", "What the recent price action says about the prevailing direction and volatility character. Will appear once step 2 (HMM regime detector) ships.")
    state = r.get("state", "?")
    conf = r.get("confidence", 0.0)
    duration = r.get("days_in_regime", 0)
    drift = r.get("annualized_drift_implied", 0.0)
    narrative = r.get("narrative", "")
    veto_active = r.get("veto_active", False)
    veto_class = "veto-fired" if veto_active else ""

    state_meaning = _REGIME_PLAIN_ENGLISH.get(state, "")

    if veto_active:
        means = "Layer-3 ENTER veto is active — engine recommends WAIT for a reversal signal (RSI recovering, 20-day momentum turning positive, volume on green days) before any new entry. Existing positions get explicit regime-risk language but no auto-EXIT."
    elif state.startswith("uptrend"):
        means = "Regime supports new entries: drift fed to MC is positive, no Layer-3 veto. The conviction breakdown still has to clear the ENTER threshold — regime is necessary but not sufficient."
    elif state == "sideways":
        means = "No directional edge from regime — drift fed to MC is near zero, no Layer-3 veto. Entries here rely on edge from other factors (catalysts, fair-value, mean-reversion)."
    else:
        means = "Drift fed to MC reflects regime direction. Watch for confidence ≥ 70% if state is downtrend/crisis — that threshold triggers the Layer-3 ENTER veto."

    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Regime — what the price action says</span><span class='meta'>step 2 — HMM</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    A statistical model classifies the past ~60 sessions into one of five regimes
    (uptrend-quiet, uptrend-noisy, downtrend, sideways, crisis). The current regime + its
    confidence drive (1) the daily drift parameter inside Monte Carlo (Layer 1 of conviction)
    and (2) the Layer-3 ENTER veto when state is downtrend/crisis with ≥70% confidence.
  </div>
  <div class='kv-row {veto_class}'>
    <span class='k'>Current state</span>
    <span class='v'>{html.escape(state)} — <span style='color: var(--text-soft); font-family: inherit;'>{html.escape(state_meaning)}</span></span>
    <span class='v-right'>{conf*100:.0f}% confidence{' — VETO active' if veto_active else ''}</span>
  </div>
  <div class='kv-row'><span class='k'>Days in this regime</span><span class='v'>{duration} sessions</span><span class='v-right' style='color: var(--muted);'>longer = more durable</span></div>
  <div class='kv-row'><span class='k'>Implied annual drift (fed to MC)</span><span class='v'>{drift*100:+.1f}% per year</span><span class='v-right' style='color: var(--muted);'>this number directly shifts P(target)</span></div>
  {f"<div style='font-size: 12.5px; color: var(--text-soft); margin-top: 8px; line-height: 1.55;'>{html.escape(narrative)}</div>" if narrative else ""}
  <div class='what-it-means'>
    <span class='label'>What this means for you</span>
    {html.escape(means)}
  </div>
</div>
"""


def _render_volatility(snap: dict) -> str:
    v = snap.get("volatility")
    if not v or v.get("status") == "pending":
        return _pending_panel("Volatility forecast — how much the price typically swings", "Will appear once step 4 (GARCH(1,1) vol model) ships. Will show current realized vol vs forward forecast for both horizons, with confidence band widened by tier.")
    current = v.get("current_realized_pct", 0.0)
    forecast_30 = v.get("forecast_30d_pct", 0.0)
    forecast_60 = v.get("forecast_60d_pct", 0.0)
    band_width = v.get("confidence_band", "tight")
    daily_swing_pct = current * 100 / (252 ** 0.5)  # de-annualize for plain-English daily figure

    # Forecast direction interpretation
    if forecast_30 < current * 0.92:
        forecast_meaning = "Vol forecast to fall meaningfully — calmer days expected. Tighter stops can be justified."
    elif forecast_30 > current * 1.08:
        forecast_meaning = "Vol forecast to rise — choppier days ahead. Stops and targets should be set wider to avoid being shaken out by normal noise."
    else:
        forecast_meaning = "Vol forecast roughly flat — set stop/target distances proportional to current realized vol."

    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Volatility — how much the price typically swings</span><span class='meta'>step 4 — GARCH(1,1)</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    Realized volatility = the standard deviation of daily returns, scaled to an annual figure
    (multiply by √252). A 30% annualized vol means the stock's typical year-over-year price
    swing is about 30% of its own price. GARCH(1,1) is a model that captures how today's
    vol depends on recent vol (vol tends to cluster — calm follows calm, wild follows wild).
  </div>
  <div class='kv-row'><span class='k'>Current realized vol (60d trailing)</span><span class='v'>{current*100:.1f}% annualized</span><span class='v-right' style='color: var(--muted);'>≈ {daily_swing_pct:.1f}% typical daily move</span></div>
  <div class='kv-row'><span class='k'>Forecast end-of-30d</span><span class='v'>{forecast_30*100:.1f}% annualized</span><span></span></div>
  <div class='kv-row'><span class='k'>Forecast end-of-60d</span><span class='v'>{forecast_60*100:.1f}% annualized</span><span></span></div>
  <div class='kv-row'><span class='k'>Confidence band (set by tier)</span><span class='v'>{html.escape(band_width)}</span><span class='v-right' style='color: var(--muted);'>Tier A tight, B moderate, C wide</span></div>
  <div class='what-it-means'>
    <span class='label'>What this means for you</span>
    {html.escape(forecast_meaning)} Higher vol also means the dip-entry and rally-sell zones in the price-levels panel will be further from current price — the model expects more movement in both directions, so the actionable levels spread out.
  </div>
</div>
"""


def _render_fair_value(snap: dict) -> str:
    fv = snap.get("fair_value")
    if not fv or fv.get("status") == "pending":
        return _pending_panel("Fair value — what the stock is fundamentally worth", "Will appear once step 5 (multi-method fair-value triangulation) ships. Will show fair-value range, current price vs mean (in standard deviations), and which methods contributed.")
    low = fv.get("range_low", 0.0)
    mean = fv.get("range_mean", 0.0)
    high = fv.get("range_high", 0.0)
    current = fv.get("current_price", 0.0)
    sigmas = fv.get("premium_sigmas", 0.0)
    methods = fv.get("methods", [])

    # Plain-English interpretation of the sigma figure
    if sigmas <= -1.5:
        sigma_meaning = "Meaningfully cheap — well below the fair-value mean. Supports new entries (no Layer-3 veto from valuation)."
        sigma_class = "ok"
    elif sigmas <= -0.5:
        sigma_meaning = "Modestly cheap — slight tailwind for new entries."
        sigma_class = "ok"
    elif sigmas <= 0.5:
        sigma_meaning = "Fairly valued — no valuation-driven push in either direction."
        sigma_class = "info"
    elif sigmas <= 1.5:
        sigma_meaning = "Modestly expensive — headwind for new entries but no veto. Entered positions remain HOLD."
        sigma_class = "warn"
    elif sigmas < 2.0:
        sigma_meaning = "Approaching the +2σ veto threshold — close to triggering TRIM for entered positions and SKIP for watching."
        sigma_class = "warn"
    else:
        sigma_meaning = "≥ +2σ premium — Layer-3 fair-value veto fires: watching → SKIP, entered → TRIM (take some profit at this extreme)."
        sigma_class = "fail"

    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Fair value — what the stock is fundamentally worth</span><span class='meta'>step 5</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    Multi-method triangulation of fundamental value: forward P/E vs sector peers, discounted-cash-flow
    where input quality is sufficient, and recent comparable transactions. The output is a RANGE (not a
    point estimate) reflecting genuine uncertainty in valuation. The σ figure measures how far today's
    market price sits above (+) or below (−) the range mean, in units of the range's own standard
    deviation: ±1σ means "roughly within the normal valuation range"; ±2σ means "meaningfully outside it."
  </div>
  <div class='kv-row'><span class='k'>Fair-value range (low — mean — high)</span><span class='v'>${low:.2f} — ${mean:.2f} — ${high:.2f}</span><span></span></div>
  <div class='kv-row'><span class='k'>Current market price</span><span class='v'>${current:.2f}</span><span class='v-right'><span class='pill pill-{sigma_class}'>{sigmas:+.2f}σ vs FV mean</span></span></div>
  <div class='kv-row'><span class='k'>Methods contributing</span><span class='v' style='font-family: inherit; font-size: 12px;'>{", ".join(html.escape(m) for m in methods)}</span><span></span></div>
  <div class='what-it-means'>
    <span class='label'>What this means for you</span>
    {html.escape(sigma_meaning)}
  </div>
</div>
"""


def _render_cross_check(snap: dict) -> str:
    cc = snap.get("cross_check")
    if not cc or cc.get("status") == "pending":
        return _pending_panel("Math cross-check — do two independent forecasts agree?", "Will appear once steps 6 (Monte Carlo) and 7 (Fokker-Planck PDE) both ship. Side-by-side comparison of the two methods on P(target), P(stop), and EV at each horizon.")
    horizons = cc.get("horizons", {})

    # Compute headline: how many of the (horizon × metric) pairs agreed?
    total = 0
    agree_count = 0
    max_pp_delta = 0.0
    for h in config.HORIZONS:
        d = horizons.get(h) or horizons.get(str(h))
        if not d:
            continue
        for metric in ("p_target", "p_stop", "ev"):
            total += 1
            if d.get(f"agree_{metric}", False):
                agree_count += 1
            delta = d.get(f"delta_{metric}", 0)
            scale_pp = 100 if metric != "ev" else 100
            max_pp_delta = max(max_pp_delta, abs(delta) * scale_pp)
    all_agree = (agree_count == total) and total > 0

    if all_agree:
        headline_class = "ok"
        headline_text = f"All {total} comparisons agree within tolerance — both methods independently arrive at the same answer. No Layer-2 haircut applied to conviction."
    else:
        headline_class = "warn"
        headline_text = f"{total - agree_count} of {total} comparison(s) outside tolerance (largest gap: {max_pp_delta:.1f}pp). Layer-2 confidence haircut applies to conviction."

    rows = []
    for h in config.HORIZONS:
        d = horizons.get(h) or horizons.get(str(h))
        if not d:
            continue
        for metric in ("p_target", "p_stop", "ev"):
            mc = d.get(f"mc_{metric}")
            pde = d.get(f"pde_{metric}")
            delta = d.get(f"delta_{metric}")
            agree = d.get(f"agree_{metric}", False)
            agree_cls = "agree-ok" if agree else "agree-warn"
            agree_label = "✓ within tol" if agree else "⚠ outside tol"
            display_metric = {"p_target": "P(target)", "p_stop": "P(stop)", "ev": "EV"}[metric]
            unit = "%" if metric != "ev" else "%"
            rows.append(
                f"<tr><td>{h}d</td><td>{display_metric}</td>"
                f"<td class='num'>{mc*100:+.2f}{unit}</td>"
                f"<td class='num'>{pde*100:+.2f}{unit}</td>"
                f"<td class='num'>Δ {abs(delta)*100:.2f}{'pp' if metric != 'ev' else 'pp'}</td>"
                f"<td class='num {agree_cls}'>{agree_label}</td>"
                f"</tr>"
            )

    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Math cross-check — do two independent forecasts agree?</span><span class='meta'>step 7 — MC vs Fokker-Planck PDE</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    We compute the same forecast (P(target hit first), P(stop hit first), expected value) two
    completely different ways: random simulation (50,000 Monte Carlo paths) AND solving a
    deterministic differential equation. The methods make different mathematical assumptions —
    when they agree, the answer is robust; when they diverge, at least one is being stretched
    by unusual conditions, so we don't know which to trust. Conviction gets a Layer-2 haircut
    when they disagree by more than {config.THRESHOLDS.cross_check.p_target_agreement_tolerance_pp:.1f}pp.
  </div>
  <div class='what-it-means' style='background: #{"f0fdf4" if all_agree else "fffbeb"}; border-left-color: var(--{headline_class});'>
    <span class='label' style='color: var(--{headline_class});'>Headline</span>
    {html.escape(headline_text)}
  </div>
  <div class='table-scroll' style='margin-top: 10px;'>
    <table class='agreement-table'>
      <thead><tr><th>Horizon</th><th>Metric</th><th style='text-align:right;'>MC</th><th style='text-align:right;'>PDE</th><th style='text-align:right;'>Δ</th><th style='text-align:right;'>Agreement</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</div>
"""


def _render_classifier(snap: dict) -> str:
    c = snap.get("tier_classifier")
    if not c or c.get("status") != "ok":
        return _pending_panel("Tier sanity-check — does the math match the label?", "Lights up after step 1 (data fetch) completes.")
    props = c.get("properties") or {}
    comparison = c.get("comparison") or {}
    anchor = comparison.get("anchor", "?")
    measured = comparison.get("measured", "?")
    direction = comparison.get("direction", "?")

    if direction == "match":
        direction_pill = "<span class='pill pill-ok'>match — no haircut</span>"
        headline = f"The {anchor}-tier label you set matches the stock's actual recent behavior. No confidence haircut applies."
        means = (
            f"The pipeline ran Tier-{anchor} statistical assumptions (drift bands, volatility-of-volatility widening, fat-tailed priors if C) "
            f"that match how this stock has actually been behaving over the last 90 sessions. The conviction score you see is calibrated to "
            f"this stock's real character."
        )
    elif direction == "stricter":
        direction_pill = "<span class='pill pill-warn'>measured stricter</span>"
        headline = f"Your watchlist label is {anchor}, but the stock has been behaving like a {measured}-tier name (more volatile / thinner / less mature) for the last several sessions."
        means = (
            f"If this mismatch persists for 5+ consecutive nights, Layer-2 confidence gets a 20% haircut because the engine ran the wrong "
            f"priors. Consider re-anchoring the watchlist to Tier {measured} via the conversational interface."
        )
    elif direction == "looser":
        direction_pill = "<span class='pill pill-info'>measured looser</span>"
        headline = f"Your watchlist label is {anchor}, but the stock has been behaving like a calmer {measured}-tier name lately."
        means = (
            f"Conservative direction — engine ran tighter statistical assumptions than the stock's actual behavior would warrant. "
            f"No conviction haircut (we only haircut when measured is stricter). You may be leaving edge on the table if this persists."
        )
    else:
        direction_pill = f"<span class='pill pill-pending'>{html.escape(direction)}</span>"
        headline = "—"
        means = "—"

    rows = []
    for prop_name, display, prop_explain in [
        ("vol_annualized", "90d realized vol",        "Typical annualized price swing"),
        ("vol_of_vol",     "90d vol-of-vol",          "How much the vol itself swings"),
        ("adv_usd",        "20d avg daily $ volume",  "Liquidity — bigger = easier trades"),
        ("history_days",   "Days of clean history",   "More history = more reliable calibration"),
    ]:
        p = props.get(prop_name) or {}
        val = p.get("value")
        tier = p.get("tier", "?")
        if prop_name == "vol_annualized":
            val_display = f"{val*100:.1f}%" if val is not None else "?"
        elif prop_name == "vol_of_vol":
            val_display = f"{val:.3f}" if val is not None else "?"
        elif prop_name == "adv_usd":
            val_display = _fmt_money(val)
        else:
            val_display = str(int(val)) if val is not None else "?"
        rows.append(
            f"<tr><td>{display}<div style='font-size: 11px; color: var(--muted);'>{prop_explain}</div></td>"
            f"<td class='num'>{val_display}</td><td>"
            f"<span class='tier-badge tier-{html.escape(tier)}'>Tier {html.escape(tier)}</span></td></tr>"
        )
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Tier sanity-check — does the math match the label?</span><span class='meta'>§4.2 — advisory in v1.0</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    Each ticker has a tier label you set in the watchlist — <strong>A</strong> mega-cap mature (think
    NVDA-like), <strong>B</strong> mid-cap growth (think AMAT-like), <strong>C</strong> small-cap
    fat-tailed (think IONQ-like). The pipeline uses your label to pick which statistical assumptions
    to run. This panel independently measures the stock's actual recent behavior — vol, vol-of-vol,
    liquidity, history — and tells you whether the label you set still matches reality. If the math
    has migrated tiers (e.g. a B-anchored name now behaving like C), the conviction confidence gets
    a Layer-2 haircut.
  </div>
  <div class='kv-row'><span class='k'>Watchlist anchor → measured</span><span class='v'>Tier {html.escape(anchor)} → Tier {html.escape(measured)}</span><span class='v-right'>{direction_pill}</span></div>
  <div class='table-scroll' style='margin-top: 8px;'>
    <table class='classifier-table'>
      <thead><tr><th>Property</th><th style='text-align:right;'>Value</th><th>Score</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
  <div class='what-it-means'>
    <span class='label'>What this means for you</span>
    {html.escape(headline)} {html.escape(means)}
  </div>
</div>
"""


def _render_trajectory(snap: dict) -> str:
    t = snap.get("trajectory")
    if not t or t.get("status") == "pending":
        return _pending_panel("Conviction trajectory — is the signal durable or flip-flopping?", "Populates after a few nights of accumulated snapshots. The sparkline will show the last 20 nights of conviction scores with the ENTER threshold marked.")
    nightly_scores = t.get("nightly_scores", [])
    annotation = t.get("annotation", "stable")
    trend_class = annotation if annotation in ("rising", "decaying", "stable", "unstable") else "stable"
    svg = _sparkline_svg(nightly_scores)
    summary = t.get("summary", "")

    # Plain-English interpretation of the trajectory annotation
    if annotation == "rising":
        means = (
            "Conviction has been rising — multiple consecutive nights have confirmed the same directional read, "
            "with new data each night pushing the score higher. Durable signal. No Layer-2 haircut applies."
        )
    elif annotation == "stable":
        means = (
            "Conviction has been holding steady — the signal is durable, just not changing. Multiple nights of "
            "the same answer means we can trust today's read. No Layer-2 haircut applies."
        )
    elif annotation == "decaying":
        means = (
            "Conviction has been falling — multiple consecutive nights have weakened the signal. The thesis is "
            "deteriorating. No haircut from the trajectory itself, but the lower current score will already be "
            "reflected in the verdict label."
        )
    elif annotation == "unstable":
        means = (
            "Conviction has been flip-flopping across recent nights — verdict has changed direction 3+ times in "
            "the last 5 sessions. This means we're at a regime boundary and small new data points are flipping the "
            "answer. Layer-2 confidence gets a 10% haircut — today's read is less trustworthy than usual."
        )
    else:
        means = "—"

    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Conviction trajectory — is the signal durable or flip-flopping?</span><span class='meta'>last {len(nightly_scores)} nights</span></div>
  <div class='what-it-is'>
    <span class='label'>What this is</span>
    Each night the engine writes a snapshot containing that night's conviction score for every (ticker × horizon).
    This panel plots the last {len(nightly_scores)} nights' scores as a sparkline. A signal that says ENTER 5 nights
    in a row is more trustworthy than one that flipped ENTER → WAIT → ENTER → SKIP → ENTER over the same 5 nights —
    the latter pattern means we're at a regime boundary and shouldn't trust today's read too much. The dashed
    green line marks the ENTER threshold ({config.THRESHOLDS.conviction.vetoes.enter_score_threshold:.2f}) for reference.
  </div>
  <div class='sparkline-block'>
    {svg}
    <div class='description'>
      <div class='trend {trend_class}' style='font-size: 14px;'>{html.escape(annotation)}</div>
      <div>{html.escape(summary)}</div>
    </div>
  </div>
  <div class='what-it-means'>
    <span class='label'>What this means for you</span>
    {html.escape(means)}
  </div>
</div>
"""


def _render_data(snap: dict) -> str:
    d = snap.get("data") or {}
    status = d.get("status", "pending")
    if status == "pending":
        return _pending_panel("Data &amp; sanity (step 1)", "Will populate from FMP on next cron run.")
    sanity = d.get("sanity") or {}
    bar_count = d.get("bar_count", "?")
    profile = d.get("profile") or {}
    rows = []
    for check in ("freshness", "completeness", "split_div", "volume"):
        c = sanity.get(check) or {}
        cls = f"pill-{c.get('status', 'pending')}"
        rows.append(
            f"<div class='kv-row'>"
            f"<span class='k'>{check}</span>"
            f"<span class='v' style='font-family: inherit; font-size: 12px;'>{html.escape(c.get('message', ''))}</span>"
            f"<span class='v-right'><span class='pill {cls}'>{html.escape(c.get('status', '?'))}</span></span>"
            f"</div>"
        )
    return f"""
<details class='subpanel'>
  <summary style='cursor: pointer; list-style: none;'>
    <div class='head'>
      <span class='title'>Data &amp; sanity (step 1) — {bar_count} bars · {html.escape(profile.get('industry') or '?')}</span>
      <span class='meta'><span class='pill pill-{html.escape(status)}'>{html.escape(status)}</span></span>
    </div>
  </summary>
  <div style='margin-top: 8px;'>{"".join(rows)}</div>
</details>
"""


def _render_daily_path(snap: dict) -> str:
    p = snap.get("daily_path")
    if not p or p.get("status") == "pending":
        return _pending_panel("Daily expected path", "Day-by-day median MC price with rally/dip zone tinting (v17 dip-engine style). Will appear once step 6 ships.")
    rows = []
    days = p.get("days") or []
    for d in days[:20]:  # cap to 20 for the inline view
        zone = d.get("zone", "")
        zone_cls = f"zone-{zone}" if zone in ("dip", "rally") else ""
        zone_label = {"dip": "↓ dip zone", "rally": "↑ rally zone", "": ""}.get(zone, "")
        rows.append(
            f"<tr><td class='num'>{d['day']}</td><td>{html.escape(d['date'])}</td>"
            f"<td class='num'>${d['median_price']:.2f}</td>"
            f"<td class='{zone_cls}'>{zone_label}</td></tr>"
        )
    return f"""
<details class='subpanel'>
  <summary style='cursor: pointer; list-style: none;'>
    <div class='head'>
      <span class='title'>Daily expected path — click to expand</span>
      <span class='meta'>step 6 — median MC scenario</span>
    </div>
  </summary>
  <table class='daily-path' style='margin-top: 8px;'>
    <thead><tr><th class='num'>Day</th><th>Date</th><th style='text-align:right;'>Median price</th><th>Zone</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <div style='font-size: 11.5px; color: var(--muted); margin-top: 8px; line-height: 1.5;'>This is one typical scenario out of {config.MC_PATHS:,} simulated paths. Reality could be shallower, deeper, or different days. The dip/rally zones highlight the ±7-day window around the median's lowest and highest points; the headline conviction-engine targets remain primary.</div>
</details>
"""


# ---------- footer ----------


def _render_footer(payload: dict) -> str:
    rendered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""
<footer class='band'>
  <p><strong>SGC Swing Trader v1.</strong> Built atop the SGC Dip Engine pattern, evolved for swing-trader semantics: dual horizon, three-layer conviction, MC↔PDE cross-check, per-user verdicts, measurement-driven tier classifier.</p>
  <p>Not investment advice. The system is a research tool. Position sizing and execution are manual on Trading 212.</p>
  <p>Rendered at {html.escape(rendered_at)}.</p>
</footer>
"""


# ---------- low-level helpers ----------


def _pending_panel(title: str, description: str) -> str:
    return f"""
<div class='subpanel pending'>
  <div class='head'>
    <span class='title'>{title}</span>
    <span class='meta'><span class='pill pill-pending'>pending</span></span>
  </div>
  <div style='font-size: 12.5px; color: var(--muted); font-style: italic;'>{description}</div>
</div>
"""


def _sparkline_svg(values: list[float], width: int = 360, height: int = 56) -> str:
    """Server-side SVG sparkline. Values normalized to fit; min/max
    markers shown as small dots. No external dependencies."""
    if not values:
        return f"<svg width='{width}' height='{height}'></svg>"
    pad_x = 4
    pad_y = 6
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y

    vmin = min(values)
    vmax = max(values)
    if vmax - vmin < 1e-9:
        vmax = vmin + 1.0

    points = []
    for i, v in enumerate(values):
        x = pad_x + (i / max(1, len(values) - 1)) * plot_w
        y = pad_y + (1 - (v - vmin) / (vmax - vmin)) * plot_h
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)

    # final value marker
    final_x = pad_x + plot_w
    final_y = pad_y + (1 - (values[-1] - vmin) / (vmax - vmin)) * plot_h

    # threshold line for ENTER (0.70)
    enter_threshold = config.THRESHOLDS.conviction.vetoes.enter_score_threshold
    threshold_y = pad_y + (1 - (enter_threshold - vmin) / (vmax - vmin)) * plot_h
    threshold_line = ""
    if vmin <= enter_threshold <= vmax:
        threshold_line = f"<line x1='{pad_x}' y1='{threshold_y:.1f}' x2='{pad_x + plot_w}' y2='{threshold_y:.1f}' stroke='#10b981' stroke-width='1' stroke-dasharray='3,3' opacity='0.6'/>"

    return f"""
<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' xmlns='http://www.w3.org/2000/svg'>
  <rect x='0' y='0' width='{width}' height='{height}' fill='#fafafa' rx='3'/>
  {threshold_line}
  <polyline points='{polyline}' fill='none' stroke='#1d4ed8' stroke-width='1.5'/>
  <circle cx='{final_x:.1f}' cy='{final_y:.1f}' r='3' fill='#1d4ed8'/>
  <text x='{pad_x + 4}' y='{height - 4}' font-size='10' fill='#737373'>{vmin:.2f}</text>
  <text x='{pad_x + 4}' y='{pad_y + 10}' font-size='10' fill='#737373'>{vmax:.2f}</text>
  <text x='{width - pad_x - 4}' y='{pad_y + 10}' font-size='10' fill='#10b981' text-anchor='end' opacity='0.7'>ENTER {enter_threshold:.2f}</text>
</svg>
"""


def _fmt_money(n) -> str:
    if n is None:
        return "?"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= unit:
            return f"${n / unit:.2f}{suffix}"
    return f"${n:.0f}"
