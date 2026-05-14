#!/usr/bin/env bash
# AI Coding Gym supervisor: watches the problem folder, appends compact
# activity cards to dashboard.html, and auto-runs the notebook metric
# extractor whenever files change.
#
# Design goals:
#   * Zero external deps beyond coreutils + diff + python (rsync optional).
#   * Compact dashboard: per-file +/- summary up front, full diffs hidden
#     behind <details>, notebooks never dumped as raw JSON, binary files
#     reported by name only.
#   * First run after fetch shows a "Supervisor Ready" card, never the
#     entire workspace as a "+everything" diff.
#   * Idempotent: re-running is safe; the lock file prevents doubles.
#
# Canonical copy: publish via gym-environment on GitHub; the CLI vendors
# templates/supervisor.sh.template derived from this file (placeholder commands).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_PATH="$ROOT_DIR/dashboard.html"
NOTEBOOK_PATH="$ROOT_DIR/solution.ipynb"
SNAPSHOT_DIR="$ROOT_DIR/.supervisor_snapshot"
LOCK_FILE="$ROOT_DIR/.supervisor.lock"
HELPER="$ROOT_DIR/tools/notebook_metrics.py"
APPROACH_HELPER="$ROOT_DIR/tools/summarize_approach.py"
DEFAULT_CMD="aicodinggym mle log show <competition_or_problem_id>"
SUBMIT_CMD="aicodinggym mle submit <competition_or_problem_id> -F submission.csv"
WATCH_INTERVAL=3
MAX_DIFF_LINES_PER_FILE=200
MAX_OUTPUT_LINES=200
MAX_AI_SUMMARY_CHARS=700
AGENT_NOTE_PATH="$ROOT_DIR/.agent_note.json"

read_agent_note() {
  AGENT_SUMMARY=""
  AGENT_WHY=""
  AGENT_APPROACH=""
  AGENT_STAGE=""
  AGENT_IMPACT=""
  AGENT_PROMPT=""
  AGENT_NEXT_PROMPT=""
  AGENT_PROMPT_ID=""
  AGENT_PROMPT_TS=""
  if [[ -f "$AGENT_NOTE_PATH" ]]; then
    local parsed
    parsed="$(PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$AGENT_NOTE_PATH" 2>/dev/null <<'PY'
import json, sys, pathlib
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
except Exception:
    sys.exit(0)
mapping = [
    ("change_summary", "AGENT_SUMMARY"),
    ("why",            "AGENT_WHY"),
    ("approach_description", "AGENT_APPROACH"),
    ("stage_label",    "AGENT_STAGE"),
    ("impact",         "AGENT_IMPACT"),
    ("user_prompt",    "AGENT_PROMPT"),
    ("prompt",         "AGENT_PROMPT"),
    ("prompt_text",    "AGENT_PROMPT"),
    ("user_prompt_text", "AGENT_PROMPT"),
    ("next_prompt",    "AGENT_NEXT_PROMPT"),
    ("nextPrompt",     "AGENT_NEXT_PROMPT"),
    ("next_prompt_text","AGENT_NEXT_PROMPT"),
    ("prompt_id",      "AGENT_PROMPT_ID"),
    ("prompt_ts",      "AGENT_PROMPT_TS"),
]
resolved = {}
for key, var in mapping:
    raw = data.get(key, "")
    val = str(raw).strip() if raw is not None else ""
    if not val and resolved.get(var):
        continue
    resolved[var] = val

for var in [
    "AGENT_SUMMARY",
    "AGENT_WHY",
    "AGENT_APPROACH",
    "AGENT_STAGE",
    "AGENT_IMPACT",
    "AGENT_PROMPT",
    "AGENT_NEXT_PROMPT",
    "AGENT_PROMPT_ID",
    "AGENT_PROMPT_TS",
]:
    val = resolved.get(var, "").replace("\n", " ").replace("'", "'\\''")
    print(f"{var}='{val}'")
PY
    )" || true
    [[ -n "$parsed" ]] && eval "$parsed"
    # Write notebook_analysis and model fields to persistent cache so they
    # survive agent_note deletion and reach the metric card.
    local _cache="$ROOT_DIR/.supervisor_agent_meta_cache.json"
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$AGENT_NOTE_PATH" "$_cache" 2>/dev/null <<'PYCACHE' || true
import json, sys, pathlib
note_path = pathlib.Path(sys.argv[1])
cache_path = pathlib.Path(sys.argv[2])
if not note_path.exists():
    sys.exit(0)
try:
    data = json.loads(note_path.read_text(encoding="utf-8-sig"))
except Exception:
    sys.exit(0)
cache = {}
nb = data.get("notebook_analysis", data.get("notebookAnalysis"))
if isinstance(nb, dict) and nb.get("cells"):
    cache["notebook_analysis"] = nb
model = data.get("model")
if isinstance(model, dict) and model:
    cache["model"] = model
if cache:
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
elif cache_path.exists():
    cache_path.unlink()
PYCACHE
    rm -f "$AGENT_NOTE_PATH"
  fi
  # Plain-text prompt fallback: agent writes .prompt (one file, no JSON required).
  # Cursor, Windsurf, and other tools that skip .agent_note.json can use this.
  # Not deleted — persists as the current prompt context until the user updates it.
  local _pt="$ROOT_DIR/.prompt"
  if [[ -f "$_pt" ]] && [[ -z "${AGENT_PROMPT:-}" ]]; then
    AGENT_PROMPT="$(tr '\n' ' ' <"$_pt")"
  fi
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

