"""Dashboard HTML generation.

See docs/V1_SPEC.md §6. Renders a single static HTML file at
docs/index.html. No client-side JS dependencies; inline CSS only; charts
will be server-side SVG when chart-producing pipeline steps come online.

This is the walking-skeleton renderer: it draws the §6.2 summary table
including the watchlist-vs-measured-tier comparison from the §4.2
classifier, plus per-ticker deep sections that render whatever's
available and show 'pending — not yet implemented' for stubbed steps.
As each pipeline step comes online its panel becomes live without
needing dashboard changes (we render whatever shape the snapshot has).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from src import config

_STYLES = """
:root {
  --bg: #fafaf9;
  --panel: #fff;
  --border: #e5e5e5;
  --text: #111;
  --muted: #6b6b6b;
  --ok: #15803d;
  --warn: #b45309;
  --fail: #b91c1c;
  --pending: #6b7280;
  --link: #1d4ed8;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 0 16px 64px;
  line-height: 1.45;
}
.wrap { max-width: 1100px; margin: 0 auto; }
header.band {
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  margin: 0 -16px 24px;
  padding: 16px;
}
header.band .row {
  display: flex; flex-wrap: wrap; gap: 16px 32px;
  font-size: 13px; color: var(--muted);
}
header.band h1 {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 600;
}
h2 { font-size: 16px; margin: 32px 0 12px; }
h3 { font-size: 14px; margin: 24px 0 8px; color: var(--muted); }
table.summary {
  width: 100%;
  border-collapse: collapse;
  background: var(--panel);
  border: 1px solid var(--border);
  font-size: 13px;
}
table.summary th, table.summary td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
table.summary th { background: #f4f4f5; font-weight: 600; }
.ticker { font-weight: 600; font-family: ui-monospace, SFMono-Regular, monospace; }
.tier-A { color: #15803d; font-weight: 600; }
.tier-B { color: #b45309; font-weight: 600; }
.tier-C { color: #b91c1c; font-weight: 600; }
.agree-yes { color: var(--ok); }
.agree-no { color: var(--warn); }
.agree-no::before { content: "⚠ "; }
.status-ok      { color: var(--ok); }
.status-warn    { color: var(--warn); }
.status-fail    { color: var(--fail); font-weight: 600; }
.status-pending { color: var(--pending); font-style: italic; }
section.ticker-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 16px 20px;
  margin: 16px 0;
}
section.ticker-card .head {
  display: flex; justify-content: space-between; align-items: baseline;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 12px;
}
section.ticker-card .head .ticker { font-size: 20px; }
section.ticker-card .head .notes { color: var(--muted); font-size: 13px; }
.panel {
  margin: 12px 0;
  padding: 10px 12px;
  background: #fcfcfc;
  border: 1px solid var(--border);
  border-radius: 3px;
  font-size: 13px;
}
.panel .panel-title {
  font-weight: 600;
  margin-bottom: 4px;
  display: flex; justify-content: space-between;
}
.panel.pending {
  background: #f9fafb;
  color: var(--pending);
  font-style: italic;
}
.kv { display: grid; grid-template-columns: 200px 1fr; gap: 4px 16px; }
.kv .k { color: var(--muted); }
.kv .v { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; }
footer.band {
  margin-top: 48px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--muted);
}
.errors {
  background: #fef2f2;
  border: 1px solid #fecaca;
  padding: 12px;
  border-radius: 4px;
  margin: 16px 0;
  font-size: 13px;
}
@media (max-width: 600px) {
  table.summary { font-size: 12px; }
  table.summary th, table.summary td { padding: 6px 6px; }
  .kv { grid-template-columns: 130px 1fr; }
}
"""


def render(payload: dict) -> str:
    """Build the full HTML document from a run payload (as produced by
    src.main.run())."""
    run_date = payload.get("run_date", "?")
    started = payload.get("run_started_at", "?")
    finished = payload.get("run_finished_at", "?")
    watchlist = payload.get("watchlist", {})
    tickers = payload.get("tickers", {})
    errors = payload.get("errors", [])

    parts: list[str] = []
    parts.append(_render_header(run_date, started, finished, tickers, errors))

    if errors:
        parts.append(_render_errors_panel(errors))

    parts.append(_render_summary_table(watchlist, tickers))

    parts.append("<h2>Per-ticker deep reports</h2>")
    for ticker in watchlist.keys():
        snap = tickers.get(ticker)
        if snap is None:
            continue
        parts.append(_render_ticker_card(ticker, snap, watchlist[ticker]))

    parts.append(_render_footer(run_date))

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SGC Swing Trader — {html.escape(run_date)}</title>
<style>{_STYLES}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


# ---------- sections ----------


def _render_header(run_date: str, started: str, finished: str, tickers: dict, errors: list) -> str:
    n_ok = sum(1 for s in tickers.values() if s.get("data", {}).get("status") == "ok")
    n_total = len(tickers)
    return f"""
<header class="band">
  <div class="wrap">
    <h1>SGC Swing Trader — nightly report</h1>
    <div class="row">
      <span>Run date: <strong>{html.escape(run_date)}</strong></span>
      <span>Started: {html.escape(started)}</span>
      <span>Finished: {html.escape(finished)}</span>
      <span>Fetched cleanly: {n_ok}/{n_total}</span>
      {"<span class='status-fail'>" + str(len(errors)) + " error(s)</span>" if errors else ""}
    </div>
    <div class="row" style="margin-top:6px; font-size:12px;">
      <em>Walking-skeleton v1 — most pipeline steps still pending. As each comes online its panel below goes live.</em>
    </div>
  </div>
</header>
"""


def _render_errors_panel(errors: list) -> str:
    items = "".join(
        f"<li><strong>{html.escape(e.get('ticker', '?'))}</strong> "
        f"(stage: {html.escape(e.get('stage', '?'))}) — "
        f"{html.escape(e.get('message', '?'))}</li>"
        for e in errors
    )
    return f"<div class='errors'><strong>Run-level errors:</strong><ul>{items}</ul></div>"


def _render_summary_table(watchlist: dict, tickers: dict) -> str:
    rows = []
    for ticker, entry in watchlist.items():
        snap = tickers.get(ticker, {})
        anchor = entry.get("tier", "?")
        classifier = snap.get("tier_classifier") or {}
        measured = classifier.get("measured_tier") or "—"
        comparison = classifier.get("comparison") or {}
        agree = comparison.get("agreement")
        direction = comparison.get("direction", "")
        if agree is True:
            agree_cell = f"<span class='agree-yes'>match</span>"
        elif agree is False and direction in ("stricter", "looser"):
            agree_cell = f"<span class='agree-no'>{html.escape(direction)}</span>"
        else:
            agree_cell = "<span class='status-pending'>—</span>"

        holders = entry.get("holders") or {}
        verdict_block = snap.get("verdict") or {}
        user_cells = []
        for user in config.USERS:
            user_state = holders.get(user)
            if user_state is None:
                user_cells.append("<td>—</td>")
                continue
            state_label = user_state.get("state", "?")
            user_verdict = verdict_block.get(user) or {}
            v_status = user_verdict.get("status", "pending")
            user_cells.append(
                f"<td><div>{html.escape(state_label)}</div>"
                f"<div class='status-{html.escape(v_status)}'>{html.escape(v_status)}</div></td>"
            )

        data_status = (snap.get("data") or {}).get("status", "pending")
        flags = []
        if data_status == "fail":
            flags.append("<span class='status-fail'>data sanity FAIL</span>")
        if anchor == "C":
            flags.append("<span class='status-warn'>low-liquidity tier</span>")
        flags_cell = " ".join(flags) if flags else ""

        rows.append(
            f"<tr>"
            f"<td class='ticker'>{html.escape(ticker)}</td>"
            f"<td class='tier-{html.escape(anchor)}'>{html.escape(anchor)}</td>"
            f"<td class='tier-{html.escape(measured) if measured in ('A','B','C') else ''}'>{html.escape(measured)}</td>"
            f"<td>{agree_cell}</td>"
            f"{user_cells[0]}{user_cells[1]}"
            f"<td class='status-{html.escape(data_status)}'>{html.escape(data_status)}</td>"
            f"<td>{flags_cell}</td>"
            f"</tr>"
        )

    return f"""
<h2>Summary</h2>
<table class="summary">
  <thead>
    <tr>
      <th>Ticker</th>
      <th>Watchlist tier</th>
      <th>Measured tier</th>
      <th>§4.2 agreement</th>
      <th>Aidy</th>
      <th>Jesse</th>
      <th>Data</th>
      <th>Flags</th>
    </tr>
  </thead>
  <tbody>
{"".join(rows)}
  </tbody>
</table>
"""


def _render_ticker_card(ticker: str, snap: dict, watchlist_entry: dict) -> str:
    notes = watchlist_entry.get("notes") or ""
    as_of = snap.get("as_of", "?")
    sections = [
        _render_data_panel(snap),
        _render_classifier_panel(snap),
        _render_pending_or_panel("Regime", snap.get("regime")),
        _render_pending_or_panel("Catalyst", snap.get("catalyst")),
        _render_pending_or_panel("Volatility forecast (GARCH)", snap.get("volatility")),
        _render_pending_or_panel("Fair value", snap.get("fair_value")),
        _render_pending_or_panel("Monte Carlo (50,000 paths)", snap.get("monte_carlo")),
        _render_pending_or_panel("Analytic verifier (Fokker-Planck PDE)", snap.get("analytic_verifier")),
        _render_verdict_panel(snap, watchlist_entry),
    ]
    return f"""
<section class="ticker-card" id="ticker-{html.escape(ticker)}">
  <div class="head">
    <div>
      <span class="ticker">{html.escape(ticker)}</span>
      <span class="tier-{html.escape(snap.get('tier_anchor') or '')}" style="margin-left: 8px;">
        Tier {html.escape(snap.get('tier_anchor') or '?')}
      </span>
    </div>
    <div class="notes">as of {html.escape(as_of)} — {html.escape(notes)}</div>
  </div>
{"".join(sections)}
</section>
"""


def _render_data_panel(snap: dict) -> str:
    d = snap.get("data") or {}
    status = d.get("status", "pending")
    sanity = d.get("sanity") or {}
    profile = d.get("profile") or {}
    bar_count = d.get("bar_count", "?")

    kvs = [
        ("Bars on disk", str(bar_count)),
        ("As-of", snap.get("as_of", "?")),
        ("Sector", profile.get("sector") or "?"),
        ("Industry", profile.get("industry") or "?"),
        ("Market cap", _fmt_money(profile.get("market_cap"))),
        ("Sanity overall", str(sanity.get("overall", "?"))),
    ]
    for check in ("freshness", "completeness", "split_div", "volume"):
        c = sanity.get(check) or {}
        kvs.append((f"  {check}", f"{c.get('status', '?')} — {c.get('message', '')}"))

    return _panel("Data fetch & sanity (step 1)", status, _kv_block(kvs))


def _render_classifier_panel(snap: dict) -> str:
    c = snap.get("tier_classifier") or {}
    status = c.get("status", "pending")
    if status != "ok":
        return _panel("Measurement-driven tier (§4.2)", status, "")
    props = c.get("properties") or {}
    comparison = c.get("comparison") or {}
    kvs = [
        ("Measured tier", c.get("measured_tier", "?")),
        ("Decisive property", c.get("decisive_property", "?")),
        ("vs watchlist anchor", f"{comparison.get('anchor', '?')} → {comparison.get('measured', '?')} ({comparison.get('direction', '?')})"),
        ("90d realized vol (annualized)", f"{_pct(props.get('vol_annualized', {}).get('value'))}  → tier {props.get('vol_annualized', {}).get('tier', '?')}"),
        ("90d vol-of-vol", f"{_num(props.get('vol_of_vol', {}).get('value'), 4)}  → tier {props.get('vol_of_vol', {}).get('tier', '?')}"),
        ("20d avg daily $ volume", f"{_fmt_money(props.get('adv_usd', {}).get('value'))}  → tier {props.get('adv_usd', {}).get('tier', '?')}"),
        ("Days of clean history", f"{int(props.get('history_days', {}).get('value', 0))}  → tier {props.get('history_days', {}).get('tier', '?')}"),
    ]
    notes = c.get("notes") or []
    if notes:
        kvs.append(("Notes", "; ".join(notes)))
    return _panel("Measurement-driven tier (§4.2)", status, _kv_block(kvs))


def _render_pending_or_panel(title: str, block: dict | None) -> str:
    block = block or {"status": "pending", "reason": "no output yet"}
    status = block.get("status", "pending")
    if status == "pending":
        return _panel(title, status, f"<em>pending — {html.escape(str(block.get('reason', '')))}</em>")
    if status == "fail":
        return _panel(title, status, f"<em>failed — {html.escape(str(block.get('reason', '')))}</em>")
    # status == "ok" with real content — render the dict generically until
    # specialized renderers are added per step.
    kvs = [(k, _stringify(v)) for k, v in block.items() if k not in ("status", "traceback")]
    return _panel(title, status, _kv_block(kvs))


def _render_verdict_panel(snap: dict, watchlist_entry: dict) -> str:
    holders = watchlist_entry.get("holders") or {}
    verdict_block = snap.get("verdict") or {}
    if not holders:
        return _panel("Verdicts", "pending", "<em>no holders tracking this ticker</em>")

    rows = []
    for user in config.USERS:
        if user not in holders:
            continue
        user_state = holders[user]
        v = verdict_block.get(user) or {"status": "pending", "reason": "verdict step 8 not yet implemented"}
        rows.append(
            f"<div style='margin-top:8px; padding-top:8px; border-top:1px solid var(--border);'>"
            f"<strong>{html.escape(user.title())}</strong> — state: {html.escape(user_state.get('state', '?'))}"
            f" — <span class='status-{html.escape(v.get('status', 'pending'))}'>{html.escape(v.get('status', 'pending'))}</span>"
            f"{' — ' + html.escape(str(v.get('reason', ''))) if v.get('status') != 'ok' else ''}"
            f"</div>"
        )
    return _panel("Dual verdict (step 8)", "pending", "".join(rows))


def _render_footer(run_date: str) -> str:
    rendered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""
<footer class="band">
  <p>SGC Swing Trader v1 walking skeleton. Rendered at {html.escape(rendered_at)}.</p>
  <p>This is not investment advice. The system is a research tool. Sizing and execution are manual on Trading 212.</p>
</footer>
"""


# ---------- small helpers ----------


def _panel(title: str, status: str, body_html: str) -> str:
    pending_class = " pending" if status == "pending" else ""
    return f"""
<div class="panel{pending_class}">
  <div class="panel-title">
    <span>{html.escape(title)}</span>
    <span class="status-{html.escape(status)}">{html.escape(status)}</span>
  </div>
  {body_html}
</div>
"""


def _kv_block(kvs: list[tuple[str, str]]) -> str:
    parts = []
    for k, v in kvs:
        parts.append(f"<div class='k'>{html.escape(str(k))}</div>")
        parts.append(f"<div class='v'>{html.escape(str(v))}</div>")
    return f"<div class='kv'>{''.join(parts)}</div>"


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


def _pct(n) -> str:
    if n is None:
        return "?"
    try:
        return f"{float(n) * 100:.1f}%"
    except (TypeError, ValueError):
        return "?"


def _num(n, places: int = 2) -> str:
    if n is None:
        return "?"
    try:
        return f"{float(n):.{places}f}"
    except (TypeError, ValueError):
        return "?"


def _stringify(v) -> str:
    if isinstance(v, (dict, list)):
        return str(v)[:200]
    return str(v)
