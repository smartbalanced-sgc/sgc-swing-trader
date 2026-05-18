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


# ---------- top-level render ----------


def render(payload: dict) -> str:
    """Build the full HTML document from a run payload."""
    parts: list[str] = []

    parts.append(_render_header(payload))
    parts.append(_render_system_status(payload))
    parts.append(_render_data_quality(payload))
    parts.append(_render_backtest(payload))
    parts.append(_render_deployment(payload))

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
    tickers = payload.get("tickers", {})
    by_user_label: dict[tuple, list[str]] = {}
    for ticker, snap in tickers.items():
        verdicts = snap.get("verdict") or {}
        for user, v in verdicts.items():
            if not isinstance(v, dict) or v.get("status") == "pending":
                continue
            horizon_label = v.get("primary_label") or "—"
            by_user_label.setdefault((user, horizon_label), []).append(ticker)

    rows: list[str] = []
    for user in config.USERS:
        for label in ("ENTER", "HOLD", "WAIT", "TRIM", "SKIP", "EXIT"):
            tickers_for = by_user_label.get((user, label), [])
            if not tickers_for:
                continue
            rows.append(
                f"<div class='row'>"
                f"<span class='label'><span class='verdict verdict-{html.escape(label)}'>{html.escape(label)}</span></span>"
                f"<span class='count'>{user.title()} ({len(tickers_for)}):</span>"
                f"<span class='ticker-list'>{', '.join(tickers_for)}</span>"
                f"</div>"
            )
    if not rows:
        rows.append("<div class='row'><span class='label'>—</span><span class='count'>No actionable verdicts this run.</span></div>")

    return f"""
<div class='deployment'>
  <h3>Today's deployment ({payload.get('run_date', '?')})</h3>
  {"".join(rows)}
</div>
"""


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
    <span class='symbol'>{html.escape(ticker)}</span>
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
    users = per_horizon.get("users", {})

    # We display the breakdown once per user (since user_state can affect
    # which vetoes fire). For the common case where both users are in
    # the same state, the breakdowns will be identical — we don't merge
    # them in v1 (keeps rendering simple). v1.x could optimize.
    user_columns = []
    for user in config.USERS:
        if user not in users:
            continue
        u = users[user]
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

    lines = []
    if next_event:
        lines.append(f"<div class='catalyst-line'><span class='icon'>⚡</span><span class='label'>Next event</span><span class='value'>{html.escape(next_event['type'])} on {html.escape(next_event['date'])} ({distance} session{'s' if distance != 1 else ''} away)</span></div>")
    if implied is not None:
        lines.append(f"<div class='catalyst-line'><span class='label'>Options-implied move</span><span class='value'>±{implied*100:.1f}% (straddle)</span></div>")
    if analyst:
        trend_str = analyst.get('trend', '')
        lines.append(f"<div class='catalyst-line'><span class='label'>Analyst PT revisions (14d)</span><span class='value'>{analyst.get('count', '?')} upgrades, avg PT ${analyst.get('avg_pt', '?')} ({trend_str})</span></div>")

    reactions_html = ""
    if reactions:
        rows = "".join(
            f"<tr><td>{html.escape(r['date'])}</td><td>{html.escape(r.get('type', 'earnings'))}</td><td class='num'>{r['reaction_pct']:+.1f}%</td></tr>"
            for r in reactions
        )
        avg = sum(r["reaction_pct"] for r in reactions) / len(reactions)
        reactions_html = f"""
<div style='margin-top: 8px; font-size: 12px; color: var(--text-soft);'>Last {len(reactions)} earnings reactions (avg {avg:+.1f}%):</div>
<table class='reactions-table'>
  <thead><tr><th>Date</th><th>Type</th><th style='text-align:right;'>Reaction</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""

    news_html = ""
    if news:
        news_html = f"<div style='margin-top: 8px; font-size: 12px; color: var(--text-soft);'>Recent news ({len(news)} headlines):</div><ul class='news-bullets'>{''.join(f'<li>{html.escape(n)}</li>' for n in news)}</ul>"

    rec_html = f"<div class='engine-rec'><strong>Engine read:</strong> {html.escape(rec)}</div>" if rec else ""

    return f"""
<div class='subpanel'>
  <div class='head'>
    <span class='title'>Catalyst narrative</span>
    <span class='meta'>step 3 — catalyst detection</span>
  </div>
  {"".join(lines)}
  {reactions_html}
  {news_html}
  {rec_html}