html_escape() {
  sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

PY_BIN="${PYTHON:-python}"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN=python3

ensure_dashboard() {
  if [[ -f "$DASHBOARD_PATH" ]]; then
    return
  fi
  cat >"$DASHBOARD_PATH" <<'EOF'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="5" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MLE Bench Logger</title>
  <link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%23F97316'/><text x='50%25' y='56%25' text-anchor='middle' font-family='Consolas,monospace' font-size='18' font-weight='800' fill='white'>%3E_</text></svg>" />
  <style>
    :root {
      color-scheme: light;
      --bg: #FAFAF9;
      --surface: #FFFFFF;
      --surface-muted: #F5F5F4;
      --panel: #FFFFFF;
      --border: #E7E5E4;
      --border-strong: #D6D3D1;
      --text: #1C1917;
      --text-soft: #44403C;
      --muted: #78716C;
      --accent: #F97316;
      --accent-strong: #EA580C;
      --accent-soft: #FFF7ED;
      --plus: #15803D;
      --plus-bg: #DCFCE7;
      --minus: #B91C1C;
      --minus-bg: #FEE2E2;
      --info: #1D4ED8;
      --info-bg: #DBEAFE;
      --code-bg: #FFF7ED;
      --code-fg: #9A3412;
      --shadow-sm: 0 1px 2px rgba(28,25,23,0.04);
      --shadow-md: 0 4px 16px rgba(28,25,23,0.06);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; }
    body { font-family: ui-sans-serif, -apple-system, "Segoe UI", Inter, Roboto, Arial, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }

    /* Header / brand --------------------------------------------------- */
    header { position: sticky; top: 0; z-index: 10; background: rgba(255,255,255,0.88); backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark { width: 36px; height: 36px; border-radius: 9px; background: var(--accent); color: #fff; display: inline-flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(249,115,22,0.25); font-family: ui-monospace, Consolas, Menlo, monospace; font-weight: 800; font-size: 16px; letter-spacing: -1px; }
    .brand-text { display: flex; flex-direction: column; line-height: 1.15; }
    .brand-title { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.01em; }
    .brand-sub { font-size: 11.5px; color: var(--muted); }
    .brand-sub .dot { margin: 0 6px; color: var(--border-strong); }
    .beta { display: inline-block; padding: 2px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent-strong); font-size: 10px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; margin-left: 6px; vertical-align: middle; }

    .stats { display: flex; align-items: center; gap: 22px; margin-left: auto; }
    .stat { display: flex; flex-direction: column; gap: 2px; }
    .stat .label { color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
    .stat .value { font-size: 16px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
    .stat .value.accent { color: var(--accent-strong); }

    main { max-width: 1120px; margin: 0 auto; padding: 24px 28px 56px; }

    /* Panels & cards --------------------------------------------------- */
    .panel { border: 1px solid var(--border); border-radius: 14px; padding: 16px 20px; margin-bottom: 20px; background: var(--panel); box-shadow: var(--shadow-sm); }
    .panel h2 { margin: 0 0 10px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }

    .card { border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px; margin-bottom: 10px; background: var(--surface); box-shadow: var(--shadow-sm); }
    .card .row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .card h3 { margin: 0; font-size: 14px; font-weight: 700; color: var(--text); letter-spacing: -0.005em; }
    .card .time { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; margin-left: auto; }
    .card .meta { color: var(--text-soft); font-size: 12.5px; margin: 6px 0 0; word-break: break-word; }
    .card .meta code { background: var(--code-bg); padding: 1px 6px; border-radius: 5px; color: var(--code-fg); font-size: 11.5px; font-family: ui-monospace, Menlo, Consolas, monospace; border: 1px solid #FED7AA; }
    .muted { color: var(--muted); font-size: 12px; }

    /* Approach summary ------------------------------------------------- */
    .approach .approach-header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
    .approach .approach-header h2 { margin: 0; color: var(--muted); }
    .approach .approach-sub { color: var(--muted); font-size: 11.5px; }
    .approach-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    @media (max-width: 860px) { .approach-grid { grid-template-columns: 1fr; } }
    .approach-col { background: var(--surface-muted); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
    .approach-col h3 { margin: 0 0 8px; font-size: 11px; color: var(--accent-strong); text-transform: uppercase; letter-spacing: 0.09em; font-weight: 700; }
    .approach-col ul { margin: 0; padding-left: 18px; }
    .approach-col li { font-size: 12.5px; line-height: 1.6; margin: 4px 0; color: var(--text-soft); }
    .approach-col li b { color: var(--text); font-weight: 700; }
    .approach-col .empty { color: var(--muted); font-size: 12px; list-style: none; margin-left: -16px; }
    .approach-dim { color: var(--muted); font-weight: 400; font-size: 11.5px; }
    .approach-howto { margin-top: 14px; border-top: 1px dashed var(--border-strong); padding-top: 10px; }
    .approach-howto summary { color: var(--accent-strong); font-size: 12px; font-weight: 600; }
    .approach-howto ol { margin: 10px 0 0; padding-left: 20px; color: var(--text-soft); }
    .approach-howto ol li { font-size: 12.5px; line-height: 1.65; margin: 3px 0; }
    .approach-howto code { background: var(--code-bg); padding: 1px 6px; border-radius: 5px; color: var(--code-fg); font-size: 11.5px; border: 1px solid #FED7AA; }

    /* Metric chart ----------------------------------------------------- */
    #metricChart { width: 100%; height: 210px; display: block; font-family: ui-sans-serif, -apple-system, "Segoe UI", Inter, Arial, sans-serif; }
    #metricChart .axis { stroke: var(--border); stroke-width: 1; }
    #metricChart .gridlabel { fill: var(--muted); font-size: 10.5px; font-variant-numeric: tabular-nums; }
    #metricChart .pt-label { fill: var(--text-soft); font-size: 11.5px; font-variant-numeric: tabular-nums; text-anchor: middle; }
    #metricChart .pt-label.latest { fill: var(--accent-strong); font-size: 13.5px; font-weight: 700; }

    /* Disclosures & pills --------------------------------------------- */
    details { margin-top: 8px; }
    details summary { cursor: pointer; color: var(--muted); font-size: 12px; user-select: none; padding: 4px 0; font-weight: 500; }
    details summary:hover { color: var(--text); }
    details[open] summary { color: var(--text); margin-bottom: 4px; }
    pre { white-space: pre-wrap; margin: 0; font-size: 11.5px; line-height: 1.5; font-family: ui-monospace, Menlo, Consolas, monospace; overflow-x: auto; background: var(--surface-muted); color: var(--text); border-radius: 8px; padding: 10px 12px; border: 1px solid var(--border); }
    .plus { color: var(--plus); font-weight: 600; }
    .minus { color: var(--minus); font-weight: 600; }
    .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 10.5px; font-weight: 700; letter-spacing: 0.02em; border: 1px solid transparent; }
    .pill.ok { background: var(--plus-bg); color: var(--plus); border-color: #BBF7D0; }
    .pill.fail { background: var(--minus-bg); color: var(--minus); border-color: #FECACA; }
    .pill.info { background: var(--accent-soft); color: var(--accent-strong); border-color: #FED7AA; }
    .empty { color: var(--muted); font-size: 12px; padding: 8px 4px; }
    .trajectory-panel { font-size: 12.5px; color: var(--text-soft); line-height: 1.55; margin-bottom: 12px; padding: 10px 12px; background: var(--surface-muted); border: 1px solid var(--border); border-radius: 10px; }
    .trajectory-panel ul { margin: 6px 0 0; padding-left: 18px; }
    .trajectory-panel strong { color: var(--text); }
    #metricChart circle.selected { stroke: #1C1917; stroke-width: 3px; }
    /* Dot tooltip (model + hyperparams popup) ----------------------- */
    #dot-tooltip {
      display: none;
      position: fixed;
      width: 230px;
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-radius: 10px;
      padding: 10px 12px;
      box-shadow: var(--shadow-md);
      z-index: 200;
      pointer-events: none;
    }
    #dot-tooltip.visible { display: block; }
    .dot-tip-metric { font-size: 20px; font-weight: 800; color: var(--accent-strong); margin-bottom: 4px; font-variant-numeric: tabular-nums; }
    .dot-tip-model { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 1px; }
    .dot-tip-type { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
    .dot-tip-params { margin: 4px 0 0; display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; font-size: 11.5px; }
    .dot-tip-params dt { color: var(--muted); white-space: nowrap; }
    .dot-tip-params dd { margin: 0; color: var(--text-soft); font-family: ui-monospace, Menlo, Consolas, monospace; word-break: break-all; }
    .dot-tip-empty { font-size: 12px; color: var(--muted); }
    /* Cell breakdown in timeline groups ----------------------------- */
    .cell-breakdown { margin: 8px 0 0; }
    .cell-breakdown-title { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 6px; }
    .cell-row { display: flex; gap: 8px; margin: 4px 0; align-items: baseline; flex-wrap: wrap; }
    .cell-role-badge { flex-shrink: 0; padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 700; background: var(--accent-soft); color: var(--accent-strong); border: 1px solid #FED7AA; }
    .cell-summary { font-size: 12.5px; color: var(--text-soft); }
    .cell-why { font-size: 11px; color: var(--muted); font-style: italic; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">&gt;_</div>
      <div class="brand-text">
        <div class="brand-title">MLE Bench Logger<span class="beta">Beta</span></div>
        <div class="brand-sub">AICodingGym<span class="dot">&middot;</span>live supervisor feed</div>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><span class="label">Best accuracy</span><span class="value accent" id="latestMetric">&mdash;</span></div>
      <div class="stat"><span class="label">Updated</span><span class="value" id="latestTime">&mdash;</span></div>
      <div class="stat"><span class="label">Cards</span><span class="value" id="cardCount">0</span></div>
    </div>
  </header>
  <main>
    <section id="approach" class="panel approach">
      <div class="approach-header">
        <h2>Approach summary</h2>
        <span id="approachSelectionLabel" class="approach-sub">Showing latest metric run.</span>
      </div>
      <div id="trajectorySummary" class="trajectory-panel"></div>
      <div id="approachDisplay">
        <div class="empty">Create <code>solution.ipynb</code> and save it &mdash; this panel will auto-summarize your preprocessing, model, and evaluation in plain English.</div>
      </div>
    </section>
    <div class="panel">
      <h2>Metric trend (<span id="metricDirection">higher is better</span>) <span class="approach-dim" style="font-weight:500;text-transform:none;letter-spacing:0;">&middot; oldest on the left, newest on the right &middot; click a dot for what changed</span></h2>
      <svg id="metricChart" viewBox="0 0 1000 210" preserveAspectRatio="xMidYMid meet"></svg>
      <div id="metricNote" class="empty" style="margin-top:6px;">Click a point on the chart to see a one-line summary of what changed to produce it.</div>
      <div id="dot-tooltip" role="tooltip"></div>
    </div>
    <div id="cards"></div>
  </main>
  <script>
    (function () {
      function escapeHtml(s) {
        return String(s)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      }
      function fillTrajectorySummary() {
        const el = document.getElementById("trajectorySummary");
        if (!el) return;
        const metricCards = Array.from(document.querySelectorAll("#cards .card")).filter(function (c) {
          return (
            c.hasAttribute("data-metric") &&
            Number.isFinite(Number(c.getAttribute("data-metric")))
          );
        });
        const chronological = metricCards.slice().reverse();
        if (!chronological.length) {
          el.innerHTML =
            '<span class="approach-dim">No metric runs yet. Save <code>solution.ipynb</code> with <code>VAL_ACC:</code> (or <code>validation_accuracy:</code>) in the output to build a trajectory.</span>';
          return;
        }
        let best = -Infinity;
        const items = [];
        for (let i = 0; i < chronological.length; i++) {
          const c = chronological[i];
          const v = Number(c.getAttribute("data-metric"));
          if (!Number.isFinite(v)) continue;
          if (v > best) {
            best = v;
            const note = (c.getAttribute("data-note") || "").trim();
            const tEl = c.querySelector(".time");
            const ts = tEl ? tEl.textContent.trim() : "";
            const prose =
              note ||
              "Notebook run set a new best validation accuracy (" + String(v) + ").";
            items.push(
              "<li><span class=\"approach-dim\">" +
                escapeHtml(ts) +
                "</span> &mdash; " +
                escapeHtml(prose) +
                ' <span class="approach-dim">(' +
                escapeHtml(String(v)) +
                ")</span></li>"
            );
          }
        }
        const cap = 12;
        let tailNote = "";
        if (items.length > cap) {
          tailNote =
            '<p class="approach-dim" style="margin:8px 0 0">Earlier improvements omitted.</p>';
          while (items.length > cap) items.shift();
        }
        if (!items.length) {
          el.innerHTML =
            '<span class="approach-dim">No new bests yet &mdash; validation accuracy has not strictly increased since the first logged run.</span>';
          return;
        }
        el.innerHTML =
          "<strong>Trajectory summary</strong> &mdash; each time validation accuracy reached a new high, the change that preceded it was:<ul>" +
          items.join("") +
          "</ul>" +
          tailNote;
      }

      const cards = Array.from(document.querySelectorAll("#cards .card"));
      document.getElementById("cardCount").textContent = cards.length;
      fillTrajectorySummary();

      const approachDisplay = document.getElementById("approachDisplay");
      const approachLabel = document.getElementById("approachSelectionLabel");
      const latestApproachHtml = approachDisplay ? approachDisplay.innerHTML : "";
      const latestLabelText = approachLabel ? approachLabel.textContent : "";

      const metricCardsDom = cards.filter(function (c) {
        return (
          c.hasAttribute("data-metric") &&
          Number.isFinite(Number(c.getAttribute("data-metric")))
        );
      });
      const metricCards = metricCardsDom.slice().reverse();
      const values = metricCards.map(function (c) {
        return Number(c.getAttribute("data-metric"));
      });
      const notes = metricCards.map(function (c) {
        return c.getAttribute("data-note") || "";
      });
      const times = metricCards.map(function (c) {
        const t = c.querySelector(".time");
        return t ? t.textContent : "";
      });
      const snaps = metricCards.map(function (c) {
        const sc = c.querySelector("script.approach-snap-data");
        return sc && sc.textContent ? sc.textContent : "";
      });

      const svg = document.getElementById("metricChart");
      const latestEl = document.getElementById("latestMetric");
      const latestTimeEl = document.getElementById("latestTime");
      const noteEl = document.getElementById("metricNote");

      if (cards.length) {
        const t = cards[0].querySelector(".time");
        if (t) latestTimeEl.textContent = t.textContent;
      }

      const fmt = function (v) {
        const a = Math.abs(v);
        if (a >= 100) return v.toFixed(1);
        if (a >= 1) return v.toFixed(3);
        return v.toFixed(4);
      };

      const latestIdx = function () {
        return values.length ? values.length - 1 : -1;
      };

      function hideDotTooltip() {
        var t = document.getElementById('dot-tooltip');
        if (t) t.classList.remove('visible');
      }
      function showDotTooltip(circleEl, metricVal, modelJsonStr) {
        var t = document.getElementById('dot-tooltip');
        if (!t) return;
        var html = '<div class="dot-tip-metric">' + escapeHtml(fmt(metricVal)) + '</div>';
        var hasModel = false;
        if (modelJsonStr) {
          try {
            var m = JSON.parse(modelJsonStr);
            if (m && typeof m === 'object') {
              if (m.name) { html += '<div class="dot-tip-model">' + escapeHtml(m.name) + '</div>'; hasModel = true; }
              if (m.type) { html += '<div class="dot-tip-type">' + escapeHtml(m.type) + '</div>'; }
              var hp = m.hyperparams || m.hyperparameters;
              if (hp && typeof hp === 'object' && Object.keys(hp).length) {
                html += '<dl class="dot-tip-params">';
                Object.keys(hp).forEach(function(k) {
                  html += '<dt>' + escapeHtml(String(k)) + '</dt><dd>' + escapeHtml(String(hp[k])) + '</dd>';
                });
                html += '</dl>';
              }
            }
          } catch (_) {}
        }
        if (!hasModel) {
          html += '<div class="dot-tip-empty">No model info — add <code>model</code> to .agent_note.json</div>';
        }
        t.innerHTML = html;
        t.classList.add('visible');
        var r = circleEl.getBoundingClientRect();
        var tw = 230;
        var cx = r.left + r.width / 2;
        var left = cx - tw / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
        t.style.left = left + 'px';
        var tipH = t.offsetHeight || 90;
        var topAbove = r.top - tipH - 10;
        t.style.top = (topAbove >= 8 ? topAbove : r.bottom + 10) + 'px';
      }
      document.addEventListener('click', function(e) {
        if (!e.target.closest || (!e.target.closest('#dot-tooltip') && !e.target.closest('#metricChart'))) {
          hideDotTooltip();
        }
      });
      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') hideDotTooltip();
      });

      function setSelectedDot(iSel) {
        if (!svg) return;
        svg.querySelectorAll("circle[data-i]").forEach(function (c) {
          c.classList.remove("selected");
          const di = Number(c.getAttribute("data-i"));
          if (di === iSel) c.classList.add("selected");
        });
      }

      function applyApproachForIndex(i) {
        if (!approachDisplay) return;
        const li = latestIdx();
        if (i === li) {
          approachDisplay.innerHTML = latestApproachHtml;
          if (approachLabel) approachLabel.textContent = latestLabelText || "Showing latest metric run.";
          return;
        }
        const snap = snaps[i] || "";
        if (snap.trim()) {
          approachDisplay.innerHTML = snap;
        } else {
          approachDisplay.innerHTML =
            '<p class="empty">Approach snapshot was not stored for this run (older dashboard or summarize skipped). Use the change note under the chart.</p>';
        }
        if (approachLabel) {
          approachLabel.textContent =
            "Showing run #" +
            (i + 1) +
            (times[i] ? " \u00b7 " + times[i] : "") +
            " (as of that metric).";
        }
      }

      const showNote = function (i) {
        if (!noteEl) return;
        const note =
          notes[i] ||
          (i === 0
            ? "First recorded run \u2014 see the Approach summary for the pipeline at that point."
            : "No change note was captured for this run.");
        noteEl.innerHTML =
          "<b>#" +
          (i + 1) +
          "</b> &middot; " +
          fmt(values[i]) +
          (times[i] ? ' &middot; <span style="color:var(--muted)">' + times[i] + "</span>" : "") +
          "<br>" +
          note;
      };

      function selectDot(i) {
        if (i < 0 || i >= values.length) return;
        showNote(i);
        applyApproachForIndex(i);
        setSelectedDot(i);
        var circle = svg ? svg.querySelector('circle[data-i="' + i + '"]') : null;
        var modelJsonStr = metricCards[i] ? (metricCards[i].getAttribute('data-model-json') || '') : '';
        if (circle) {
          showDotTooltip(circle, values[i], modelJsonStr);
        } else {
          hideDotTooltip();
        }
      }

      if (!values.length) {
        if (svg) {
          svg.innerHTML =
            '<text x="20" y="30" fill="#78716C" font-size="12">No metric values yet. Add a VAL_ACC: line to your notebook.</text>';
        }
        return;
      }

      latestEl.textContent = fmt(Math.max.apply(null, values));

      const w = 1000,
        h = 210,
        padL = 60,
        padR = 64,
        padT = 24,
        padB = 30;
      const min = Math.min.apply(null, values),
        max = Math.max.apply(null, values);
      const span = max - min || Math.max(1e-9, Math.abs(max) * 0.01);
      const xOf = function (i) {
        return padL + (i * (w - padL - padR)) / Math.max(values.length - 1, 1);
      };
      const yOf = function (v) {
        return h - padB - ((v - min) / span) * (h - padT - padB);
      };
      const STROKE = "#F97316";

      if (values.length === 1) {
        const v = values[0];
        svg.innerHTML =
          '<line class="axis" x1="' +
          padL +
          '" y1="' +
          (h - padB) +
          '" x2="' +
          (w - padR) +
          '" y2="' +
          (h - padB) +
          '" />' +
          '<circle cx="' +
          w / 2 +
          '" cy="' +
          h / 2 +
          '" r="6" fill="' +
          STROKE +
          '" stroke="#FFFFFF" stroke-width="2.25" style="cursor:pointer" class="selected" data-i="0" />' +
          '<text class="pt-label latest" x="' +
          w / 2 +
          '" y="' +
          (h / 2 - 16) +
          '" style="font-weight:800">' +
          fmt(v) +
          "</text>" +
          '<text class="gridlabel" x="' +
          padL +
          '" y="' +
          (h - 8) +
          '" text-anchor="start">single observation</text>';
        svg.querySelectorAll("circle[data-i]").forEach(function (c) {
          c.addEventListener("click", function () {
            selectDot(Number(c.getAttribute("data-i")));
          });
        });
        selectDot(0);
        return;
      }

      const gridLines = [min, (min + max) / 2, max];
      let html = "";
      for (let gi = 0; gi < gridLines.length; gi++) {
        const g = gridLines[gi];
        const yy = yOf(g);
        html +=
          '<line class="axis" x1="' +
          padL +
          '" y1="' +
          yy +
          '" x2="' +
          (w - padR) +
          '" y2="' +
          yy +
          '" stroke-dasharray="3 4" />' +
          '<text class="gridlabel" x="' +
          (padL - 8) +
          '" y="' +
          (yy + 3) +
          '" text-anchor="end">' +
          fmt(g) +
          "</text>";
      }
      const pts = values.map(function (v, i) {
        return xOf(i) + "," + yOf(v);
      });
      html +=
        '<polyline fill="none" stroke="' +
        STROKE +
        '" stroke-width="2.75" stroke-linejoin="round" stroke-linecap="round" points="' +
        pts.join(" ") +
        '" />';
      values.forEach(function (v, i) {
        const x = xOf(i),
          y = yOf(v);
        const isLatest = i === values.length - 1;
        html +=
          '<circle cx="' +
          x +
          '" cy="' +
          y +
          '" r="' +
          (isLatest ? 6.5 : 3.25) +
          '" fill="' +
          STROKE +
          '" stroke="#FFFFFF" stroke-width="' +
          (isLatest ? 2.25 : 1.5) +
          '" style="cursor:pointer" data-i="' +
          i +
          '"><title>Click for approach at this run</title></circle>';
        const aboveOK = y - padT > 20;
        const labelY = aboveOK ? y - 10 : y + 18;
        const weight = isLatest ? ' style="font-weight:800"' : "";
        html +=
          '<text class="pt-label' +
          (isLatest ? " latest" : "") +
          '"' +
          weight +
          ' x="' +
          x +
          '" y="' +
          labelY +
          '">' +
          fmt(v) +
          "</text>";
      });
      svg.innerHTML = html;
      svg.querySelectorAll("circle[data-i]").forEach(function (c) {
        c.addEventListener("click", function () {
          selectDot(Number(c.getAttribute("data-i")));
        });
      });
      selectDot(values.length - 1);

      // ── renderCardEnhancements: prompt-group timeline with cell breakdown ──
      function renderCardEnhancements() {
        var allCards = Array.from(document.querySelectorAll('#cards .card'));
        if (!allCards.length) return;

        // Group cards by data-prompt-key (fall back to individual cards)
        var groupMap = {};
        var groupOrder = [];
        allCards.forEach(function(card) {
          var key = card.getAttribute('data-prompt-key') || ('__solo__' + groupOrder.length);
          if (!groupMap[key]) {
            groupMap[key] = { key: key, cards: [], files: [], notes: [] };
            groupOrder.push(key);
          }
          var g = groupMap[key];
          g.cards.push(card);
          var note = card.getAttribute('data-note') || '';
          if (note) g.notes.push(note);
          var changeAttr = card.getAttribute('data-change') || '';
          if (changeAttr) {
            try {
              var chg = JSON.parse(changeAttr);
              (chg.files || []).forEach(function(f) {
                if (f.path && g.files.indexOf(f.path) === -1) g.files.push(f.path);
              });
            } catch (_) {}
          }
        });

        // Build timeline container
        var cardsEl = document.getElementById('cards');
        if (!cardsEl) return;
        var timelineEl = document.createElement('div');
        timelineEl.id = 'prompt-timeline';

        groupOrder.forEach(function(key) {
          var group = groupMap[key];
          if (!group.cards.length) return;

          // Files HTML
          var filesHtml = '';
          group.files.slice(0, 12).forEach(function(f) {
            filesHtml += '<li><code>' + escapeHtml(f) + '</code></li>';
          });
          if (!filesHtml) {
            group.cards.forEach(function(card) {
              var h3 = card.querySelector('h3');
              if (h3 && !filesHtml) filesHtml += '<li>' + escapeHtml(h3.textContent || '') + '</li>';
            });
          }

          // Summary line from first card with data-ai-summary
          var summaryLine = '';
          group.cards.forEach(function(card) {
            if (summaryLine) return;
            summaryLine = card.getAttribute('data-ai-summary') || '';
          });

          // Cell breakdown from first card with data-notebook-analysis
          let cellsHtml = '';
          group.cards.forEach(function(card) {
            if (cellsHtml) return;
            var nbData = card.getAttribute('data-notebook-analysis') || '';
            if (!nbData) return;
            try {
              var nb = JSON.parse(nbData);
              var cells = (nb && nb.cells) ? nb.cells : (Array.isArray(nb) ? nb : []);
              if (!cells.length) return;
              var rows = '';
              cells.forEach(function(cell) {
                var role = String(cell.role || 'cell').replace(/-/g, ' ');
                var summary = escapeHtml(String(cell.summary || ''));
                var why = escapeHtml(String(cell.why || ''));
                rows += '<div class="cell-row">'
                  + '<span class="cell-role-badge">' + escapeHtml(role) + '</span>'
                  + '<span class="cell-summary">' + summary
                  + (why ? ' <span class="cell-why">— ' + why + '</span>' : '')
                  + '</span></div>';
              });
              cellsHtml = '<div class="cell-breakdown"><p class="cell-breakdown-title">Notebook Cells (agent analysis)</p>' + rows + '</div>';
            } catch (_) {}
          });

          var bodyBits = [
            '<p class="prompt-block-title">All Changes</p><ul class="mini-list">' + (filesHtml || '<li class="muted">No files recorded</li>') + '</ul>'
          ];
          if (summaryLine) {
            bodyBits.unshift('<p style="font-size:13px;color:var(--text-soft);margin:0 0 6px;">' + escapeHtml(summaryLine) + '</p>');
          }
          if (cellsHtml) bodyBits.push(cellsHtml);

          // Attach cell breakdown to the first card in the group in the DOM
          var firstCard = group.cards[0];
          if (cellsHtml && !firstCard.querySelector('.cell-breakdown')) {
            var cbDiv = document.createElement('div');
            cbDiv.innerHTML = cellsHtml;
            firstCard.appendChild(cbDiv.firstChild);
          }
        });
      }
      renderCardEnhancements();
    })();
  </script>
</body>
</html>

EOF
}

append_card() {
  # Args: title, meta_html, body_html, metric_value (optional), note (optional),
  # approach_snap_path (optional), change_json (optional), ai_summary (optional),
  # ai_source (optional), ai_status (optional), why (optional), stage (optional),
  # impact (optional: low|medium|high), prompt (optional), next_prompt (optional),
  # prompt_key (optional), prompt_seq (optional)
  local title="$1"
  local meta="$2"
  local body_html="$3"
  local metric="${4:-}"
  local note="${5:-}"
  local snap_path="${6:-}"
  local change_json="${7:-}"
  local ai_summary="${8:-}"
  local ai_source="${9:-}"
  local ai_status="${10:-}"
  local why="${11:-}"
  local stage="${12:-}"
  local impact="${13:-}"
  local prompt="${14:-}"
  local next_prompt="${15:-}"
  local prompt_key="${16:-}"
  local prompt_seq="${17:-}"
  local model_json="${18:-}"
  local nb_analysis_json="${19:-}"
  local temp_file
  temp_file="$(mktemp)"
  APPEND_CARD_MODEL_JSON="$model_json" \
  APPEND_CARD_NB_ANALYSIS="$nb_analysis_json" \
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$DASHBOARD_PATH" "$title" "$meta" "$metric" "$note" "$snap_path" "$change_json" "$ai_summary" "$ai_source" "$ai_status" "$why" "$stage" "$impact" "$prompt" "$next_prompt" "$prompt_key" "$prompt_seq" <<'PY' "$body_html" >"$temp_file"
import re, sys, pathlib, datetime, html as _h

dash_path, title, meta, metric, note, snap_path, change_json, ai_summary, ai_source, ai_status, why, stage, impact, prompt, next_prompt, prompt_key, prompt_seq, body = sys.argv[1:19]
import os
model_json = os.environ.get('APPEND_CARD_MODEL_JSON', '')
nb_analysis_json = os.environ.get('APPEND_CARD_NB_ANALYSIS', '')


def _extract_approach_snap(text: str) -> str:
    m = re.search(
        r"<!--BEGIN_APPROACH_DISPLAY-->(.*)<!--END_APPROACH_DISPLAY-->",
        text,
        flags=re.DOTALL,
    )
    return m.group(1).strip() if m else ""


snap_inner = ""
if snap_path:
    p = pathlib.Path(snap_path)
    if p.is_file() and p.stat().st_size > 0:
        frag = p.read_text(encoding="utf-8")
        snap_inner = _extract_approach_snap(frag)
        if len(snap_inner) > 400000:
            snap_inner = snap_inner[:400_000] + "\n<!-- truncated -->"

src = pathlib.Path(dash_path).read_text(encoding="utf-8")
anchor = '<div id="cards">'
idx = src.find(anchor)
out = src
if idx != -1:
    insert_at = idx + len(anchor)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    attrs = ''
    if metric:
        attrs += f' data-metric="{metric}"'
    if note:
        attrs += f' data-note="{_h.escape(note, quote=True)}"'
    if change_json:
        attrs += f' data-change="{_h.escape(change_json, quote=True)}"'
    if ai_summary:
        attrs += f' data-ai-summary="{_h.escape(ai_summary, quote=True)}"'
    if ai_source:
        attrs += f' data-ai-source="{_h.escape(ai_source, quote=True)}"'
    if ai_status:
        attrs += f' data-ai-status="{_h.escape(ai_status, quote=True)}"'
    if why:
        attrs += f' data-why="{_h.escape(why, quote=True)}"'
    if stage:
        attrs += f' data-stage="{_h.escape(stage, quote=True)}"'
    if impact:
        attrs += f' data-impact="{_h.escape(impact, quote=True)}"'
    if prompt:
        attrs += f' data-user-prompt="{_h.escape(prompt, quote=True)}"'
    if next_prompt:
        attrs += f' data-next-prompt="{_h.escape(next_prompt, quote=True)}"'
    if prompt_key:
        attrs += f' data-prompt-key="{_h.escape(prompt_key, quote=True)}"'
    if prompt_seq:
        attrs += f' data-prompt-seq="{_h.escape(prompt_seq, quote=True)}"'
    if model_json:
        attrs += f' data-model-json="{_h.escape(model_json, quote=True)}"'
    if nb_analysis_json:
        attrs += f' data-notebook-analysis="{_h.escape(nb_analysis_json, quote=True)}"'
    card_lines = [f'\n      <div class="card"{attrs}>']
    card_lines.append(f'        <div class="row"><h3>{title}</h3><span class="time">{ts}</span></div>')
    if meta:
        card_lines.append(f'        <div class="meta">{meta}</div>')
    card_lines.append(body)
    if snap_inner:
        esc = snap_inner.replace("</script", "<\\/script")
        card_lines.append(
            '        <script type="text/plain" class="approach-snap-data">'
            + esc
            + "</script>"
        )
    card_lines.append('      </div>')
    out = src[:insert_at] + "\n".join(card_lines) + src[insert_at:]
# Always force utf-8 bytes to stdout so non-ASCII chars survive round-tripping on Windows.
sys.stdout.buffer.write(out.encode("utf-8"))
PY
  mv "$temp_file" "$DASHBOARD_PATH"
}

snapshot_workspace() {
  rm -rf "$SNAPSHOT_DIR"
  mkdir -p "$SNAPSHOT_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude ".git" \
      --exclude ".supervisor_snapshot" \
      --exclude ".supervisor.lock" \
      --exclude ".supervisor_prev_notebook.ipynb" \
      --exclude "dashboard.html" \
      --exclude ".prompt" \
      "$ROOT_DIR/" "$SNAPSHOT_DIR/"
    return
  fi
  # Portable fallback when rsync is missing (e.g. Git Bash on Windows).
  (
    cd "$ROOT_DIR"
    find . \
      -path ./.git -prune -o \
      -path ./.supervisor_snapshot -prune -o \
      -name .supervisor.lock -prune -o \
      -name .supervisor_prev_notebook.ipynb -prune -o \
      -name dashboard.html -prune -o \
      -name .prompt -prune -o \
      -print0 |
      while IFS= read -r -d '' path; do
        [[ "$path" == "." ]] && continue
        dest="$SNAPSHOT_DIR/${path#./}"
        if [[ -d "$path" ]]; then
          mkdir -p "$dest"
        else
          mkdir -p "$(dirname "$dest")"
          cp -a "$path" "$dest"
        fi
      done
  )
}

# Builds a compact list of changed files and an optional collapsed <details>
# with the full diff. Notebooks and binaries get summarized, not dumped.
# Output layout: first line is ``TITLE=<human-friendly title>``; remaining lines
# are the HTML body to splice into a card. ``render_change_title`` reads that
# first line and strips it off so callers can use it as the card title.
render_change_card_body() {
  local body_file
  body_file="$(mktemp)"
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$SNAPSHOT_DIR" "$ROOT_DIR" "$MAX_DIFF_LINES_PER_FILE" >"$body_file" <<'PY'
import difflib, html, json, os, re, sys, pathlib, filecmp

snap, root, max_lines = sys.argv[1], sys.argv[2], int(sys.argv[3])
SKIP_DIRS = {".git", ".supervisor_snapshot", "__pycache__"}
SKIP_NAMES = {".supervisor.lock", ".supervisor_prev_notebook.ipynb", "dashboard.html", ".prompt"}
BINARY_SUFFIXES = {".zip", ".gz", ".tar", ".pkl", ".joblib", ".npy", ".npz", ".parquet", ".pt", ".pth", ".bin", ".onnx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".csv", ".xls", ".xlsx"}

def impact_bucket_for(rel):
    r = rel.replace(os.sep, "/").lower()
    if r.startswith("data/") or "/data/" in r:
        return "data"
    if r.endswith("solution.ipynb"):
        return "notebook_pipeline"
    base = pathlib.PurePosixPath(r).name
    if base in ("submission.csv", "predictions.csv") or "submission" in base or base.startswith("predictions"):
        return "submission"
    if r.endswith(".py"):
        return "code"
    if r.endswith(".md") and "description" in r:
        return "docs"
    if pathlib.PurePosixPath(r).suffix.lower() in (".csv", ".parquet", ".feather"):
        return "tabular_artifact"
    return "other"


def facets_and_boost_from_diff(diff_lines):
    """From unified diff lines, infer notebook/code change facets and a short boosting hint."""
    added = "\n".join(ln[1:] for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    low = added.lower()
    facets = []
    pre = (
        "fillna", "standardscaler", "minmaxscaler", "onehotencoder", "ordinalencoder",
        "simpleimputer", "tfidf", "countvectorizer", "hashingvectorizer", "get_dummies",
        "robustscaler", "powertransformer", "truncatedsvd", "pca", "knnimputer",
        "columntransformer", "targetencoder", "labelencoder",
    )
    if any(p in low for p in pre):
        facets.append("preprocessing")
    model = (
        "lgbm", "lightgbm", "xgbclassifier", "xgbregressor", "catboost",
        ".fit(", "histgradientboosting", "randomforest", "logisticregression",
        "gradientboosting", "extratrees", "mlpclassifier", "svc(", "kneighbors",
    )
    if any(p in low for p in model):
        facets.append("model training")
    if any(p in low for p in ("to_csv", "submission", "predictions")):
        facets.append("prediction export")
    if any(p in low for p in ("val_acc", "validation_accuracy", "accuracy_score", "roc_auc", "cross_val", "f1_score")):
        facets.append("evaluation")

    boost = ""
    m = re.search(
        r"(?:LGBM\w+|XGB\w+|CatBoost\w+|lgb\.train)\s*\(\s*([^)]{0,900})",
        added,
        re.I | re.DOTALL,
    )
    if m:
        inner = re.sub(r"\s+", " ", m.group(0).strip())
        boost = inner[:280] + ("..." if len(inner) > 280 else "")
    else:
        params = []
        for pat, label in (
            (r"num_leaves\s*=\s*\d+", "num_leaves"),
            (r"learning_rate\s*=\s*[\d.eE+-]+", "learning_rate"),
            (r"n_estimators\s*=\s*\d+", "n_estimators"),
            (r"max_depth\s*=\s*[\d-]+", "max_depth"),
            (r"subsample\s*=\s*[\d.]+", "subsample"),
            (r"colsample_bytree\s*=\s*[\d.]+", "colsample_bytree"),
        ):
            mm = re.search(pat, added, re.I)
            if mm:
                params.append(mm.group(0).replace(" ", ""))
        if params:
            boost = "Boosting params touched: " + ", ".join(params[:6])

    out_f = []
    seen = set()
    for f in facets:
        if f not in seen:
            seen.add(f)
            out_f.append(f)
    return out_f, boost


def walk(base):
    files = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            try:
                files[rel] = os.path.getsize(full)
            except OSError:
                pass
    return files

snap_files = walk(snap) if os.path.isdir(snap) else {}
root_files = walk(root)

def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines(keepends=False)
    except (OSError, UnicodeDecodeError):
        return None

def classify(rel):
    suf = pathlib.PurePosixPath(rel).suffix.lower()
    if suf == ".ipynb":
        return "notebook"
    if suf in BINARY_SUFFIXES:
        return "binary"
    return "text"

change_rows = []  # dicts: status, rel, kind, added, removed, diff_html, impact_bucket, facets, boost_snippet
all_rels = sorted(set(snap_files) | set(root_files))
for rel in all_rels:
    in_snap = rel in snap_files
    in_root = rel in root_files
    snap_path = os.path.join(snap, rel)
    root_path = os.path.join(root, rel)
    if in_snap and in_root:
        # Quick shallow check.
        try:
            if filecmp.cmp(snap_path, root_path, shallow=False):
                continue
        except OSError:
            pass
        status = "modified"
    elif in_root:
        status = "added"
    else:
        status = "removed"

    kind = classify(rel)
    added = removed = 0
    diff_html = ""
    facets = []
    boost_snippet = ""
    ibucket = impact_bucket_for(rel)

    if kind == "notebook":
        # Diff only the cell source text (code + markdown). Skipping execution
        # counts, outputs, and base64 blobs gives an accurate +/- line count and
        # a readable diff instead of a meaningless "size X -> Y" line.
        def _notebook_sources(path):
            import json as _json
            try:
                data = _json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
            except Exception:
                return None
            out = []
            for idx, cell in enumerate(data.get("cells", [])):
                ctype = cell.get("cell_type", "?")
                src = cell.get("source", [])
                if isinstance(src, list):
                    src = "".join(src)
                out.append(f"# --- cell {idx} ({ctype}) ---")
                out.extend(src.splitlines() or [""])
            return out
        a_src = _notebook_sources(snap_path) if in_snap else []
        b_src = _notebook_sources(root_path) if in_root else []
        if a_src is None or b_src is None:
            s1 = snap_files.get(rel, 0)
            s2 = root_files.get(rel, 0)
            diff_html = f'<pre>notebook {status} (unparseable JSON; size {s1} \u2192 {s2} bytes)</pre>'
        else:
            diff_lines = list(difflib.unified_diff(a_src, b_src, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
            facets, boost_snippet = facets_and_boost_from_diff(diff_lines)
            for ln in diff_lines:
                if ln.startswith("+") and not ln.startswith("+++"):
                    added += 1
                elif ln.startswith("-") and not ln.startswith("---"):
                    removed += 1
            if len(diff_lines) > max_lines:
                truncated = diff_lines[:max_lines]
                truncated.append(f"... ({len(diff_lines) - max_lines} more lines)")
                diff_lines = truncated
            rendered = []
            for ln in diff_lines:
                esc = html.escape(ln)
                if ln.startswith("+") and not ln.startswith("+++"):
                    rendered.append(f'<span class="plus">{esc}</span>')
                elif ln.startswith("-") and not ln.startswith("---"):
                    rendered.append(f'<span class="minus">{esc}</span>')
                else:
                    rendered.append(esc)
            body = "\n".join(rendered) if rendered else "(notebook source unchanged; only outputs/metadata differ)"
            diff_html = f'<pre>{body}</pre>'
    elif kind == "binary":
        s1 = snap_files.get(rel, 0)
        s2 = root_files.get(rel, 0)
        diff_html = f'<pre>binary {status} (size {s1} \u2192 {s2} bytes)</pre>'
    else:
        a = read_text(snap_path) if in_snap else []
        b = read_text(root_path) if in_root else []
        if a is None or b is None:
            diff_html = f'<pre>{status} (not UTF-8 readable)</pre>'
        else:
            diff_lines = list(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
            facets, boost_snippet = facets_and_boost_from_diff(diff_lines)
            for ln in diff_lines:
                if ln.startswith("+") and not ln.startswith("+++"):
                    added += 1
                elif ln.startswith("-") and not ln.startswith("---"):
                    removed += 1
            if len(diff_lines) > max_lines:
                truncated = diff_lines[:max_lines]
                truncated.append(f"... ({len(diff_lines) - max_lines} more lines)")
                diff_lines = truncated
            rendered = []
            for ln in diff_lines:
                esc = html.escape(ln)
                if ln.startswith("+") and not ln.startswith("+++"):
                    rendered.append(f'<span class="plus">{esc}</span>')
                elif ln.startswith("-") and not ln.startswith("---"):
                    rendered.append(f'<span class="minus">{esc}</span>')
                else:
                    rendered.append(esc)
            body = "\n".join(rendered) if rendered else "(no textual diff)"
            diff_html = f'<pre>{body}</pre>'

    change_rows.append({
        "status": status,
        "rel": rel,
        "kind": kind,
        "added": added,
        "removed": removed,
        "diff_html": diff_html,
        "impact_bucket": ibucket,
        "facets": facets,
        "boost_snippet": boost_snippet,
    })

changes = [(r["status"], r["rel"], r["added"], r["removed"], r["diff_html"]) for r in change_rows]

if not change_rows:
    print('TITLE=No changes')
    print('        <div class="empty">No file changes detected.</div>')
    sys.exit(0)

# Group by status, preserving order of discovery.
added_names = [c[1] for c in changes if c[0] == "added"]
modified_names = [c[1] for c in changes if c[0] == "modified"]
removed_names = [c[1] for c in changes if c[0] == "removed"]
total_added = sum(c[2] for c in changes)
total_removed = sum(c[3] for c in changes)

def _fmt_names(names, limit=3):
    if not names:
        return ""
    shown = [f'<code>{html.escape(n)}</code>' for n in names[:limit]]
    extra = len(names) - len(shown)
    if extra > 0:
        shown.append(f'<span class="muted">+{extra} more</span>')
    return ", ".join(shown)

parts = []
if added_names:
    parts.append(f'<span class="plus">added</span> {_fmt_names(added_names)}')
if modified_names:
    parts.append(f'<span class="pill info">edited</span> {_fmt_names(modified_names)}')
if removed_names:
    parts.append(f'<span class="minus">removed</span> {_fmt_names(removed_names)}')
parts.append(f'<span class="plus">+{total_added}</span> / <span class="minus">-{total_removed}</span>')
summary = " \u00b7 ".join(parts)

# Union of facets / buckets for subtitles
_all_facets = []
_bucket_order = []
for _r in change_rows:
    for _f in _r.get("facets") or []:
        if _f not in _all_facets:
            _all_facets.append(_f)
    _b = _r.get("impact_bucket", "other")
    if _b not in _bucket_order:
        _bucket_order.append(_b)
_boost_hints = [_r["boost_snippet"] for _r in change_rows if _r.get("boost_snippet")]

# Human-friendly title for the card. Single-file actions get a specific verb.
def _title():
    n = len(change_rows)
    if n == 1:
        row = change_rows[0]
        st, rel = row["status"], row["rel"]
        verb = {"added": "Added", "removed": "Removed", "modified": "Edited"}[st]
        if rel.endswith(".ipynb") and st == "modified" and row.get("facets"):
            return "Notebook: " + " + ".join(row["facets"])
        bk = row["impact_bucket"]
        if bk == "submission" and st == "added":
            return f"Added submission artifact ({rel})"
        if bk == "data" and st == "added":
            return f"Added data ({rel})"
        if bk == "notebook_pipeline" and st == "added":
            return f"Added {rel}"
        return f'{verb} {rel}'
    if added_names and not modified_names and not removed_names:
        if all(impact_bucket_for(x) == "data" for x in added_names):
            return f'Added {len(added_names)} data file{"s" if len(added_names) != 1 else ""}'
        return f'Added {len(added_names)} file{"s" if len(added_names)!=1 else ""}'
    if modified_names and not added_names and not removed_names:
        first = modified_names[0]
        extra = len(modified_names) - 1
        if first.endswith(".ipynb") and _all_facets:
            base = "Notebook: " + " + ".join(_all_facets)
            return base + (f' (+{extra} more files)' if extra else '')
        return f'Edited {first}' + (f' + {extra} more' if extra else '')
    if _all_facets and any(r["rel"].endswith(".ipynb") for r in change_rows):
        tail = " + ".join(_all_facets[:4])
        if len(_all_facets) > 4:
            tail += ", ..."
        return f"Multi-file notebook-related change: {tail}"
    return f'Changed {n} files'

# ── Per-cell notebook analysis ───────────────────────────────────────────────
_CELL_PURPOSE_MAP = [
    (["!pip","!conda","import ","from "],                                      "setup",         "info"),
    (["read_csv","read_parquet","pd.read","load_data","dataset"],              "data loading",  "info"),
    (["fillna","dropna","standardscaler","minmaxscaler","onehotencoder",
      "labelencoder","get_dummies","tfidf","countvectorizer","simpleimputer",
      "robustscaler","columntransformer","targetencoder","powertransformer"],  "preprocessing", "ok"),
    (["lgbm","lightgbm","xgbclassifier","xgbregressor","catboost",
      "randomforestclassifier","randomforestregressor","histgradientboosting",
      "logisticregression","gradientboosting","mlpclassifier","svc(",
      "kneighborsclassifier","extratrees","decisiontree"],                     "model",         "ok"),
    ([".fit(","model.train","lgb.train","xgb.train","cross_val","kfold",
      "stratifiedkfold"],                                                       "training",      "ok"),
    (["val_acc","validation_accuracy","accuracy_score","roc_auc",
      "f1_score","mean_squared_error","print(f\"val","print(\"val"],           "evaluation",    "info"),
    (["to_csv","submission","predict(","predict_proba("],                      "prediction",    "info"),
]

def _cell_purpose(src):
    low = src.lower()
    for kws, purpose, pill_cls in _CELL_PURPOSE_MAP:
        if any(k in low for k in kws):
            return purpose, pill_cls
    return "code", "info"

def _cell_detail(src):
    low = src.lower()
    mm = re.search(
        r"(LGBM\w+|XGB\w+|CatBoost\w+|RandomForest\w+|HistGradientBoosting\w+|"
        r"LogisticRegression|GradientBoosting\w+|MLPClassifier|SVC\b|SVR\b|"
        r"KNeighbors\w+|ExtraTrees\w+|DecisionTree\w+)", src, re.IGNORECASE)
    parts = [mm.group(1)] if mm else []
    for pat, lbl in [
        (r"n_estimators\s*=\s*(\d+)",        "n_estimators"),
        (r"learning_rate\s*=\s*([\d.eE+-]+)","lr"),
        (r"max_depth\s*=\s*([\d-]+)",        "max_depth"),
        (r"num_leaves\s*=\s*(\d+)",          "num_leaves"),
        (r"subsample\s*=\s*([\d.]+)",        "subsample"),
        (r"colsample_bytree\s*=\s*([\d.]+)", "colsample_bytree"),
        (r"min_child_samples\s*=\s*(\d+)",   "min_child_samples"),
    ]:
        m2 = re.search(pat, src, re.IGNORECASE)
        if m2:
            parts.append(f"{lbl}={m2.group(1)}")
    if not parts:
        steps = []
        if "fillna" in low: steps.append("fill NA")
        if any(p in low for p in ["standardscaler","minmaxscaler","robustscaler"]): steps.append("scale")
        if any(p in low for p in ["onehotencoder","get_dummies","ordinalencoder","labelencoder"]): steps.append("encode cats")
        if any(p in low for p in ["tfidf","countvectorizer","hashingvectorizer"]): steps.append("vectorize text")
        if "imputer" in low: steps.append("impute")
        parts = steps
    if not parts:
        libs = list(dict.fromkeys(re.findall(r"(?:import|from)\s+(\w+)", src)))
        if libs: parts = [", ".join(libs[:5])]
    return " | ".join(parts[:6])

def _analyze_nb_cells(nb_path):
    try:
        data = json.loads(pathlib.Path(nb_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for idx, cell in enumerate(data.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", [])
        if isinstance(src, list): src = "".join(src)
        if not src.strip(): continue
        purpose, pill_cls = _cell_purpose(src)
        detail = _cell_detail(src)
        out.append((idx, purpose, pill_cls, detail))
    return out

is_pure_add    = bool(added_names)    and not modified_names and not removed_names
is_pure_remove = bool(removed_names)  and not modified_names and not added_names

# The shell caller reads the TITLE= line and strips it before inserting the rest as body.
print(f'TITLE={_title()}')
change_payload = {
    "title": _title(),
    "counts": {"added": len(added_names), "modified": len(modified_names), "removed": len(removed_names)},
    "line_churn": {"added": total_added, "removed": total_removed},
    "impact": {
        "buckets": _bucket_order,
        "notebook_facets": _all_facets,
        "boosting_hint": (_boost_hints[0] if _boost_hints else ""),
    },
    "files": [
        {
            "status": r["status"],
            "path": r["rel"],
            "added_lines": r["added"],
            "removed_lines": r["removed"],
            "impact_bucket": r["impact_bucket"],
            "facets": r["facets"],
        }
        for r in change_rows
    ],
}
print(f'CHANGE_JSON={json.dumps(change_payload, ensure_ascii=False)}')
print(f'        <div class="meta">{summary}</div>')
if _all_facets:
    print(f'        <div class="meta" style="margin-top:6px;"><span class="pill info">Impact</span> {" + ".join(html.escape(f) for f in _all_facets)}</div>')
elif _bucket_order and set(_bucket_order) != {"other"}:
    print(f'        <div class="meta" style="margin-top:6px;"><span class="pill info">Areas</span> {", ".join(html.escape(b.replace("_", " ")) for b in _bucket_order)}</div>')

solution_row = next((r for r in change_rows if r["rel"] == "solution.ipynb" or r["rel"].endswith("/solution.ipynb")), None)
other_rows   = [r for r in change_rows if r is not solution_row] if solution_row else change_rows

if solution_row:
    # ── Cell-by-cell pipeline log ─────────────────────────────────────────
    nb_abs = os.path.join(root, solution_row["rel"])
    cells = _analyze_nb_cells(nb_abs)
    if cells:
        print('        <div style="margin-top:8px;padding:10px 12px;background:var(--surface-muted);border:1px solid var(--border);border-radius:8px;">')
        print('          <div style="font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px;">Notebook pipeline</div>')
        for idx, purpose, pill_cls, detail in cells:
            det_html = f' <span style="color:var(--text-soft);font-size:12px;">{html.escape(detail)}</span>' if detail else ""
            print(f'          <div style="display:flex;align-items:baseline;gap:8px;margin:3px 0;">'
                  f'<span style="color:var(--muted);font-size:11px;min-width:44px;">cell {idx}</span>'
                  f'<span class="pill {pill_cls}" style="font-size:10px;">{html.escape(purpose)}</span>'
                  f'{det_html}</div>')
        print('        </div>')
    st2, a2, r2 = solution_row["status"], solution_row["added"], solution_row["removed"]
    badge2 = {"added":"<span class=\"pill info\">added</span>","removed":"<span class=\"pill fail\">removed</span>","modified":"<span class=\"pill info\">modified</span>"}[st2]
    hdr2 = f'{badge2} <code>{html.escape(solution_row["rel"])}</code>'
    if a2 or r2:
        hdr2 += f' <span class="plus">+{a2}</span> / <span class="minus">-{r2}</span>'
    print('        <details style="margin-top:6px;">')
    print(f'          <summary>{hdr2} — show diff</summary>')
    print(f'          {solution_row["diff_html"]}')
    print('        </details>')
    if other_rows:
        other_esc = [f'<code>{html.escape(r["rel"])}</code>' for r in other_rows[:6]]
        extra_ct = len(other_rows) - len(other_esc)
        tail = f' <span class="muted">+{extra_ct} more</span>' if extra_ct > 0 else ""
        print(f'        <div class="meta muted" style="margin-top:4px;font-size:11.5px;">Also changed: {", ".join(other_esc)}{tail}</div>')
else:
    if is_pure_add or is_pure_remove:
        color_cls = "ok" if is_pure_add else "fail"
        verb      = "Created" if is_pure_add else "Deleted"
        names     = added_names if is_pure_add else removed_names
        pills = " ".join(
            f'<span class="pill {color_cls}"><code>{html.escape(n)}</code></span>'
            for n in names[:8]
        )
        extra = len(names) - 8
        if extra > 0:
            pills += f' <span class="muted">+{extra} more</span>'
        print(f'        <div class="meta" style="margin-top:6px;">{verb}: {pills}</div>')
    else:
        print('        <details>')
        print('          <summary>Show per-file diffs</summary>')
        for status, rel, added, removed, diff_html in changes:
            badge = {"added":"<span class=\"pill info\">added</span>","removed":"<span class=\"pill fail\">removed</span>","modified":"<span class=\"pill info\">modified</span>"}[status]
            header = f'{badge} <code>{html.escape(rel)}</code>'
            if added or removed:
                header += f' <span class="plus">+{added}</span> / <span class="minus">-{removed}</span>'
            print('          <details>')
            print(f'            <summary>{header}</summary>')
            print(f'            {diff_html}')
            print('          </details>')
        print('        </details>')
PY
  cat "$body_file"
  rm -f "$body_file"
}

hybrid_change_summary() {
  local change_json="$1"
  local out_file
  out_file="$(mktemp)"
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$change_json" "$MAX_AI_SUMMARY_CHARS" >"$out_file" <<'PY'
import json, os, sys, urllib.request, urllib.error

raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
cap = int(sys.argv[2]) if len(sys.argv) > 2 else 700

def out(source: str, status: str, text: str) -> None:
    text = (text or "").strip()
    if len(text) > cap:
        text = text[:cap - 3].rstrip() + "..."
    print(f"SOURCE={source}")
    print(f"STATUS={status}")
    print("TEXT=" + text.replace("\n", " "))

try:
    change = json.loads(raw) if raw else {}
except json.JSONDecodeError:
    change = {}

files = change.get("files") or []
counts = change.get("counts") or {}
churn = change.get("line_churn") or {}
imp = change.get("impact") or {}
n = len(files)
added = int(churn.get("added") or 0)
removed = int(churn.get("removed") or 0)
top = ", ".join(f.get("path", "?") for f in files[:3]) if files else "no files"
facets = imp.get("notebook_facets") or []
buckets = imp.get("buckets") or []
boost = (imp.get("boosting_hint") or "").strip()
parts = []
if buckets:
    parts.append("areas touched: " + ", ".join(str(b).replace("_", " ") for b in buckets))
if facets:
    parts.append("pipeline stages: " + ", ".join(facets))
if boost:
    parts.append("model detail: " + boost[:220])
context = (" (" + "; ".join(parts) + ")") if parts else ""
focus = "run the notebook and compare validation metrics" if ("notebook_pipeline" in buckets or facets) else "run the relevant checks or tests for this task type"
fallback = (
    f"Updated {n} file(s), mainly {top}{context}, with about +{added}/-{removed} lines of churn. "
    f"This should improve the current approach, and the next step is to {focus}."
)

endpoint = os.environ.get("AICODINGGYM_LLM_ENDPOINT", "").strip()
api_key = os.environ.get("AICODINGGYM_LLM_API_KEY", "").strip()
model = os.environ.get("AICODINGGYM_LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
timeout = float(os.environ.get("AICODINGGYM_LLM_TIMEOUT_SEC", "3.0") or "3.0")
if not endpoint or not api_key:
    out("fallback", "no_llm_config", fallback)
    raise SystemExit(0)

prompt = (
    "Summarize this coding change for a dashboard card in natural, concise English. "
    "Reply with exactly 2 short sentences and no markdown. "
    "Sentence 1 should explain what changed and the approach chosen, in plain words. "
    "Sentence 2 should state expected impact and the most practical next validation step. "
    "Make it sound natural for SWE, MLE, or code review tasks. "
    "If impact.notebook_facets exists, weave those stages in naturally. "
    "If impact.boosting_hint exists, include key parameter clues in plain words.\n\n"
    + json.dumps(change, ensure_ascii=False)
)
payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You write clear, human-sounding engineering update summaries."},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0.2,
}
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    endpoint,
    data=data,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    text = (
        (((parsed.get("choices") or [{}])[0].get("message") or {}).get("content"))
        or ""
    ).strip()
    if not text:
        out("fallback", "llm_empty", fallback)
    else:
        out("llm", "ok", text)
except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
    out("fallback", "llm_error", fallback)
PY
  cat "$out_file"
  rm -f "$out_file"
}

APPROACH_SNAP_TMP=""

# Refreshes the approach panel in dashboard.html. Runs summarize_approach.py,
# optionally injects LLM prose from .agent_note.json above the static columns,
# fixes the approach snapshot markers (existing bug fix), and splices the updated
# section into dashboard.html. Sets APPROACH_SNAP_TMP to a temp file path with
# the snap content (wrapped in BEGIN/END_APPROACH_DISPLAY markers) for the caller
# to pass to append_card(); caller must rm -f it.
refresh_approach_panel() {
  local llm_prose="${1:-}"
  local agent_cells_json="${2:-}"
  APPROACH_SNAP_TMP=""
  [[ -f "$APPROACH_HELPER" ]] || return 0
  [[ -f "$DASHBOARD_PATH" ]] || return 0
  local tmp_section snap_tmp
  tmp_section="$(mktemp)"
  snap_tmp="$(mktemp)"
  local _approach_extra_args=()
  if [[ -n "${agent_cells_json:-}" ]]; then
    _approach_extra_args=(--agent-cells "$agent_cells_json")
  fi
  if ! PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" "$APPROACH_HELPER" "$NOTEBOOK_PATH" "$tmp_section" "${_approach_extra_args[@]}" 2>/dev/null; then
    rm -f "$tmp_section" "$snap_tmp"
    return 0
  fi
  # Post-process: extract approachDisplay content, wrap in markers for chart dot
  # snapshots (fixes bug where snap was always empty), inject LLM prose if present.
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$tmp_section" "$snap_tmp" "$llm_prose" <<'PY'
import pathlib, re, sys, html as _h

tmp_section_path = pathlib.Path(sys.argv[1])
snap_path        = pathlib.Path(sys.argv[2])
llm_prose        = sys.argv[3] if len(sys.argv) > 3 else ""

content = tmp_section_path.read_text(encoding="utf-8")

# Extract the inner HTML of approachDisplay
m = re.search(r'(<div id="approachDisplay">)(.*?)(</div>\s*</section>)', content, re.DOTALL)
if m:
    pre, display_inner, post = m.group(1), m.group(2).strip(), m.group(3)
else:
    pre, display_inner, post = "", content, ""

# Build snap content for chart dot clicks
snap_inner = display_inner
if llm_prose:
    esc = _h.escape(llm_prose)
    llm_block = f'<div class="llm-approach-block"><p class="llm-approach-prose">{esc}</p></div>'
    snap_inner = llm_block + "\n" + display_inner

snap_content = "<!--BEGIN_APPROACH_DISPLAY-->\n" + snap_inner + "\n<!--END_APPROACH_DISPLAY-->"
snap_path.write_text(snap_content, encoding="utf-8")

# Inject LLM prose into the section HTML; clear old block if absent
if m:
    if llm_prose:
        esc = _h.escape(llm_prose)
        llm_block = (
            '<!--BEGIN_LLM_APPROACH-->'
            f'<div class="llm-approach-block"><p class="llm-approach-prose">{esc}</p></div>'
            '<!--END_LLM_APPROACH-->'
        )
        new_display = pre + "\n" + llm_block + "\n" + display_inner + "\n" + post
    else:
        # Clear any LLM block that may have been written by a previous run
        new_display = pre + "\n" + display_inner + "\n" + post
    content = content[: m.start()] + new_display + content[m.end():]

tmp_section_path.write_text(content, encoding="utf-8")
PY
  # Splice the updated approach section into dashboard.html
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$DASHBOARD_PATH" "$tmp_section" <<'PY'
import pathlib, re, sys
dash_path = pathlib.Path(sys.argv[1])
frag_path = pathlib.Path(sys.argv[2])
dash = dash_path.read_text(encoding="utf-8")
frag = frag_path.read_text(encoding="utf-8").strip()
pattern = re.compile(r'<section id="approach".*?</section>', re.DOTALL)
if pattern.search(dash):
    new = pattern.sub(lambda _m: frag, dash, count=1)
else:
    new = dash.replace("<main>", "<main>\n    " + frag, 1)
if new != dash:
    dash_path.write_text(new, encoding="utf-8", newline="\n")
PY
  rm -f "$tmp_section"
  APPROACH_SNAP_TMP="$snap_tmp"
}

# Produces a one-line summary of what changed in solution.ipynb since the last
# metric run. The previous notebook state is kept in $ROOT_DIR/.supervisor_prev_notebook.ipynb
# (excluded from snapshots and the change walker). Callers should refresh that
# state *after* consuming the note so the next run diffs against this run.
compute_notebook_change_note() {
  local prev="$ROOT_DIR/.supervisor_prev_notebook.ipynb"
  if [[ ! -f "$NOTEBOOK_PATH" ]]; then
    printf ''
    return
  fi
  if [[ ! -f "$prev" ]]; then
    printf 'First recorded run \xe2\x80\x94 refer to the Approach summary panel for the current pipeline.'
    return
  fi
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - "$prev" "$NOTEBOOK_PATH" <<'PY'
import json, sys, difflib, pathlib

def load_cells(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    out = []
    for cell in data.get("cells", []):
        ctype = cell.get("cell_type", "?")
        src = cell.get("source", [])
        if isinstance(src, list):
            src = "".join(src)
        out.append((ctype, src.splitlines()))
    return out

a = load_cells(sys.argv[1])
b = load_cells(sys.argv[2])
if a is None or b is None:
    print("Notebook could not be parsed; no change summary available.")
    sys.exit(0)

# Align by cell index (common case: supervisor re-runs with the same layout,
# individual cell sources mutated). We count +/- lines per cell and collect the
# single most informative added line (first non-empty + line) as a hint.
hints = []
total_add = total_rm = 0
touched = []
for i in range(max(len(a), len(b))):
    if i >= len(a):
        ctype, lines = b[i]
        total_add += len(lines)
        touched.append(f"+cell {i} ({ctype})")
        for ln in lines:
            s = ln.strip()
            if s:
                hints.append(s)
                break
        continue
    if i >= len(b):
        ctype, lines = a[i]
        total_rm += len(lines)
        touched.append(f"-cell {i} ({ctype})")
        continue
    (ta, la), (tb, lb) = a[i], b[i]
    if la == lb and ta == tb:
        continue
    diff = list(difflib.unified_diff(la, lb, lineterm=""))
    a_cnt = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    r_cnt = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    if a_cnt == 0 and r_cnt == 0:
        continue
    total_add += a_cnt
    total_rm += r_cnt
    touched.append(f"cell {i} (+{a_cnt}/-{r_cnt})")
    if not hints:
        for ln in diff:
            if ln.startswith("+") and not ln.startswith("+++"):
                s = ln[1:].strip()
                if s:
                    hints.append(s)
                    break

if not touched:
    print("Re-ran notebook; source unchanged since the previous metric.")
    sys.exit(0)

if len(touched) <= 3:
    detail = ", ".join(touched)
else:
    detail = ", ".join(touched[:3]) + f" and {len(touched)-3} more"

summary = f"Notebook source changed: {detail}; total +{total_add}/-{total_rm} source lines."
if hints:
    hint = hints[0]
    if len(hint) > 140:
        hint = hint[:137] + "..."
    summary += f" First new line: `{hint}`"
print(summary)
PY
}

run_notebook_and_log_metric() {
  read_agent_note
  local _cache="$ROOT_DIR/.supervisor_agent_meta_cache.json"
  local _agent_model_json=""
  local _agent_nb_analysis_json=""
  if [[ -f "$_cache" ]]; then
    _agent_model_json="$(PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    m = d.get('model')
    print(json.dumps(m) if m else '')
except Exception: print('')
" "$_cache" 2>/dev/null || true)"
    _agent_nb_analysis_json="$(PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    nb = d.get('notebook_analysis')
    print(json.dumps(nb) if nb else '')
except Exception: print('')
" "$_cache" 2>/dev/null || true)"
  fi
  if [[ ! -f "$NOTEBOOK_PATH" ]]; then
    append_card "Notebook Metric" 'No <code>solution.ipynb</code> found yet' '        <div class="empty">Create solution.ipynb to enable automatic metric extraction.</div>'
    refresh_approach_panel
    [[ -n "${APPROACH_SNAP_TMP:-}" ]] && rm -f "$APPROACH_SNAP_TMP"
    return
  fi
  local change_note
  change_note="$(compute_notebook_change_note || true)"
  local output status max_acc

  # If an agent provided a prompt key/timestamp but not prompt text, we
  # still want the dashboard to show *something* (instead of "context
  # unavailable").
  if [[ -z "${AGENT_PROMPT:-}" ]]; then
    if [[ -n "${AGENT_PROMPT_ID:-}" ]]; then
      AGENT_PROMPT="$AGENT_PROMPT_ID"
    elif [[ -n "${AGENT_PROMPT_TS:-}" ]]; then
      AGENT_PROMPT="$AGENT_PROMPT_TS"
    fi
  fi
  set +e
  output="$("$PY_BIN" "$HELPER" "$NOTEBOOK_PATH" 2>&1)"
  status=$?
  set -e
  max_acc="$(printf '%s\n' "$output" | grep -oE 'MAX_VALIDATION_ACCURACY=[^[:space:]]+' | tail -n1 | cut -d= -f2 || true)"
  [[ -z "${max_acc:-}" ]] && max_acc="NA"
  local model_name hyperparams
  model_name="$(printf '%s\n' "$output" | grep -oE 'MODEL_NAME=[^[:space:]]+' | tail -n1 | cut -d= -f2- || true)"
  hyperparams="$(printf '%s\n' "$output" | grep -m1 '^HYPERPARAMS=' | sed 's/^HYPERPARAMS=//' || true)"
  # Agent model data takes priority over auto-detected; merge if agent absent
  local _combined_model_json="${_agent_model_json:-}"
  if [[ -z "$_combined_model_json" ]] && [[ -n "${model_name:-}" ]]; then
    local _hp_json="{}"
    [[ -n "${hyperparams:-}" ]] && _hp_json="$(printf '{"raw":"%s"}' "$(printf '%s' "${hyperparams}" | sed 's/\\/\\\\/g; s/"/\\"/g')")"
    _combined_model_json="{\"name\":\"$(printf '%s' "${model_name}" | sed 's/\\/\\\\/g; s/"/\\"/g')\",\"type\":\"auto-detected\",\"hyperparams\":$_hp_json}"
  fi
  local tail_output
  tail_output="$(printf '%s\n' "$output" | tail -n "$MAX_OUTPUT_LINES")"
  local prompt_key prompt_seq
  prompt_key="${AGENT_PROMPT_ID:-}"
  [[ -z "$prompt_key" ]] && prompt_key="${AGENT_PROMPT:-}"
  [[ -z "$prompt_key" ]] && prompt_key="${AGENT_PROMPT_TS:-}"
  prompt_seq="${AGENT_PROMPT_TS:-}"
  local body
  body="$(printf '        <details>\n          <summary>Show notebook output (last %d lines)</summary>\n          <pre>%s</pre>\n        </details>' \
    "$MAX_OUTPUT_LINES" \
    "$(printf '%s' "$tail_output" | html_escape)")"
  # Refresh approach panel (creates snap with markers, splices into dashboard.html)
  # Extract cells array from agent notebook_analysis for approach panel
  local _agent_cells_for_approach="[]"
  if [[ -n "${_agent_nb_analysis_json:-}" ]]; then
    _agent_cells_for_approach="$(PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" -c "
import json, sys
try:
    nb = json.loads(sys.argv[1])
    cells = nb.get('cells', []) if isinstance(nb, dict) else []
    print(json.dumps(cells))
except Exception:
    print('[]')
" "$_agent_nb_analysis_json" 2>/dev/null || echo '[]')"
  fi
  refresh_approach_panel "${AGENT_APPROACH:-}" "${_agent_cells_for_approach}"
  local snap_tmp="${APPROACH_SNAP_TMP:-}"
  # Build model/hyperparams suffix shown in the metric card header
  local meta_suffix=""
  if [[ -n "${model_name:-}" ]]; then
    meta_suffix=" <span class=\"pill info\">$(printf '%s' "${model_name}" | html_escape)</span>"
  fi
  if [[ -n "${hyperparams:-}" ]]; then
    meta_suffix="${meta_suffix}<br><code style=\"font-size:11px;\">$(printf '%s' "${hyperparams}" | html_escape)</code>"
  fi
  local meta
  if [[ "$status" -ne 0 ]]; then
    meta='<span class="pill fail">exit '"$status"'</span> <code>MAX_VALIDATION_ACCURACY='"$max_acc"'</code>'"${meta_suffix}"
    append_card "Notebook Metric" "$meta" "$body" "" "$change_note" "$snap_tmp" "" \
      "${AGENT_SUMMARY:-}" "" "" "${AGENT_WHY:-}" "${AGENT_STAGE:-}" "${AGENT_IMPACT:-}" \
      "${AGENT_PROMPT:-}" "${AGENT_NEXT_PROMPT:-}" "$prompt_key" "$prompt_seq" \
      "${_combined_model_json:-}" "${_agent_nb_analysis_json:-}"
  elif [[ "$max_acc" == "NA" ]]; then
    meta="<code>MAX_VALIDATION_ACCURACY=NA</code> — add <code>VAL_ACC: &lt;float&gt;</code> or <code>validation_accuracy: &lt;float&gt;</code> in your notebook${meta_suffix}"
    append_card "Notebook Metric" "$meta" "$body" "" "$change_note" "$snap_tmp" "" \
      "${AGENT_SUMMARY:-}" "" "" "${AGENT_WHY:-}" "${AGENT_STAGE:-}" "${AGENT_IMPACT:-}" \
      "${AGENT_PROMPT:-}" "${AGENT_NEXT_PROMPT:-}" "$prompt_key" "$prompt_seq" \
      "${_combined_model_json:-}" "${_agent_nb_analysis_json:-}"
  else
    meta="<code>MAX_VALIDATION_ACCURACY=$max_acc</code>${meta_suffix}"
    append_card "Notebook Metric" "$meta" "$body" "$max_acc" "$change_note" "$snap_tmp" "" \
      "${AGENT_SUMMARY:-}" "" "" "${AGENT_WHY:-}" "${AGENT_STAGE:-}" "${AGENT_IMPACT:-}" \
      "${AGENT_PROMPT:-}" "${AGENT_NEXT_PROMPT:-}" "$prompt_key" "$prompt_seq" \
      "${_combined_model_json:-}" "${_agent_nb_analysis_json:-}"
  fi
  [[ -n "$snap_tmp" ]] && rm -f "$snap_tmp"
  # Persist metric to .metrics_log.jsonl so it survives dashboard resets.
  if [[ "$max_acc" != "NA" ]]; then
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_BIN" - \
      "$(timestamp)" "$max_acc" "${AGENT_PROMPT:-}" "$ROOT_DIR/.metrics_log.jsonl" \
      "${model_name:-}" "${hyperparams:-}" \
      2>/dev/null <<'PY' || true
import json, sys
ts, metric, prompt, log_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
model_name, hyperparams = (sys.argv[5] if len(sys.argv) > 5 else ""), (sys.argv[6] if len(sys.argv) > 6 else "")
entry: dict = {"timestamp": ts, "metric": float(metric), "prompt": prompt}
if model_name: entry["model"] = model_name
if hyperparams: entry["hyperparams"] = hyperparams
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY
  fi
  cp -f "$NOTEBOOK_PATH" "$ROOT_DIR/.supervisor_prev_notebook.ipynb" 2>/dev/null || true
}

run_wrapped_command() {
  local command="$1"
  ensure_dashboard
  snapshot_workspace

  local command_output status
  set +e
  command_output="$(bash -lc "$command" 2>&1)"
  status=$?
  set -e

  sleep 1
  local raw_body title body meta change_json summary_meta ai_source ai_status ai_text
  raw_body="$(render_change_card_body)"
  title="$(printf "%s\n" "$raw_body" | head -n1)"
  change_json="$(printf "%s\n" "$raw_body" | sed -n '2p')"
  if [[ "$title" == TITLE=* ]]; then
    title="${title#TITLE=}"
    body="$(printf "%s\n" "$raw_body" | tail -n +3)"
  else
    title="AI Run"
    body="$raw_body"
  fi
  if [[ "$change_json" == CHANGE_JSON=* ]]; then
    change_json="${change_json#CHANGE_JSON=}"
  else
    change_json=""
  fi
  summary_meta="$(hybrid_change_summary "$change_json")"
  ai_source="$(printf "%s\n" "$summary_meta" | sed -n 's/^SOURCE=//p' | head -n1)"
  ai_status="$(printf "%s\n" "$summary_meta" | sed -n 's/^STATUS=//p' | head -n1)"
  ai_text="$(printf "%s\n" "$summary_meta" | sed -n 's/^TEXT=//p' | head -n1)"
  if [[ "$status" -eq 0 ]]; then
    meta="<code>$(printf "%s" "$command" | html_escape)</code>"
  else
    meta='<span class="pill fail">exit '"$status"'</span> <code>'"$(printf "%s" "$command" | html_escape)"'</code>'
  fi
  append_card "$title" "$meta" "$body" "" "" "" "$change_json" "$ai_text" "$ai_source" "$ai_status"

  local tail_output
  tail_output="$(printf "%s\n" "$command_output" | tail -n "$MAX_OUTPUT_LINES")"
  local output_body
  output_body="$(printf '        <details>\n          <summary>Show command output (last %d lines)</summary>\n          <pre>%s</pre>\n        </details>' \
    "$MAX_OUTPUT_LINES" \
    "$(printf "%s" "$tail_output" | html_escape)")"
  append_card "Command Output" "" "$output_body"

  run_notebook_and_log_metric
}

watch_loop() {
  ensure_dashboard
  local first_run=0
  if [[ ! -d "$SNAPSHOT_DIR" ]]; then
    first_run=1
  fi
  snapshot_workspace
  # Populate the Approach summary panel immediately so users see it even
  # before the first notebook run completes.
  refresh_approach_panel
  if [[ "$first_run" -eq 1 ]]; then
    append_card "Supervisor Ready" "<span class=\"pill info\">watching</span> interval=${WATCH_INTERVAL}s â€“ edits you make will appear below" '        <div class="empty">No changes yet. Start codingâ€”each save will append a card.</div>'
    # Auto-open the dashboard only for manual terminal runs; when the aicodinggym
    # CLI spawns us in the background stdout is redirected to a file and will
    # pop the browser itself, so we skip to avoid opening two tabs.
    if [[ -t 1 ]]; then
      open_dashboard
    fi
  else
    append_card "Watcher Restarted" "<span class=\"pill info\">watching</span> interval=${WATCH_INTERVAL}s" '        <div class="empty">Resuming watch mode.</div>'
  fi
  # One metric pass at startup so the chart + trajectory + approach snapshot
  # populate without waiting for a file-change event (nbconvert may be slow).
  if [[ -f "$NOTEBOOK_PATH" ]]; then
    run_notebook_and_log_metric
  fi
  snapshot_workspace
  while true; do
    sleep "$WATCH_INTERVAL"
    # Check whether any file actually changed before doing expensive work.
    local raw title body change_json summary_meta ai_source ai_status ai_text
    raw="$(render_change_card_body)"
    if [[ "$raw" == *"No file changes detected."* ]]; then
      continue
    fi
    title="$(printf "%s\n" "$raw" | head -n1)"
    change_json="$(printf "%s\n" "$raw" | sed -n '2p')"
    if [[ "$title" == TITLE=* ]]; then
      title="${title#TITLE=}"
      body="$(printf "%s\n" "$raw" | tail -n +3)"
    else
      title="Change Detected"
      body="$raw"
    fi
    if [[ "$change_json" == CHANGE_JSON=* ]]; then
      change_json="${change_json#CHANGE_JSON=}"
    else
      change_json=""
    fi
    read_agent_note
    summary_meta="$(hybrid_change_summary "$change_json")"
    ai_source="$(printf "%s\n" "$summary_meta" | sed -n 's/^SOURCE=//p' | head -n1)"
    ai_status="$(printf "%s\n" "$summary_meta" | sed -n 's/^STATUS=//p' | head -n1)"
    ai_text="$(printf "%s\n" "$summary_meta" | sed -n 's/^TEXT=//p' | head -n1)"
    append_card "$title" "" "$body" "" "" "" "$change_json" "$ai_text" "$ai_source" "$ai_status" \
      "${AGENT_WHY:-}" "${AGENT_STAGE:-}" "${AGENT_IMPACT:-}" \
      "${AGENT_PROMPT:-}" "${AGENT_NEXT_PROMPT:-}"
    run_notebook_and_log_metric
    snapshot_workspace
  done
}

submit_flow() {
  ensure_dashboard
  local output status ground_truth
  set +e
  output="$(bash -lc "$SUBMIT_CMD" 2>&1)"
  status=$?
  set -e
  ground_truth="$(printf "%s\n" "$output" | grep -oEi 'ground[ _-]?truth[^0-9]*[0-9]+(\.[0-9]+)?' | head -n1 || true)"
  [[ -z "${ground_truth:-}" ]] && ground_truth="Ground Truth: not found in output"
  local meta
  if [[ "$status" -eq 0 ]]; then
    meta='<span class="muted">Submitted</span>'
  else
    meta='<span class="pill fail">Submit failed (exit '"$status"')</span>'
  fi
  local body
  body="$(printf '        <div class="meta">%s</div>\n        <details open>\n          <summary>Show submit log</summary>\n          <pre>%s</pre>\n        </details>' \
    "$(printf "%s" "$ground_truth" | html_escape)" \
    "$(printf "%s" "$output" | html_escape)")"
  append_card "Final Result" "$meta" "$body"
  open_dashboard
}

open_dashboard() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$DASHBOARD_PATH" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$DASHBOARD_PATH" >/dev/null 2>&1 || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Start-Process '$DASHBOARD_PATH'" >/dev/null 2>&1 || true
  fi
}

usage() {
  cat <<EOF
Usage:
  ./supervisor.sh --watch              # background watcher (auto-started on fetch)
  ./supervisor.sh --cmd "<command>"    # run one command and log the diff
  ./supervisor.sh --submit             # run the bound submit command
  ./supervisor.sh --open               # open dashboard.html in the browser

Examples:
  ./supervisor.sh --watch
  ./supervisor.sh --cmd "$DEFAULT_CMD"
  ./supervisor.sh --submit
EOF
}

main() {
  if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Supervisor already running (pid $pid). Remove $LOCK_FILE if this is stale."
      exit 1
    fi
    rm -f "$LOCK_FILE"
  fi
  trap 'rm -f "$LOCK_FILE"' EXIT
  echo "$$" >"$LOCK_FILE"

  local mode="watch"
  local cmd="$DEFAULT_CMD"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --watch) mode="watch" ;;
      --cmd)
        shift
        [[ $# -gt 0 ]] || { echo "Missing value for --cmd"; exit 2; }
        mode="cmd"
        cmd="$1"
        ;;
      --submit) mode="submit" ;;
      --open) mode="open" ;;
      --interval) shift; WATCH_INTERVAL="${1:-3}" ;;
      -h|--help) usage; return ;;
      *) echo "Unknown arg: $1"; usage; exit 2 ;;
    esac
    shift
  done

  ensure_dashboard
  case "$mode" in
    watch) watch_loop ;;
    cmd) run_wrapped_command "$cmd" ;;
    submit) submit_flow ;;
    open) open_dashboard ;;
  esac
}

main "$@"
