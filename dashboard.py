"""
Admin analytics dashboard — /dashboard and /dashboard/download.

Extracted from app.py into its own module purely for maintainability (a
smaller, self-contained file is easier to find things in and review changes
to) — no logic, routes, templates, or behavior changed in the move. Wired
into the main app as a Flask Blueprint; see app.py's
`app.register_blueprint(dashboard_bp)`.

See wiki/feature-updates/phase-1/plan-dashboard-implementation.md for the
full design/rationale.
"""

import io
import math
import html
from datetime import date, timedelta

from flask import Blueprint, request, session, render_template_string, jsonify, send_file
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference

import db

dashboard_bp = Blueprint("dashboard", __name__)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

# /dashboard access — hardcoded fixed admin allowlist rather than a new
# role/permission column (see wiki/feature-updates/phase-1/
# plan-dashboard-implementation.md, Section 5 Q1's answer). Any uid not in
# this set gets no access at all, not a scoped/empty dashboard.
DASHBOARD_ADMIN_UIDS = {100, 1580, 1474,1387}


def _is_dashboard_admin(uid) -> bool:
    try:
        return int(uid) in DASHBOARD_ADMIN_UIDS
    except (TypeError, ValueError):
        return False


def _dashboard_authorize():
    """Decrypt an optional ?uid=... into the session (same pattern as
    app.index()), then require it to be on the admin allowlist. Returns
    None if unauthorized (no uid, or a uid not on the admin allowlist —
    both mean no access, not a scoped/empty dashboard, per
    wiki/feature-updates/phase-1/plan-dashboard-implementation.md Section 5
    Q1/Q6's answers); otherwise returns the authorized uid. Callers check
    `is None` to decide whether to short-circuit with a denied response."""
    uid_enc = request.args.get("uid")
    if uid_enc:
        try:
            # Deferred import — avoids a circular import at module-load time
            # (app.py imports dashboard_bp from this module; _decrypt_uid_param
            # lives in app.py). Safe here because this only runs inside a
            # request, long after app.py has fully finished importing.
            from app import _decrypt_uid_param

            session["uid"] = int(_decrypt_uid_param(uid_enc))
            session.permanent = True
        except Exception as e:
            print(f"[WARN] Failed to decrypt uid '{uid_enc}': {e}")
    if not _is_dashboard_admin(session.get("uid")):
        return None
    return session.get("uid")


# ---------------------------------------------------------------------------
# Chart helpers — hand-rolled inline SVG, no charting library (Section 2.4 /
# Open Question #3's answer)
# ---------------------------------------------------------------------------

DASHBOARD_CHART_COLORS = [
    "#4f46e5", "#2563eb", "#0891b2", "#15803d", "#b45309",
    "#b91c1c", "#7c3aed", "#c026d3", "#0d9488", "#65a30d",
    "#ca8a04", "#475569",
]


def _dashboard_default_range():
    """Default date-range window for /dashboard when no start/end is given —
    last 30 days (Section 5 Q4's answer)."""
    end = date.today()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