</div>
"""


def _render_regime(snap: dict) -> str:
    r = snap.get("regime")
    if not r or r.get("status") == "pending":
        return _pending_panel("Regime detector", "HMM-derived regime state with confidence + duration. Feeds MC drift (Layer 1) and Layer-3 ENTER vetoes. Will appear once step 2 ships.")
    state = r.get("state", "?")
    conf = r.get("confidence", 0.0)
    duration = r.get("days_in_regime", 0)
    drift = r.get("annualized_drift_implied", 0.0)
    narrative = r.get("narrative", "")
    veto_active = r.get("veto_active", False)
    veto_class = "veto-fired" if veto_active else ""
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Regime</span><span class='meta'>step 2 — HMM</span></div>
  <div class='kv-row {veto_class}'>
    <span class='k'>Current state</span>
    <span class='v'>{html.escape(state)}</span>
    <span class='v-right'>{conf*100:.0f}% confidence{' — ENTER veto active' if veto_active else ''}</span>
  </div>
  <div class='kv-row'><span class='k'>Days in this regime</span><span class='v'>{duration}</span><span></span></div>
  <div class='kv-row'><span class='k'>Implied annualized drift (fed to MC)</span><span class='v'>{drift*100:+.1f}%/year</span><span></span></div>
  {f"<div style='font-size: 12.5px; color: var(--text-soft); margin-top: 8px; line-height: 1.55;'>{html.escape(narrative)}</div>" if narrative else ""}
</div>
"""


def _render_volatility(snap: dict) -> str:
    v = snap.get("volatility")
    if not v or v.get("status") == "pending":
        return _pending_panel("Volatility forecast (GARCH)", "GARCH(1,1) forward vol path. Tier B and C tickers get widened confidence bands. Will appear once step 4 ships.")
    current = v.get("current_realized_pct", 0.0)
    forecast_30 = v.get("forecast_30d_pct", 0.0)
    forecast_60 = v.get("forecast_60d_pct", 0.0)
    band_width = v.get("confidence_band", "tight")
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Volatility forecast (GARCH)</span><span class='meta'>step 4</span></div>
  <div class='kv-row'><span class='k'>Current realized vol (60d trailing)</span><span class='v'>{current*100:.1f}% annualized</span><span></span></div>
  <div class='kv-row'><span class='k'>Forecast end-of-30d</span><span class='v'>{forecast_30*100:.1f}%</span><span></span></div>
  <div class='kv-row'><span class='k'>Forecast end-of-60d</span><span class='v'>{forecast_60*100:.1f}%</span><span></span></div>
  <div class='kv-row'><span class='k'>Confidence band</span><span class='v'>{html.escape(band_width)} (set by tier)</span><span></span></div>
</div>
"""


def _render_fair_value(snap: dict) -> str:
    fv = snap.get("fair_value")
    if not fv or fv.get("status") == "pending":
        return _pending_panel("Fair value", "Multi-method triangulation (multiples + DCF + comparable transactions). Will appear once step 5 ships.")
    low = fv.get("range_low", 0.0)
    mean = fv.get("range_mean", 0.0)
    high = fv.get("range_high", 0.0)
    current = fv.get("current_price", 0.0)
    sigmas = fv.get("premium_sigmas", 0.0)
    methods = fv.get("methods", [])
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Fair value</span><span class='meta'>step 5</span></div>
  <div class='kv-row'><span class='k'>Fair-value range</span><span class='v'>${low:.2f} — ${mean:.2f} — ${high:.2f}</span><span></span></div>
  <div class='kv-row'><span class='k'>Current price vs FV mean</span><span class='v'>${current:.2f}</span><span class='v-right'>{sigmas:+.2f}σ</span></div>
  <div class='kv-row'><span class='k'>Methods contributing</span><span class='v'>{", ".join(html.escape(m) for m in methods)}</span><span></span></div>
</div>
"""


