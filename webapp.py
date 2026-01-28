#!/usr/bin/env python3
"""
Lightweight Flask UI for the _timewarp toolkit.
Screens: download, scan, shift, upload.
Each screen sets env vars, reloads the corresponding script module, and runs its main() while capturing output.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import time
import subprocess
import re
import json
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Tuple
from threading import Thread, Lock

from flask import Flask, redirect, render_template_string, request, url_for, jsonify

from env_utils import load_env

# Ensure local modules are importable
BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

app = Flask(__name__)
load_env()  # load .env into process defaults if present


def read_env_file(path: Path) -> dict:
    """Read key/value pairs from a .env-style file without mutating os.environ."""
    if not path.exists():
        return {}
    data = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("#"):
            value = ""
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        data[key] = value
    return data


ENV_EXAMPLE_DEFAULTS = read_env_file(BASE_DIR / ".env.example")

TASKS: Dict[str, dict] = {
    "scan": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "lock": Lock()},
    "download": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "lock": Lock()},
    "shift": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "lock": Lock()},
    "upload": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "lock": Lock()},
}


@contextmanager
def temp_environ(updates: Dict[str, str]) -> Any:
    """Temporarily set environment variables for a block."""
    old = {}
    missing = []
    for k, v in updates.items():
        if v is None:
            continue
        if k in os.environ:
            old[k] = os.environ[k]
        else:
            missing.append(k)
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ[k] = v
        for k in missing:
            os.environ.pop(k, None)


def run_task(module_name: str, env_updates: Dict[str, str]) -> Tuple[bool, str]:
    """Reload module after applying env vars, run main(), and capture stdout/stderr."""
    # Drop empty strings to avoid wiping existing env defaults; keep explicit "false"/"true"
    cleaned = {k: v for k, v in env_updates.items() if v not in ("", None)}
    buffer = io.StringIO()
    try:
        with temp_environ(cleaned):
            mod = importlib.import_module(module_name)
            mod = importlib.reload(mod)
            with redirect_stdout(buffer), redirect_stderr(buffer):
                if hasattr(mod, "main"):
                    mod.main()
                else:
                    raise RuntimeError(f"Module '{module_name}' has no main()")
        return True, buffer.getvalue()
    except Exception as exc:
        buffer.write(f"\nERROR: {exc}\n")
        return False, buffer.getvalue()


PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>_timewarp Web UI</title>
  <style>
    :root { font-family: "Inter", system-ui, -apple-system, sans-serif; color: #0f172a; background: #f8fafc; }
    body { margin: 0; }
    header { background: linear-gradient(120deg, #0f172a, #1e293b); color: #f8fafc; padding: 18px 24px; }
    header a { color: #cbd5e1; text-decoration: none; margin-right: 16px; }
    main { padding: 24px; max-width: 1080px; margin: 0 auto; }
    h1 { margin-top: 0; font-size: 28px; }
    form { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px; box-shadow: 0 4px 20px rgba(15, 23, 42, 0.05); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
    label { font-weight: 600; display: block; margin-bottom: 6px; color: #0f172a; }
    input, textarea { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc; }
    input[type="checkbox"] { width: auto; }
    .actions { margin-top: 18px; display: flex; gap: 10px; }
    button { background: #0f172a; color: #fff; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700; cursor: pointer; }
    button.secondary { background: #e2e8f0; color: #0f172a; }
    pre { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 10px; max-height: 480px; overflow: auto; }
    .status-ok { color: #16a34a; font-weight: 700; }
    .status-fail { color: #dc2626; font-weight: 700; }
    .hidden { display: none; }
    .progress { margin: 16px 0; height: 10px; background: #e2e8f0; border-radius: 999px; overflow: hidden; }
    .progress .bar { height: 100%; width: 0%; background: linear-gradient(90deg,#0ea5e9,#6366f1); transition: width 0.3s ease; }
  </style>
  <script>
    document.addEventListener('DOMContentLoaded', () => {
      const form = document.querySelector('form');
      const progress = document.getElementById('progress');
      const output = document.getElementById('live-output');
      const stopBtn = document.getElementById('stop-btn');
      if (form) {
        form.addEventListener('submit', () => {
          // Leave inputs enabled so values submit; instead disable buttons and set text inputs readonly.
          form.querySelectorAll('button').forEach(el => el.disabled = true);
          form.querySelectorAll('input[type=\"text\"], input[type=\"number\"], input[type=\"password\"]').forEach(el => el.readOnly = true);
          if (progress) progress.classList.remove('hidden');
          if (output && !output.textContent.trim()) output.textContent = 'Running...';
          if (stopBtn) stopBtn.classList.remove('hidden');
        });
      }

      const taskSlug = window.taskSlug || null;
      if (taskSlug) {
        const poll = () => {
          fetch(`/${taskSlug}/progress`).then(r => r.json()).then(data => {
            if (progress) {
              progress.classList.toggle('hidden', !data.running);
              const bar = document.getElementById('progress-bar');
              if (bar) {
                const pct = data.progress === null || data.progress === undefined ? 0 : Math.max(2, Math.floor(data.progress * 100));
                bar.style.width = pct + '%';
              }
            }
            if (output) {
              output.textContent = data.log || '';
            }
            if (stopBtn) stopBtn.classList.toggle('hidden', !data.running);
            if (data.running) {
              setTimeout(poll, 800);
            }
          }).catch(() => setTimeout(poll, 1500));
        };
        poll();
      }

      if (stopBtn) {
        stopBtn.addEventListener('click', () => {
          const taskSlug = window.taskSlug || null;
          if (!taskSlug) return;
          stopBtn.disabled = true;
          fetch(`/${taskSlug}/stop`, {method:'POST'});
        });
      }
    });
  </script>
</head>
<body>
  <script>
    window.taskSlug = "{{ task_slug or '' }}";
  </script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <header>
    <strong>_timewarp Web UI</strong>
    <nav style="display:inline-block; margin-left:20px;">
      <a href="{{ url_for('home') }}">Home</a>
      <a href="{{ url_for('download') }}">Download</a>
      <a href="{{ url_for('scan') }}">Time-scan</a>
      <a href="{{ url_for('shift') }}">Time-shift</a>
      <a href="{{ url_for('upload') }}">Upload</a>
    </nav>
  </header>
  <main>
    <h1>{{ title }}</h1>
    {% if fields %}
    <form method="post">
      <div class="grid">
        {% for f in fields %}
        <div>
          <label for="{{ f.name }}">{{ f.label }}</label>
          {% if f.type == "checkbox" %}
            <input type="checkbox" name="{{ f.name }}" id="{{ f.name }}" value="true" {% if f.value in ['true','True','1','on'] %}checked{% endif %}>
          {% else %}
            <input type="{{ f.type }}" name="{{ f.name }}" id="{{ f.name }}" value="{{ f.value|e }}" placeholder="{{ f.placeholder or '' }}" {% if f.step %}step="{{ f.step }}"{% endif %}>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      <div class="actions">
        <button type="submit">{{ action }}</button>
        <a href="{{ request.path }}"><button type="button" class="secondary">Reset</button></a>
      </div>
    </form>
    {% endif %}
    <div id="progress" class="progress hidden"><div id="progress-bar" class="bar"></div></div>
    <button id="stop-btn" type="button" class="secondary hidden" onclick="fetch('/scan/stop', {method:'POST'});">Stop</button>
    {% if result is not none and success is not none %}
      <p class="{{ 'status-ok' if success else 'status-fail' }}">{{ 'Completed' if success else 'Failed' }}</p>
      <pre id="live-output">{{ result }}</pre>
    {% else %}
      <pre id="live-output"></pre>
    {% endif %}
    {% if extra %}
      {{ extra|safe }}
    {% endif %}
  </main>
</body>
</html>
"""