def _fmt_hms(seconds):
    """Seconds -> 'HH:MM:SS' for the review-time figures; '—' when unknown."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _dashboard_bar_chart_svg(rows, label_key, value_key, width=640, height=220, color="#4f46e5"):
    """Minimal hand-rolled vertical bar chart — no charting library (Section
    2.4 / Open Question #3's answer). `rows` is a list of dicts, drawn left
    to right in the given order."""
    if not rows:
        return '<div class="chart-empty">No data for this range.</div>'
    max_val = max((r[value_key] for r in rows), default=0) or 1
    pad_l, pad_r, pad_t, pad_b = 8, 8, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(rows)
    slot = plot_w / n
    bar_w = max(4.0, min(slot * 0.6, 40.0))
    # Thin labels out instead of hiding them all once there are too many to
    # fit legibly — previously any range with >20 bars showed no x-axis
    # labels at all (a blank strip under the chart), which is what this
    # replaces. Always shows at least the first and last bar's label.
    label_step = max(1, math.ceil(n / 15))
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg" role="img">']
    for i, row in enumerate(rows):
        val = row[value_key]
        bar_h = (val / max_val) * plot_h if max_val else 0
        x = pad_l + i * slot + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        label = html.escape(str(row[label_key]))
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="3" fill="{color}"><title>{label}: {val}</title></rect>'
        )
        if i % label_step == 0 or i == n - 1:
            short = html.escape(str(row[label_key])[-5:])
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" font-size="9" '
                f'text-anchor="middle" class="chart-axis-label">{short}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _dashboard_hbar_chart_svg(rows, label_key, value_key, width=460, colors=None):
    """Horizontal bar chart — used for the PII-category breakdown, since
    category names vary in length and read better left-to-right than
    rotated under a vertical bar. `colors` may be a single color string or
    a list cycled per row."""
    if not rows:
        return '<div class="chart-empty">No data for this range.</div>'
    colors = colors or "#4f46e5"
    row_h = 26
    height = len(rows) * row_h + 12
    max_val = max((r[value_key] for r in rows), default=0) or 1
    label_w = 108
    plot_w = width - label_w - 46
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart-svg" role="img">']
    for i, row in enumerate(rows):
        val = row[value_key]
        bar_w = (val / max_val) * plot_w if max_val else 0
        y = 6 + i * row_h
        color = colors[i % len(colors)] if isinstance(colors, (list, tuple)) else colors
        label = html.escape(str(row[label_key])[:14])
        parts.append(
            f'<text x="0" y="{y + row_h * 0.6:.1f}" font-size="10.5" '
            f'class="chart-axis-label">{label}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y + 3}" width="{bar_w:.1f}" height="{row_h - 10}" '
            f'rx="3" fill="{color}"><title>{label}: {val}</title></rect>'
        )
        parts.append(
            f'<text x="{label_w + bar_w + 6:.1f}" y="{y + row_h * 0.6:.1f}" font-size="10" '
            f'class="chart-value-label">{val}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _dashboard_donut_chart_svg(segments, size=150, stroke=26):
    """`segments` is a list of (label, value, color) tuples. Hand-rolled
    donut chart using the stroke-dasharray technique — no charting library."""
    total = sum(v for _, v, _ in segments)
    r = (size - stroke) / 2
    cx = cy = size / 2
    circumference = 2 * math.pi * r
    parts = [
        f'<svg viewBox="0 0 {size} {size}" class="chart-svg donut" role="img">',
        f'<g transform="rotate(-90 {cx} {cy})">',
    ]
    if total <= 0:
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="var(--border)" stroke-width="{stroke}"/>'
        )
    else:
        offset = 0.0
        for label, value, color in segments:
            if value <= 0:
                continue
            frac = value / total
            dash = frac * circumference
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
                f'stroke-width="{stroke}" stroke-dasharray="{dash:.2f} {circumference:.2f}" '
                f'stroke-dashoffset="{-offset:.2f}"><title>{html.escape(label)}: {value} '
                f"({frac * 100:.1f}%)</title></circle>"
            )
            offset += dash
    parts.append("</g></svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — PII Redaction Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f3f5fb;
  --surface: #ffffff;
  --surface2: #f1f3f9;
  --border: rgba(23,25,35,0.08);
  --text: #171923;
  --text-muted: #6b7280;
  --accent: #4f46e5;
  --accent2: #2563eb;
  --accent-glow: rgba(79,70,229,0.10);
  --green: #15803d;
  --green-bg: rgba(21,128,61,0.09);
  --red: #b91c1c;
  --red-bg: rgba(185,28,28,0.08);
  --amber: #92400e;
  --amber-bg: rgba(217,119,6,0.10);
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(23,25,35,.04), 0 12px 28px -16px rgba(79,70,229,.16);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse 60% 40% at 12% -10%, rgba(79,70,229,0.10) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 100% 0%, rgba(37,99,235,0.08) 0%, transparent 55%);
  background-attachment: fixed;
}
header {
  display: flex; align-items: center; gap: 14px;
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  background: rgb(191 216 255 / 80%);
  backdrop-filter: blur(12px);
  position: sticky; top: 0; z-index: 10;
}
.logo {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #4f46e5, #2563eb);
  box-shadow: 0 4px 14px -4px rgba(79,70,229,.5);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.logo svg { width: 20px; height: 20px; color: #fff; }
.header-text h1 { font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
.header-text p  { font-size: 0.72rem; color: var(--text-muted); margin-top: 1px; }
.back-link {
  font-size: .78rem; font-weight: 500; color: var(--accent);
  text-decoration: none; display: flex; align-items: center; gap: 6px;
}
.back-link:hover { text-decoration: underline; }
.back-link svg { width: 13px; height: 13px; }

main { max-width: 1240px; margin: 32px auto; padding: 0 24px 40px; }

.filter-bar {
  display: flex; align-items: flex-end; flex-wrap: wrap; gap: 12px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 16px 18px; margin-bottom: 22px;
}
.filter-field { display: flex; flex-direction: column; gap: 5px; }
.filter-field label { font-size: .7rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; }
.filter-field input {
  background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text); padding: 8px 10px; font-size: .82rem; font-family: inherit; outline: none;
}
.filter-actions { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 9px 16px; border-radius: var(--radius-sm); border: 1px solid transparent;
  font-size: .8rem; font-weight: 600; cursor: pointer; text-decoration: none;
  font-family: inherit; transition: all .15s;
}
.btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
.btn-primary:hover { box-shadow: 0 4px 14px -4px rgba(79,70,229,.5); }
.btn-secondary { background: var(--surface2); border-color: var(--border); color: var(--text); }
.btn-secondary:hover { border-color: var(--accent); color: var(--accent); }

.stat-grid {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 22px;
}
.stat-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 18px 20px;
}
.stat-card .label { font-size: .72rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
.stat-card .value { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em; }
.stat-card .sub { font-size: .74rem; color: var(--text-muted); margin-top: 4px; }

.panel-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; margin-bottom: 16px; }
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 18px 20px;
}
.panel h2 { font-size: .92rem; font-weight: 700; margin-bottom: 4px; }
.panel .panel-sub { font-size: .74rem; color: var(--text-muted); margin-bottom: 12px; }
.chart-svg { width: 100%; height: auto; display: block; }
.chart-axis-label { fill: var(--text-muted); }
.chart-value-label { fill: var(--text); font-weight: 600; }
.chart-empty { font-size: .8rem; color: var(--text-muted); padding: 30px 0; text-align: center; }

.donut-wrap { display: flex; align-items: center; gap: 18px; }
.donut-wrap .chart-svg { max-width: 150px; flex-shrink: 0; }
.legend { display: flex; flex-direction: column; gap: 8px; }
.legend-item { display: flex; align-items: center; gap: 8px; font-size: .8rem; }
.legend-swatch { width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }
.legend-value { margin-left: auto; font-weight: 600; }

.table-scroll { overflow-x: auto; }
table.dash-table { width: 100%; border-collapse: collapse; font-size: .84rem; }
table.dash-table th {
  text-align: left; padding: 10px 12px; font-size: .7rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--text-muted); border-bottom: 1px solid var(--border);
}
table.dash-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
table.dash-table tr:last-child td { border-bottom: none; }

.tab-bar { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid var(--border); }
.tab-btn {
  padding: 10px 18px; font-size: .85rem; font-weight: 600; color: var(--text-muted);
  background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer;
  font-family: inherit; margin-bottom: -1px; transition: color .15s, border-color .15s;
}
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-btn:hover:not(.active) { color: var(--text); }
.tab-content { display: none; }
.tab-content.active { display: block; }

.cluster-heading { display: flex; align-items: center; gap: 8px; }
.cluster-heading svg { width: 18px; height: 18px; color: var(--accent); flex-shrink: 0; }
.cluster-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 16px; margin-top: 14px;
}
.cluster-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm);
  box-shadow: var(--shadow); overflow: hidden; display: flex; flex-direction: column;
}
.cluster-card-header {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 12px 14px; background: linear-gradient(135deg, #1e2a4a, #16213e); color: #fff;
}
.cluster-card-name { font-size: .88rem; font-weight: 700; }
.cluster-card-count {
  font-size: .72rem; font-weight: 700; background: rgba(255,255,255,0.18);
  color: #fff; padding: 2px 9px; border-radius: 999px; flex-shrink: 0;
}
.cluster-card-body { padding: 6px 14px; max-height: 260px; overflow-y: auto; }
.cluster-user-row {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 0; border-bottom: 1px solid var(--border); font-size: .8rem;
}
.cluster-card-body .cluster-user-row:last-child { border-bottom: none; }
.cluster-user-name { color: var(--text); }
.cluster-user-code { color: var(--text-muted); font-size: .74rem; }
.cluster-user-badge {
  font-size: .7rem; font-weight: 600; color: var(--text-muted); background: var(--surface2);
  border: 1px solid var(--border); padding: 2px 8px; border-radius: 999px; flex-shrink: 0;
  white-space: nowrap;
}

.tl-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 16px; margin-top: 14px;
}
.tl-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm);
  box-shadow: var(--shadow); overflow: hidden; display: flex; flex-direction: column;
  transition: box-shadow .15s, transform .15s;
}
.tl-card:hover { box-shadow: 0 4px 18px -6px rgba(30,42,74,.35); transform: translateY(-1px); }
.tl-card-header {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 12px 14px; background: linear-gradient(135deg, #1e2a4a, #16213e); color: #fff;
}
.tl-card-name { font-size: .86rem; font-weight: 700; }
.tl-card-count {
  width: 24px; height: 24px; flex-shrink: 0; border-radius: 50%;
  background: #fff; color: #1e2a4a; font-size: .74rem; font-weight: 800;
  display: flex; align-items: center; justify-content: center;
}
.tl-card-body { padding: 6px 14px; max-height: 260px; overflow-y: auto; }
.tl-user-row {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 0; border-bottom: 1px solid var(--border); font-size: .8rem;
}
.tl-card-body .tl-user-row:last-child { border-bottom: none; }
.tl-user-name { color: var(--text); }
.tl-user-code { color: var(--text-muted); font-size: .74rem; }
.tl-user-badge {
  font-size: .7rem; font-weight: 700; color: #1e2a4a; background: rgba(30,42,74,.08);
  border: 1px solid rgba(30,42,74,.2); padding: 2px 9px; border-radius: 999px;
  flex-shrink: 0; white-space: nowrap;
}

.time-stat-grid { grid-template-columns: repeat(4, 1fr); margin-top: -8px; }
.time-stat-grid .stat-card .value { font-size: 1.5rem; font-variant-numeric: tabular-nums; }
.review-panel { margin-bottom: 16px; }
table.dash-table td.num, table.dash-table th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.muted-code { color: var(--text-muted); font-size: .74rem; }

@media (max-width: 1200px) {
  .stat-grid { grid-template-columns: repeat(3, 1fr); }
  .time-stat-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 900px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .panel-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>
  </div>
  <div class="header-text">
    <h1>Dashboard</h1>
    <p>Org-wide redaction activity &mdash; admin view</p>
  </div>
  <div style="margin-left:auto; display:flex; align-items:center; gap:16px;">
    <a href="/" class="back-link">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
      Redaction Pipeline
    </a>
  </div>
</header>

<main>
  <form class="filter-bar" method="get" action="/dashboard">
    <input type="hidden" id="tab-input" name="tab" value="{{ active_tab }}">
    <div class="filter-field">
      <label for="start">From</label>
      <input type="date" id="start" name="start" value="{{ start_date }}">
    </div>
    <div class="filter-field">
      <label for="end">To</label>
      <input type="date" id="end" name="end" value="{{ end_date }}">
    </div>
    <div class="filter-actions">
      <button type="submit" class="btn btn-primary">Apply</button>
      <button type="button" class="btn btn-secondary" onclick="location.reload()">&#8635; Refresh</button>
      <a class="btn btn-secondary" href="/dashboard/download?start={{ start_date }}&amp;end={{ end_date }}">&#8681; Download Report (.xlsx)</a>
    </div>
  </form>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Total Files Processed</div>
      <div class="value">{{ summary.total_files }}</div>
      <div class="sub">{{ summary.distinct_clients }} client(s) active</div>
    </div>
    <div class="stat-card">
      <div class="label">PII Values Redacted</div>
      <div class="value">{{ summary.total_redacted }}</div>
      <div class="sub">across all files in range</div>
    </div>
    <div class="stat-card">
      <div class="label">Upload Success Rate</div>
      <div class="value">{% if summary.success_rate is not none %}{{ "%.1f"|format(summary.success_rate) }}%{% else %}N/A{% endif %}</div>
      <div class="sub">{{ summary.success_count }} ok / {{ summary.fail_count }} failed</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Uploads</div>
      <div class="value">{{ summary.total_uploads }}</div>
      <div class="sub">{{ summary.pending_count }} pending</div>
    </div>
    <div class="stat-card">
      <div class="label">Users Active</div>
      <div class="value">{{ summary.distinct_users }}</div>
      <div class="sub">uploaded in this range</div>
    </div>
  </div>

  <div class="stat-grid time-stat-grid" title="Averages per reviewed batch in this range">
    <div class="stat-card">
      <div class="label">Avg System Time</div>
      <div class="value">{{ hms(review.summary.avg_system_seconds) }}</div>
      <div class="sub">pipeline processing per batch</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Review Time</div>
      <div class="value">{{ hms(review.summary.avg_active_seconds) }}</div>
      <div class="sub">Total {{ hms(review.summary.total_active_seconds) }} &middot; {{ review.summary.batch_count }} batch(es)</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Idle Time</div>
      <div class="value">{{ hms(review.summary.avg_idle_seconds) }}</div>
      <div class="sub">Total {{ hms(review.summary.total_idle_seconds) }}</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Elapsed Time</div>
      <div class="value">{{ hms(review.summary.avg_elapsed_seconds) }}</div>
      <div class="sub">review + idle + other tabs</div>
    </div>
  </div>

  <div class="tab-bar">
    <button type="button" class="tab-btn {{ 'active' if active_tab == 'summary' else '' }}" data-tab="summary" onclick="showDashboardTab('summary')">Summary</button>
    <button type="button" class="tab-btn {{ 'active' if active_tab == 'vertical' else '' }}" data-tab="vertical" onclick="showDashboardTab('vertical')">Vertical</button>
    <button type="button" class="tab-btn {{ 'active' if active_tab == 'cluster' else '' }}" data-tab="cluster" onclick="showDashboardTab('cluster')">Cluster</button>
    <button type="button" class="tab-btn {{ 'active' if active_tab == 'tl' else '' }}" data-tab="tl" onclick="showDashboardTab('tl')">TL</button>
    <button type="button" class="tab-btn {{ 'active' if active_tab == 'review' else '' }}" data-tab="review" onclick="showDashboardTab('review')">Review Time</button>
  </div>

  <div id="tab-summary" class="tab-content {{ 'active' if active_tab == 'summary' else '' }}">
    <div class="panel-grid">
      <div class="panel">
        <h2>Upload Volume Over Time</h2>
        <div class="panel-sub">Files uploaded per day</div>
        {{ volume_svg | safe }}
      </div>
      <div class="panel">
        <h2>Success / Fail Breakdown</h2>
        <div class="panel-sub">Upload outcomes in range</div>
        <div class="donut-wrap">
          {{ status_svg | safe }}
          <div class="legend">
            {% for item in status_legend %}
            <div class="legend-item">
              <span class="legend-swatch" style="background:{{ item.color }}"></span>
              <span>{{ item.label }}</span>
              <span class="legend-value">{{ item.value }}</span>
            </div>
            {% endfor %}
          </div>
        </div>
      </div>
    </div>

    <div class="panel-grid">
      <div class="panel table-panel">
        <h2>Top Clients by Volume</h2>
        <div class="panel-sub">Ranked by files processed in range</div>
        <div class="table-scroll">
          <table class="dash-table">
            <thead>
              <tr><th>Client</th><th>Files</th><th>PII Redacted</th><th>Last Activity</th></tr>
            </thead>
            <tbody>
              {% for row in top_clients %}
              <tr>
                <td>{{ row.client_name }}</td>
                <td>{{ row.file_count }}</td>
                <td>{{ row.redaction_count }}</td>
                <td>{{ row.last_activity or "—" }}</td>
              </tr>
              {% else %}
              <tr><td colspan="4" style="color:var(--text-muted); text-align:center; padding:24px;">No activity in this range.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>PII Entities Masked by Category</h2>
        <div class="panel-sub">Top categories in range</div>
        {{ category_svg | safe }}
      </div>
    </div>
  </div>

  <div id="tab-vertical" class="tab-content {{ 'active' if active_tab == 'vertical' else '' }}">
    <div class="panel-grid">
      <div class="panel table-panel">
        <h2>Usage by Vertical</h2>
        <div class="table-scroll">
          <table class="dash-table">
            <thead>
              <tr><th>Vertical</th><th>Uploads</th><th>Users</th><th>Success</th><th>Failed</th></tr>
            </thead>
            <tbody>
              {% for row in vertical_breakdown %}
              <tr>
                <td>{{ row.vertical_name }}</td>
                <td>{{ row.upload_count }}</td>
                <td>{{ row.user_count }}</td>
                <td>{{ row.success_count }}</td>
                <td>{{ row.fail_count }}</td>
              </tr>
              {% else %}
              <tr><td colspan="5" style="color:var(--text-muted); text-align:center; padding:24px;">No activity in this range.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>Uploads by Vertical</h2>
        <div class="panel-sub">Top verticals in range</div>
        {{ vertical_svg | safe }}
      </div>
    </div>
  </div>

  <div id="tab-cluster" class="tab-content {{ 'active' if active_tab == 'cluster' else '' }}">
    <div class="panel">
      <h2 class="cluster-heading">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        Users by Cluster
      </h2>
      <div class="cluster-grid">
        {% for cluster in cluster_user_breakdown %}
        <div class="cluster-card">
          <div class="cluster-card-header">
            <span class="cluster-card-name">{{ cluster.cluster_head_name }}</span>
            <span class="cluster-card-count">{{ cluster.users|length }}</span>
          </div>
          <div class="cluster-card-body">
            {% for u in cluster.users %}
            <div class="cluster-user-row">
              <span class="cluster-user-name">{{ u.name }} <span class="cluster-user-code">- {{ u.employee_id }}</span></span>
              <span class="cluster-user-badge">{{ u.file_count }} file{{ 's' if u.file_count != 1 else '' }}</span>
            </div>
            {% endfor %}
          </div>
        </div>
        {% else %}
        <div class="chart-empty">No activity in this range.</div>
        {% endfor %}
      </div>
    </div>
  </div>

  <div id="tab-tl" class="tab-content {{ 'active' if active_tab == 'tl' else '' }}">
    <div class="panel">
      <h2 class="cluster-heading">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        Users by Team Leader
      </h2>
      <div class="tl-grid">
        {% for card in tl_user_breakdown %}
        <div class="tl-card">
          <div class="tl-card-header">
            <span class="tl-card-name">TL &ndash; {{ card.tl_name }}</span>
            <span class="tl-card-count">{{ card.users|length }}</span>
          </div>
          <div class="tl-card-body">
            {% for u in card.users %}
            <div class="tl-user-row">
              <span class="tl-user-name">{{ u.name }} <span class="tl-user-code">- {{ u.employee_id }}</span></span>
              <span class="tl-user-badge">{{ u.file_count }} file{{ 's' if u.file_count != 1 else '' }}</span>
            </div>
            {% endfor %}
          </div>
        </div>
        {% else %}
        <div class="chart-empty">No activity in this range.</div>
        {% endfor %}
      </div>
    </div>
  </div>

  <div id="tab-review" class="tab-content {{ 'active' if active_tab == 'review' else '' }}">
    <div class="panel review-panel">
      <h2>Review Time by User</h2>
      <div class="panel-sub">Averages are per batch. Review excludes idle and other-tab time. Sorted by total review time.</div>
      <div class="table-scroll">
        <table class="dash-table">
          <thead><tr><th>User</th><th class="num">Batches</th><th class="num">Files</th><th class="num">Total Review</th><th class="num">Avg System</th><th class="num">Avg Review</th><th class="num">Avg Idle</th><th class="num">Avg Elapsed</th></tr></thead>
          <tbody>
            {% for row in review.per_user %}
            <tr>
              <td>{{ row.user_name }} <span class="muted-code">- {{ row.employee_id }}</span></td>
              <td class="num">{{ row.batch_count }}</td>
              <td class="num">{{ row.file_count }}</td>
              <td class="num">{{ hms(row.total_active_seconds) }}</td>
              <td class="num">{{ hms(row.avg_system_seconds) }}</td>
              <td class="num">{{ hms(row.avg_active_seconds) }}</td>
              <td class="num">{{ hms(row.avg_idle_seconds) }}</td>
              <td class="num">{{ hms(row.avg_elapsed_seconds) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="8" style="color:var(--text-muted); text-align:center; padding:24px;">No reviewed batches in this range.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel review-panel">
      <h2>Review Time by Client</h2>
      <div class="panel-sub">Averages are per batch</div>
      <div class="table-scroll">
        <table class="dash-table">
          <thead><tr><th>Client</th><th class="num">Batches</th><th class="num">Files</th><th class="num">Total Review</th><th class="num">Avg System</th><th class="num">Avg Review</th><th class="num">Avg Idle</th><th class="num">Avg Elapsed</th></tr></thead>
          <tbody>
            {% for row in review.per_client %}
            <tr>
              <td>{{ row.client_name }}</td>
              <td class="num">{{ row.batch_count }}</td>
              <td class="num">{{ row.file_count }}</td>
              <td class="num">{{ hms(row.total_active_seconds) }}</td>
              <td class="num">{{ hms(row.avg_system_seconds) }}</td>
              <td class="num">{{ hms(row.avg_active_seconds) }}</td>
              <td class="num">{{ hms(row.avg_idle_seconds) }}</td>
              <td class="num">{{ hms(row.avg_elapsed_seconds) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="8" style="color:var(--text-muted); text-align:center; padding:24px;">No reviewed batches in this range.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel review-panel">
      <h2>Recent Batches</h2>
      <div class="panel-sub">Latest {{ review.recent|length }} of {{ review.batches|length }} in range, newest first. The Excel report lists all of them.</div>
      <div class="table-scroll">
        <table class="dash-table">
          <thead>
            <tr><th>Last Download</th><th>Process</th><th>Client</th><th>User</th><th class="num">Files</th>
                <th class="num">System</th><th class="num">Review</th><th class="num">Idle</th><th class="num">Elapsed</th><th class="num">Downloads</th></tr>
          </thead>
          <tbody>
            {% for row in review.recent %}
            <tr>
              <td style="white-space:nowrap">{{ row.last_download_at or "—" }}</td>
              <td>{{ row.process_name }} <span class="muted-code">{{ row.process_id }}</span></td>
              <td>{{ row.client_name }}</td>
              <td>{{ row.user_name }}</td>
              <td class="num">{{ row.file_count }}</td>
              <td class="num">{{ hms(row.system_seconds) }}</td>
              <td class="num">{{ hms(row.active_seconds) }}</td>
              <td class="num">{{ hms(row.idle_seconds) }}</td>
              <td class="num">{{ hms(row.elapsed_seconds) }}</td>
              <td class="num">{{ row.download_count }}</td>
            </tr>
            {% else %}
            <tr><td colspan="10" style="color:var(--text-muted); text-align:center; padding:24px;">No reviewed batches in this range.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</main>
<script>
function showDashboardTab(name) {
  document.querySelectorAll('.tab-content').forEach(function(el) {
    el.classList.toggle('active', el.id === 'tab-' + name);
  });
  document.querySelectorAll('.tab-btn').forEach(function(el) {
    el.classList.toggle('active', el.dataset.tab === name);
  });
  var hidden = document.getElementById('tab-input');
  if (hidden) hidden.value = name;
  try {
    var url = new URL(window.location);
    url.searchParams.set('tab', name);
    history.replaceState(null, '', url);
  } catch (e) {}
}
</script>
</body>
</html>"""


DASHBOARD_DENIED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Access Denied — Dashboard</title>
  <style>
    :root {
      --bg: #f3f5fb; --surface: #ffffff; --text: #171923; --text-muted: #6b7280;
      --accent: #4f46e5; --accent2: #2563eb; --border: rgba(23,25,35,0.08);
      --radius: 16px; --shadow: 0 1px 2px rgba(23,25,35,.04), 0 12px 28px -16px rgba(79,70,229,.16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text);
      min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .card {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      box-shadow: var(--shadow); padding: 32px; max-width: 480px; width: 100%; text-align: center;
    }
    h1 { margin: 0 0 10px; font-size: 1.5rem; }
    p { margin: 0 0 18px; color: var(--text-muted); line-height: 1.6; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; padding: 10px 16px;
      border-radius: 999px; background: linear-gradient(135deg, var(--accent), var(--accent2));
      color: #fff; text-decoration: none; font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Access Denied</h1>
    <p>The dashboard is restricted to authorized administrators. If you believe you should have access, contact your system administrator.</p>
    <a class="btn" href="/">Back to Redaction Pipeline</a>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@dashboard_bp.route("/dashboard")
def dashboard_page():
    uid = _dashboard_authorize()
    if uid is None:
        return render_template_string(DASHBOARD_DENIED_HTML), 403

    start_date = request.args.get("start", "").strip() or None
    end_date = request.args.get("end", "").strip() or None
    if not start_date and not end_date:
        start_date, end_date = _dashboard_default_range()

    summary = db.get_dashboard_summary(start_date, end_date)
    volume = db.get_upload_volume_over_time(start_date, end_date)
    status_breakdown = db.get_upload_status_breakdown(start_date, end_date)
    category_breakdown = db.get_pii_category_breakdown(start_date, end_date)
    top_clients = db.get_top_clients(start_date, end_date)
    vertical_breakdown = db.get_vertical_breakdown(start_date, end_date)
    cluster_user_breakdown = db.get_cluster_user_breakdown(start_date, end_date)
    tl_user_breakdown = db.get_tl_user_breakdown(start_date, end_date)
    review = db.get_review_time_breakdown(start_date, end_date)

    status_colors = {"success": "#15803d", "fail": "#b91c1c", "pending": "#92400e"}
    status_labels = {"success": "Success", "fail": "Failed", "pending": "Pending"}
    status_segments = [
        (status_labels[k], status_breakdown.get(k, 0), status_colors[k])
        for k in ("success", "fail", "pending")
    ]

    active_tab = request.args.get("tab", "summary").strip().lower()
    if active_tab not in ("summary", "vertical", "cluster", "tl", "review"):
        active_tab = "summary"

    return render_template_string(
        DASHBOARD_HTML,
        start_date=start_date or "",
        end_date=end_date or "",
        active_tab=active_tab,
        summary=summary,
        volume_svg=_dashboard_bar_chart_svg(volume, "date", "count"),
        status_svg=_dashboard_donut_chart_svg(status_segments),
        status_legend=[
            {"label": label, "value": value, "color": color}
            for label, value, color in status_segments
        ],
        category_svg=_dashboard_hbar_chart_svg(
            category_breakdown, "category", "count", colors=DASHBOARD_CHART_COLORS
        ),
        top_clients=top_clients,
        vertical_breakdown=vertical_breakdown,
        vertical_svg=_dashboard_hbar_chart_svg(
            vertical_breakdown, "vertical_name", "upload_count", colors=DASHBOARD_CHART_COLORS
        ),
        cluster_user_breakdown=cluster_user_breakdown,
        tl_user_breakdown=tl_user_breakdown,
        review=review,
        hms=_fmt_hms,
    )


@dashboard_bp.route("/dashboard/download")
def dashboard_download():
    uid = _dashboard_authorize()
    if uid is None:
        return jsonify({"error": "Not authorized"}), 403

    start_date = request.args.get("start", "").strip() or None
    end_date = request.args.get("end", "").strip() or None
    if not start_date and not end_date:
        start_date, end_date = _dashboard_default_range()

    summary = db.get_dashboard_summary(start_date, end_date)
    volume = db.get_upload_volume_over_time(start_date, end_date)
    status_breakdown = db.get_upload_status_breakdown(start_date, end_date)
    category_breakdown = db.get_pii_category_breakdown(start_date, end_date, limit=1000)
    top_clients = db.get_top_clients(start_date, end_date, limit=1000)
    vertical_breakdown = db.get_vertical_breakdown(start_date, end_date)
    cluster_breakdown = db.get_cluster_breakdown(start_date, end_date)
    tl_user_breakdown = db.get_tl_user_breakdown(start_date, end_date)
    review = db.get_review_time_breakdown(start_date, end_date)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["PII Redaction Dashboard Report"])
    ws.append([f"Range: {start_date or 'all-time'} to {end_date or 'all-time'}"])
    ws.append([])
    ws.append(["Metric", "Value"])
    ws.append(["Total files processed", summary["total_files"]])
    ws.append(["Total PII values redacted", summary["total_redacted"]])
    ws.append(["Distinct clients touched", summary["distinct_clients"]])
    ws.append(["Users active", summary["distinct_users"]])
    ws.append(["Total uploads", summary["total_uploads"]])
    ws.append(["Successful uploads", summary["success_count"]])
    ws.append(["Failed uploads", summary["fail_count"]])
    ws.append(["Pending uploads", summary["pending_count"]])
    ws.append([
        "Success rate",
        f"{summary['success_rate']:.1f}%" if summary["success_rate"] is not None else "N/A",
    ])
    rs = review["summary"]
    ws.append(["Batches reviewed", rs["batch_count"]])
    ws.append(["Avg system time", _fmt_hms(rs["avg_system_seconds"])])
    ws.append(["Avg review time", _fmt_hms(rs["avg_active_seconds"])])
    ws.append(["Avg idle time", _fmt_hms(rs["avg_idle_seconds"])])
    ws.append(["Avg elapsed time", _fmt_hms(rs["avg_elapsed_seconds"])])
    ws.append(["Total review time", _fmt_hms(rs["total_active_seconds"])])

    ws_volume = wb.create_sheet("Upload Volume")
    ws_volume.append(["Date", "Uploads"])
    for row in volume:
        ws_volume.append([row["date"], row["count"]])
    if volume:
        chart = LineChart()
        chart.title = "Upload Volume Over Time"
        chart.y_axis.title = "Uploads"
        chart.x_axis.title = "Date"
        chart.height, chart.width = 9, 18
        data = Reference(ws_volume, min_col=2, min_row=1, max_row=len(volume) + 1)
        cats = Reference(ws_volume, min_col=1, min_row=2, max_row=len(volume) + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws_volume.add_chart(chart, "D2")

    ws_status = wb.create_sheet("Status Breakdown")
    ws_status.append(["Status", "Count"])
    for status, count in status_breakdown.items():
        ws_status.append([status, count])
    if status_breakdown:
        chart = PieChart()
        chart.title = "Success / Fail Breakdown"
        n = len(status_breakdown)
        data = Reference(ws_status, min_col=2, min_row=1, max_row=n + 1)
        cats = Reference(ws_status, min_col=1, min_row=2, max_row=n + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws_status.add_chart(chart, "D2")

    ws_category = wb.create_sheet("PII Category Breakdown")
    ws_category.append(["Category", "Count"])
    for row in category_breakdown:
        ws_category.append([row["category"], row["count"]])
    if category_breakdown:
        chart = BarChart()
        chart.type = "bar"
        chart.title = "PII Entities Masked by Category"
        chart.y_axis.title = "Category"
        chart.x_axis.title = "Count"
        chart.height, chart.width = 9, 18
        n = len(category_breakdown)
        data = Reference(ws_category, min_col=2, min_row=1, max_row=n + 1)
        cats = Reference(ws_category, min_col=1, min_row=2, max_row=n + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws_category.add_chart(chart, "D2")

    ws_clients = wb.create_sheet("Top Clients")
    ws_clients.append(["Client", "Files", "PII Redacted", "Last Activity"])
    for row in top_clients:
        ws_clients.append(
            [row["client_name"], row["file_count"], row["redaction_count"], row["last_activity"]]
        )

    ws_vertical = wb.create_sheet("Vertical Breakdown")
    ws_vertical.append(["Vertical", "Uploads", "Users", "Success", "Failed"])
    for row in vertical_breakdown:
        ws_vertical.append(
            [row["vertical_name"], row["upload_count"], row["user_count"],
             row["success_count"], row["fail_count"]]
        )
    if vertical_breakdown:
        chart = BarChart()
        chart.type = "col"
        chart.title = "Uploads by Vertical"
        chart.y_axis.title = "Uploads"
        chart.x_axis.title = "Vertical"
        chart.height, chart.width = 9, 18
        n = len(vertical_breakdown)
        data = Reference(ws_vertical, min_col=2, min_row=1, max_row=n + 1)
        cats = Reference(ws_vertical, min_col=1, min_row=2, max_row=n + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws_vertical.add_chart(chart, "G2")

    ws_cluster = wb.create_sheet("Cluster Breakdown")
    ws_cluster.append(["Cluster Head", "Team Size", "Active Users", "Uploads", "Success", "Failed"])
    for row in cluster_breakdown:
        ws_cluster.append(
            [row["cluster_head_name"], row["team_size"], row["active_user_count"],
             row["upload_count"], row["success_count"], row["fail_count"]]
        )

    ws_tl = wb.create_sheet("TL Breakdown")
    ws_tl.append(["Team Leader", "Employee", "Employee ID", "Files"])
    for card in tl_user_breakdown:
        for u in card["users"]:
            ws_tl.append([card["tl_name"], u["name"], u["employee_id"], u["file_count"]])

    _append_review_time_sheets(wb, review)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"PII_Dashboard_Report_{start_date or 'all'}_to_{end_date or 'all'}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _append_review_time_sheets(wb, review):
    """Review-timer sheets for the Excel report. Durations are written as
    Excel time values (seconds / 86400) formatted [h]:mm:ss, so they display
    as HH:MM:SS but can still be summed and sorted in Excel."""
    def as_time(seconds):
        return seconds / 86400 if seconds is not None else None

    def write(ws, header, rows, time_cols):
        ws.append(header)
        for row in rows:
            ws.append(row)
        for col in time_cols:
            for cells in ws.iter_cols(min_col=col, max_col=col, min_row=2):
                for cell in cells:
                    cell.number_format = "[h]:mm:ss"

    agg_header = ["Batches", "Files", "Total Review", "Avg System", "Avg Review", "Avg Idle", "Avg Elapsed"]

    def agg_cells(a):
        return [a["batch_count"], a["file_count"], as_time(a["total_active_seconds"]),
                as_time(a["avg_system_seconds"]), as_time(a["avg_active_seconds"]),
                as_time(a["avg_idle_seconds"]), as_time(a["avg_elapsed_seconds"])]

    write(wb.create_sheet("Review Time by User"), ["User", "Employee ID"] + agg_header,
          [[u["user_name"], u["employee_id"]] + agg_cells(u) for u in review["per_user"]],
          time_cols=range(5, 10))
    write(wb.create_sheet("Review Time by Client"), ["Client"] + agg_header,
          [[c["client_name"]] + agg_cells(c) for c in review["per_client"]],
          time_cols=range(4, 9))
    write(wb.create_sheet("Review Time Batches"),
          ["Last Download", "Process ID", "Process Name", "Client", "User", "Employee ID", "Files",
           "System", "Review", "Idle", "Tab Hidden", "Elapsed", "Downloads"],
          [[b["last_download_at"], b["process_id"], b["process_name"], b["client_name"], b["user_name"],
            b["employee_id"], b["file_count"], as_time(b["system_seconds"]), as_time(b["active_seconds"]),
            as_time(b["idle_seconds"]), as_time(b["tab_hidden_seconds"]), as_time(b["elapsed_seconds"]),
            b["download_count"]] for b in review["batches"]],
          time_cols=range(8, 13))