def _render_cross_check(snap: dict) -> str:
    cc = snap.get("cross_check")
    if not cc or cc.get("status") == "pending":
        return _pending_panel("MC ↔ PDE agreement", "Side-by-side per horizon. Will appear once steps 6 and 7 both ship.")
    horizons = cc.get("horizons", {})
    rows = []
    for h in config.HORIZONS:
        d = horizons.get(h) or horizons.get(str(h))
        if not d:
            continue
        for metric in ("p_target", "p_stop", "ev"):
            mc = d.get(f"mc_{metric}")
            pde = d.get(f"pde_{metric}")
            delta = d.get(f"delta_{metric}")
            tol = d.get(f"tolerance_{metric}")
            agree = d.get(f"agree_{metric}", False)
            agree_cls = "agree-ok" if agree else "agree-warn"
            agree_label = "within tolerance" if agree else "outside tolerance"
            display_metric = {"p_target": "P(target)", "p_stop": "P(stop)", "ev": "EV"}[metric]
            unit = "" if metric == "ev" else "%"
            scale = 100 if metric != "ev" else 100
            rows.append(
                f"<tr><td>{h}d</td><td>{display_metric}</td>"
                f"<td class='num'>{mc*scale:+.2f}{unit if metric == 'ev' else ('%' if scale==100 else '')}</td>"
                f"<td class='num'>{pde*scale:+.2f}{unit if metric == 'ev' else ('%' if scale==100 else '')}</td>"
                f"<td class='num'>Δ {abs(delta)*scale:.2f}{unit if metric == 'ev' else 'pp'}</td>"
                f"<td class='num {agree_cls}'>{agree_label}</td>"
                f"</tr>"
            )
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>MC ↔ PDE agreement</span><span class='meta'>step 7 cross-check</span></div>
  <table class='agreement-table'>
    <thead><tr><th>Horizon</th><th>Metric</th><th style='text-align:right;'>MC</th><th style='text-align:right;'>PDE</th><th style='text-align:right;'>Δ</th><th style='text-align:right;'>Agreement</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
"""


def _render_classifier(snap: dict) -> str:
    c = snap.get("tier_classifier")
    if not c or c.get("status") != "ok":
        return _pending_panel("§4.2 measured-tier check", "Watchlist anchor vs measurement-driven classifier across 4 behavioral properties. Lights up after step 1 (data fetch) completes.")
    props = c.get("properties") or {}
    comparison = c.get("comparison") or {}
    anchor = comparison.get("anchor", "?")
    measured = comparison.get("measured", "?")
    direction = comparison.get("direction", "?")
    direction_pill = {
        "match":    "<span class='pill pill-ok'>match</span>",
        "stricter": "<span class='pill pill-warn'>measured stricter (B-tier behavior on A-tier anchor, etc.)</span>",
        "looser":   "<span class='pill pill-info'>measured looser</span>",
    }.get(direction, f"<span class='pill pill-pending'>{html.escape(direction)}</span>")
    rows = []
    for prop_name, display in [
        ("vol_annualized", "90d realized vol (annualized)"),
        ("vol_of_vol", "90d vol-of-vol"),
        ("adv_usd", "20d avg daily $ volume"),
        ("history_days", "Days of clean history"),
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
            f"<tr><td>{display}</td><td class='num'>{val_display}</td><td>"
            f"<span class='tier-badge tier-{html.escape(tier)}'>Tier {html.escape(tier)}</span></td></tr>"
        )
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>§4.2 measured-tier check</span><span class='meta'>advisory in v1.0</span></div>
  <div class='kv-row'><span class='k'>Watchlist anchor → measured</span><span class='v'>{html.escape(anchor)} → {html.escape(measured)}</span><span class='v-right'>{direction_pill}</span></div>
  <table class='classifier-table' style='margin-top: 8px;'>
    <thead><tr><th>Property</th><th style='text-align:right;'>Value</th><th>Score</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
"""


def _render_trajectory(snap: dict) -> str:
    t = snap.get("trajectory")
    if not t or t.get("status") == "pending":
        return _pending_panel("Conviction trajectory", "Sparkline of last N nights' conviction score + rising/stable/decaying/flip-flopping annotation. Populates after a few nights of accumulated snapshots.")
    nightly_scores = t.get("nightly_scores", [])
    annotation = t.get("annotation", "stable")
    trend_class = annotation if annotation in ("rising", "decaying", "stable", "unstable") else "stable"
    svg = _sparkline_svg(nightly_scores)
    summary = t.get("summary", "")
    return f"""
<div class='subpanel'>
  <div class='head'><span class='title'>Conviction trajectory</span><span class='meta'>last {len(nightly_scores)} nights</span></div>
  <div class='sparkline-block'>
    {svg}
    <div class='description'>
      <div class='trend {trend_class}'>{html.escape(annotation)}</div>
      <div>{html.escape(summary)}</div>
    </div>
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