def render_page(
    title: str,
    fields: List[Dict[str, Any]] | None,
    action: str | None = None,
    result: str | None = None,
    success: bool | None = None,
    extra_html: str | None = None,
    task_slug: str | None = None,
) -> str:
    return render_template_string(
        PAGE_TEMPLATE,
        title=title,
        fields=fields or [],
        action=action,
        result=result,
        success=success,
        extra=extra_html,
        task_slug=task_slug,
    )


def prefill(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for f in fields:
        if request.method == "POST":
            value = request.form.get(f["name"], "")
        else:
            value = os.getenv(f["name"], ENV_EXAMPLE_DEFAULTS.get(f["name"], ""))
        out.append({**f, "value": value})
    return out


@app.route("/")
def home():
    extra = """
    <p>Use the navigation above to run the four stages: download, time-scan, time-shift, and upload.</p>
    <p>Each screen lets you override the environment variables used by the existing CLI scripts, then runs the same code and shows the logs.</p>
    <ul>
      <li><a href="{{ url_for('download') }}">Download</a> — run import_data.py</li>
      <li><a href="{{ url_for('scan') }}">Time-scan</a> — run scan_sourcetypes_and_dates.py</li>
      <li><a href="{{ url_for('shift') }}">Time-shift</a> — run shift_timestamps.py</li>
      <li><a href="{{ url_for('upload') }}">Upload</a> — run upload_data_hec.py</li>
    </ul>
    """
    return render_page("Welcome", fields=None, action=None, extra_html=extra)


@app.route("/download", methods=["GET", "POST"])
def download():
    fields = prefill([
        {"name": "SPLUNK_BASE_URL", "label": "Splunk Base URL", "type": "text"},
        {"name": "SPLUNK_USERNAME", "label": "Username", "type": "text"},
        {"name": "SPLUNK_PASSWORD", "label": "Password", "type": "password"},
        {"name": "SPLUNK_SEARCH", "label": "Base search (no 'search')", "type": "text"},
        {"name": "SPLUNK_FIELDS", "label": "Fields (space separated)", "type": "text"},
        {"name": "SPLUNK_EXPORT_DIR", "label": "Export dir", "type": "text"},
        {"name": "SPLUNK_EARLIEST", "label": "Earliest (ISO)", "type": "text", "placeholder": "YYYY-MM-DDTHH:MM:SS"},
        {"name": "SPLUNK_LATEST", "label": "Latest (ISO)", "type": "text", "placeholder": "YYYY-MM-DDTHH:MM:SS"},
        {"name": "SPLUNK_STEP_HOURS", "label": "Window size (hours)", "type": "number", "step": "0.25"},
    ])

    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("download", "import_data.py", env_updates)

    return render_page("Download", fields, "Run download", task_slug="download")


@app.route("/scan", methods=["GET", "POST"])
def scan():
    fields = prefill([
        {"name": "SCAN_INPUT_GLOB", "label": "Input glob", "type": "text"},
    ])

    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("scan", "scan_sourcetypes_and_dates.py", env_updates)

    return render_page("Time-scan", fields, "Run scan", task_slug="scan", extra_html=build_analyze_panel_js())


@app.route("/shift", methods=["GET", "POST"])
def shift():
    fields = prefill([
        {"name": "SHIFT_INPUT_GLOB", "label": "Input glob", "type": "text"},
        {"name": "SHIFT_OUTPUT_DIR", "label": "Output dir", "type": "text"},
        {"name": "SHIFT_TARGET_EARLIEST_ISO", "label": "Target earliest ISO (optional)", "type": "text", "placeholder": "YYYY-MM-DDTHH:MM:SSZ"},
        {"name": "SHIFT_TARGET_EARLIEST_NOW_MINUS_DAYS", "label": "If no target ISO, use now minus N days", "type": "number", "step": "1"},
        {"name": "SHIFT_AGGRESSIVE_RAW_REWRITE", "label": "Aggressive raw rewrite", "type": "checkbox"},
    ])

    if request.method == "POST":
        env_updates = {}
        for f in fields:
            if f["type"] == "checkbox":
                env_updates[f["name"]] = "true" if request.form.get(f["name"]) else "false"
            else:
                env_updates[f["name"]] = request.form.get(f["name"], "")
        start_task("shift", "shift_timestamps.py", env_updates)

    return render_page("Time-shift", fields, "Run shift", task_slug="shift")


def _task_state(name: str) -> dict:
    return TASKS[name]


def start_task(name: str, script: str, env_updates: Dict[str, str]) -> None:
    state = _task_state(name)
    cleaned = {k: v for k, v in env_updates.items() if v not in ("", None)}
    env = os.environ.copy()
    env.update(cleaned)

    with state["lock"]:
        state["log"] = []
        state["progress"] = None
        state["counts"] = None
        state["running"] = True
        state["summary"] = None

    cmd = [sys.executable, "-u", script]
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    state["proc"] = proc

    def reader():
        try:
            for line in proc.stdout:
                with state["lock"]:
                    state["log"].append(line)
                    if line.startswith("SCAN_SUMMARY_JSON::"):
                        try:
                            summary_json = line.split("SCAN_SUMMARY_JSON::",1)[1]
                            state["summary"] = json.loads(summary_json)
                        except Exception:
                            pass
                    m = re.search(r"\[(\d+)/(\d+)\]", line)
                    if m:
                        cur = int(m.group(1))
                        total = int(m.group(2))
                        if total > 0:
                            state["counts"] = (cur, total)
                            state["progress"] = min(1.0, max(0.0, cur / total))
            proc.wait()
        finally:
            with state["lock"]:
                state["running"] = False

    Thread(target=reader, daemon=True).start()


def task_progress(name: str) -> dict:
    state = _task_state(name)
    with state["lock"]:
        log = "".join(state["log"])
        running = state["running"]
        progress = state["progress"]
        counts = state["counts"]
        summary = state.get("summary")
    return {"running": running, "log": log, "progress": progress, "counts": counts, "summary": summary}


def stop_task(name: str) -> None:
    state = _task_state(name)
    proc: subprocess.Popen | None = state.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    with state["lock"]:
        state["running"] = False

@app.route("/upload", methods=["GET", "POST"])
def upload():
    fields = prefill([
        {"name": "HEC_INPUT_GLOB", "label": "Input glob", "type": "text"},
        {"name": "SPLUNK_HEC_BASE_URL", "label": "HEC Base URL", "type": "text"},
        {"name": "SPLUNK_HEC_TOKEN", "label": "HEC Token", "type": "password"},
    ])

    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("upload", "upload_data_hec.py", env_updates)

    return render_page("Upload", fields, "Run upload", task_slug="upload")


@app.route("/<task>/progress")
def task_progress_route(task: str):
    if task not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    return jsonify(task_progress(task))

@app.route("/scan/summary")
def scan_summary_route():
    if "scan" not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    state = TASKS["scan"]
    with state["lock"]:
        summary = state.get("summary")
        if summary is None:
            # Fallback: try to parse the latest SCAN_SUMMARY_JSON line from log buffer
            for line in reversed(state.get("log", [])):
                if line.startswith("SCAN_SUMMARY_JSON::"):
                    try:
                        summary = json.loads(line.split("SCAN_SUMMARY_JSON::", 1)[1])
                        state["summary"] = summary
                        break
                    except Exception:
                        continue
    if summary is None:
        return jsonify({"error": "no summary available; run a time-scan first"}), 404
    return jsonify(summary)


@app.route("/<task>/stop", methods=["POST"])
def task_stop_route(task: str):
    if task not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    stop_task(task)
    return jsonify({"stopped": True})


def build_analyze_panel_js() -> str:
    return """
    <section style="margin-top:16px;">
      <div style="display:flex;gap:12px;margin:0 0 12px;">
        <button id="tab-logs" class="secondary" type="button">Logs</button>
        <button id="tab-analyze" class="secondary" type="button">Timeline</button>
        <button id="tab-table" class="secondary" type="button">Table</button>
      </div>
      <div id="panel-logs">
        <!-- logs already rendered in live-output -->
      </div>
      <div id="panel-analyze" class="hidden">
        <div style="margin-bottom:8px;color:#475569;font-size:13px;">Timeline uses <code>_time</code> min/max per sourcetype (not raw fields).</div>
        <div style="margin-bottom:12px;">
          <label style="font-weight:700;display:block;margin-bottom:4px;">Sourcetypes</label>
          <div id="st-list" style="display:flex;flex-direction:column;gap:8px;max-height:220px;overflow:auto;border:1px solid #e2e8f0;padding:8px;border-radius:8px;background:#fff;"></div>
        </div>
        <canvas id="chart" height="260"></canvas>
      </div>
      <div id="panel-table" class="hidden">
        <h3>Ranges & Raw Fields</h3>
        <table id="summary-table" style="width:100%; border-collapse:collapse; font-size:14px;">
          <thead><tr><th style="text-align:left;">Sourcetype</th><th>_time min</th><th>_time max</th><th>Total</th><th>Raw timestamp fields?</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>
    <script>
      const logsTab = document.getElementById('tab-logs');
      const analyzeTab = document.getElementById('tab-analyze');
      const tabTable = document.getElementById('tab-table');
      const panelLogs = document.getElementById('panel-logs');
      const panelAnalyze = document.getElementById('panel-analyze');
      const panelTable = document.getElementById('panel-table');
      let summaryLoaded = false;
      let cachedSummary = null;
      function activate(tab) {
        const logsActive = tab === 'logs';
        const analyzeActive = tab === 'analyze';
        const tableActive = tab === 'table';
        panelLogs.classList.toggle('hidden', !logsActive);
        panelAnalyze.classList.toggle('hidden', !analyzeActive);
        panelTable.classList.toggle('hidden', !tableActive);
        logsTab.classList.toggle('secondary', !logsActive);
        analyzeTab.classList.toggle('secondary', !analyzeActive);
        tabTable.classList.toggle('secondary', !tableActive);
        if ((analyzeActive || tableActive) && !summaryLoaded) {
          loadSummary();
        } else if (tableActive && cachedSummary) {
          renderTable(cachedSummary, currentSelected);
        }
      }
      logsTab?.addEventListener('click', () => activate('logs'));
      analyzeTab?.addEventListener('click', () => activate('analyze'));
      tabTable?.addEventListener('click', () => activate('table'));
      activate('logs');
      const chartCtx = document.getElementById('chart').getContext('2d');
      let chart;
      function renderChart(summary, selected) {
        const datasets = [];
        const colors = ['#0ea5e9','#6366f1','#22c55e','#f97316','#e11d48','#a855f7','#14b8a6','#f59e0b'];
        let ci = 0;
        let globalMin = Infinity;
        let globalMax = -Infinity;
        for (const st of summary.sourcetypes) {
          if (!selected.has(st.sourcetype)) continue;
          if (!st.time_range.min || !st.time_range.max) continue;
          const start = new Date(st.time_range.min).getTime();
          const end = new Date(st.time_range.max).getTime();
          globalMin = Math.min(globalMin, start);
          globalMax = Math.max(globalMax, end);
          datasets.push({
            label: st.sourcetype,
            data: [
              {x: start, y: st.sourcetype},
              {x: end, y: st.sourcetype},
            ],
            backgroundColor: colors[ci % colors.length],
            borderColor: colors[ci % colors.length],
            borderWidth: 10,
            showLine: true,
            pointRadius: 0,
            });
          ci++;
        }
        if (!datasets.length) {
          if (chart) chart.destroy();
          return;
        }
        if (chart) chart.destroy();
        const padding = (globalMax > globalMin) ? (globalMax - globalMin) * 0.05 : 86_400_000; // 1 day padding if range tiny
        const minDomain = isFinite(globalMin) ? globalMin - padding : undefined;
        const maxDomain = isFinite(globalMax) ? globalMax + padding : undefined;
        chart = new Chart(chartCtx, {
          type: 'line',
          data: { datasets },
          options: {
            animation:false,
            scales:{
              x:{
                type:'time',
                time:{unit:'day', displayFormats:{day:'MMM d yyyy', month:'MMM yyyy', year:'yyyy'}},
                title:{display:true,text:'Time'},
                min: minDomain,
                max: maxDomain,
              },
              y:{type:'category', title:{display:true,text:'Sourcetype'}}
            },
            plugins:{legend:{display:false}},
            elements:{line:{stepped:false}}
          }
        });
      }

      function renderTable(summary, selected) {
        const tbody = document.querySelector('#summary-table tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        for (const st of summary.sourcetypes) {
          const tr = document.createElement('tr');
          const hasRaw = (st.raw_fields && st.raw_fields.length>0);
          tr.innerHTML = `
            <td>${st.sourcetype}</td>
            <td>${st.time_range.min || '?'}</td>
            <td>${st.time_range.max || '?'}</td>
            <td>${st.total_events || 0}</td>
            <td style="text-align:center;">${hasRaw ? '✅' : '⚠️'}</td>
          `;
          tbody.appendChild(tr);
        }
      }

      function renderControls(summary, selected) {
        const div = document.getElementById('st-list');
        if (!div) return;
        div.innerHTML = '';
        for (const st of summary.sourcetypes) {
          const id = `st-${st.sourcetype.replace(/[^a-zA-Z0-9_-]/g,'_')}`;
          const wrap = document.createElement('label');
          wrap.style.display = 'flex';
          wrap.style.alignItems = 'center';
          wrap.style.gap = '4px';
          wrap.innerHTML = `<input type="checkbox" id="${id}" ${selected.has(st.sourcetype)?'checked':''}> ${st.sourcetype}`;
          wrap.querySelector('input').addEventListener('change', (e) => {
            if (e.target.checked) currentSelected.add(st.sourcetype); else currentSelected.delete(st.sourcetype);
            renderChart(summary, currentSelected);
            renderTable(summary, currentSelected);
          });
          div.appendChild(wrap);
        }
      }

      let currentSelected = new Set();

      function loadSummary() {
        const showError = (msg) => {
          const out = document.getElementById('live-output');
          if (out) out.textContent = msg;
        };
        fetch('/scan/summary')
          .then(r => r.ok ? r.json() : r.json().then(j => {throw new Error(j.error || 'failed');}))
          .catch(() => fetch('/scan/progress').then(r => r.json()))
          .then(summary => {
            if (summary && summary.summary) summary = summary.summary;
            if (!summary || !summary.sourcetypes) {
              showError('No summary yet. Run a scan.');
              return;
            }
            cachedSummary = summary;
            currentSelected = new Set(summary.sourcetypes.slice(0,12).map(s => s.sourcetype));
            renderControls(summary, currentSelected);
            renderTable(summary, currentSelected);
            renderChart(summary, currentSelected);
            activate('analyze');
            summaryLoaded = true;
          })
          .catch(err => {
            showError('Failed to load summary: ' + err);
          });
      }

      // Only load summary when Analyze is first opened; see activate()
    </script>
    """


@app.route("/analyze")
def analyze():
    return redirect(url_for("scan"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
