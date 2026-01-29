#!/usr/bin/env python3
from __future__ import annotations

import importlib
import io
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Thread, Lock
from typing import Any, Dict, List

from base64 import b64encode
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from env_utils import load_env

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_env()

def read_env_file(path: Path) -> dict:
    if not path.exists():
        return {}
    data = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("#"):
            v = ""
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        data[k] = v
    return data

ENV_EXAMPLE_DEFAULTS = read_env_file(BASE_DIR / ".env.example")

TASKS: Dict[str, dict] = {
    "scan": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "summary": None, "lock": Lock()},
    "download": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "summary": None, "lock": Lock()},
    "shift": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "summary": None, "lock": Lock()},
    "upload": {"log": [], "running": False, "proc": None, "progress": None, "counts": None, "summary": None, "lock": Lock()},
}

@contextmanager
def temp_environ(updates: Dict[str, str]):
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
        state["summary"] = None
        state["running"] = True

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
                    m = re.search(r"\[(\d+)/(\d+)\]", line)
                    if m:
                        cur, total = int(m.group(1)), int(m.group(2))
                        state["counts"] = (cur, total)
                        state["progress"] = min(1.0, cur / total) if total else None
                    if line.startswith("SCAN_SUMMARY_JSON::"):
                        try:
                            summary = json.loads(line.split("SCAN_SUMMARY_JSON::", 1)[1])
                            state["summary"] = summary
                        except Exception:
                            pass
            proc.wait()
        finally:
            with state["lock"]:
                state["running"] = False

    Thread(target=reader, daemon=True).start()


def task_progress(name: str) -> dict:
    state = _task_state(name)
    with state["lock"]:
        return {
            "running": state["running"],
            "log": "".join(state["log"]),
            "progress": state["progress"],
            "counts": state["counts"],
            "summary": state.get("summary"),
        }


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


PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>_timewarp Web UI</title>
  <link rel="icon" type="image/png" href="{{ url_for('static', filename='favicon.png') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/tokens.css') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='css/app.css') }}">
  <script>
    document.addEventListener('DOMContentLoaded', () => {
      const form = document.querySelector('form');
      const progress = document.getElementById('progress');
      const stopBtn = document.getElementById('stop-btn');
      const helpButtons = document.querySelectorAll('.help-btn');

      const showHelp = (text) => {
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = `
          <div class="modal__content">
            <div style="font-weight:700; margin-bottom:8px;">Field help</div>
            <div style="color:var(--text-2); line-height:1.5;">${text || 'No description available.'}</div>
            <div class="modal__actions">
              <button class="btn primary" type="button" id="help-close">Close</button>
            </div>
          </div>`;
        const close = () => modal.remove();
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        modal.querySelector('#help-close').addEventListener('click', close);
        document.addEventListener('keydown', function esc(e){ if (e.key === 'Escape'){ close(); document.removeEventListener('keydown', esc);}}, {once:true});
        document.body.appendChild(modal);
      };

      helpButtons.forEach(btn => {
        btn.addEventListener('click', () => showHelp(btn.dataset.help));
      });
      if (form) {
        form.addEventListener('submit', () => {
          form.querySelectorAll('button').forEach(el => el.disabled = true);
          form.querySelectorAll('input[type="text"], input[type="number"], input[type="password"]').forEach(el => el.readOnly = true);
          if (progress) progress.classList.remove('hidden');
          if (output && !output.textContent.trim()) output.textContent = 'Running...';
          if (stopBtn) stopBtn.classList.remove('hidden');
        });
      }

      const taskSlug = window.taskSlug || null;
      if (taskSlug) {
        const poll = () => {
          const output = document.getElementById('live-output');
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
              if (data.log && data.log.length) {
                output.textContent = data.log;
              } else if (data.running) {
                output.textContent = '(no output yet; task is running...)';
              } else {
                output.textContent = output.textContent || '(no output captured)';
              }
              output.scrollTop = output.scrollHeight;
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
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
</head>
<body>
  <script>window.taskSlug = "{{ task_slug or '' }}";</script>
  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar__brand">_timewarp</div>
      <div class="sidebar__hero"><img src="{{ url_for('static', filename='delorean.png') }}" alt="DeLorean"></div>
      <div class="sidebar__section">Navigate</div>
      <ul class="nav">
        <li><a href="{{ url_for('home') }}" class="{{ 'active' if request.endpoint=='home' else '' }}"><span>Home</span></a></li>
        <li><a href="{{ url_for('download') }}" class="{{ 'active' if request.endpoint=='download' else '' }}"><span>Download</span></a></li>
        <li><a href="{{ url_for('scan') }}" class="{{ 'active' if request.endpoint=='scan' else '' }}"><span>Time-scan</span></a></li>
        <li><a href="{{ url_for('shift') }}" class="{{ 'active' if request.endpoint=='shift' else '' }}"><span>Time-shift</span></a></li>
        <li><a href="{{ url_for('upload') }}" class="{{ 'active' if request.endpoint=='upload' else '' }}"><span>Upload</span></a></li>
      </ul>
      <div class="sidebar__bottom" style="justify-content:flex-start; padding-left:4px;">
        <a href="{{ url_for('robbybird') }}" title="Robbybird" style="color:var(--status-warn);">&#128038;</a>
      </div>
    </aside>
    <main class="content">
      <div class="panel">
        <div class="panel__header">{{ title }}{% if title_hint %}<span class="title-hint" title="{{ title_hint }}">?</span>{% endif %}</div>
        <div class="panel__body">
          {% if fields %}
          <form method="post" class="form">
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;">
              {% for f in fields %}
              <div class="form-group">
                <label for="{{ f.name }}">{{ f.label }}</label>
                {% if f.type == "checkbox" %}
                  <div class="input-with-help">
                    <input type="checkbox" name="{{ f.name }}" id="{{ f.name }}" value="true" title="{{ f.hint or '' }}" {% if f.value in ['true','True','1','on'] %}checked{% endif %}>
                    <button class="help-btn" type="button" data-help="{{ f.hint or 'No description yet.' }}">?</button>
                  </div>
                {% else %}
                  <div class="input-with-help">
                    <input type="{{ f.type }}" name="{{ f.name }}" id="{{ f.name }}" value="{{ f.value|e }}" placeholder="{{ f.placeholder or '' }}" title="{{ f.hint or '' }}" {% if f.step %}step="{{ f.step }}"{% endif %}>
                    <button class="help-btn" type="button" data-help="{{ f.hint or 'No description yet.' }}">?</button>
                  </div>
                {% endif %}
              </div>
              {% endfor %}
            </div>
            <div style="margin-top:10px; display:flex; gap:8px;">
              <button class="btn primary" type="submit">{{ action }}</button>
              <a href="{{ request.path }}"><button class="btn" type="button">Reset</button></a>
            </div>
          </form>
          {% endif %}

          {% if show_tabs %}
            <div style="display:flex;gap:8px;margin:12px 0;">
              <button id="tab-logs" class="btn secondary" type="button">Logs</button>
              <button id="tab-timeline" class="btn secondary" type="button">Timeline</button>
              <button id="tab-table" class="btn secondary" type="button">Table</button>
            </div>
            <div id="tab-content"></div>
          {% else %}
            <div id="progress" class="progress hidden"><div id="progress-bar" class="bar"></div></div>
            <pre id="live-output" class="log-area">{{ result if result is not none else '' }}</pre>
          {% endif %}

          {% if result is not none and success is not none %}
            <div style="margin-top:8px;color:{{ 'var(--status-success)' if success else 'var(--status-error)' }};">
              {{ 'Completed' if success else 'Failed' }}
            </div>
          {% endif %}

          {% if extra %}
            {{ extra|safe }}
          {% endif %}
        </div>
      </div>
    </main>
  </div>
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
    show_tabs: bool = False,
    title_hint: str | None = None,
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
        title_hint=title_hint,
        show_tabs=show_tabs,
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


def build_scan_extra() -> str:
    return """
    <script>
      const tabs = {
        logs: document.getElementById('tab-logs'),
        timeline: document.getElementById('tab-timeline'),
        table: document.getElementById('tab-table'),
      };
      const tabContent = document.getElementById('tab-content');
      let activeTab = 'logs';
      let cachedSummary = null;
      let currentSelected = new Set();

      function setActiveTab(name) {
        activeTab = (name === 'logs' || name === 'timeline' || name === 'table') ? name : 'logs';
        console.log('tab click', activeTab);
        Object.entries(tabs).forEach(([k, btn]) => {
          if (!btn) return;
          btn.classList.toggle('secondary', k !== activeTab);
          btn.classList.toggle('primary', k === activeTab);
        });
        renderActive();
      }

      function ensureSummary(cb) {
        if (cachedSummary) { cb(cachedSummary); return; }
        fetch('/scan/summary')
          .then(r => r.ok ? r.json() : r.json().then(j=>{throw new Error(j.error||'failed');}))
          .catch(() => fetch('/scan/progress').then(r=>r.json()))
          .then(summary => {
            if (summary && summary.summary) summary = summary.summary;
            if (!summary || !summary.sourcetypes) throw new Error('No summary yet. Run a scan.');
            cachedSummary = summary;
            currentSelected = new Set(summary.sourcetypes.slice(0,12).map(s=>s.sourcetype));
            cb(summary);
          }).catch(err => {
            tabContent.innerHTML = `<pre class="log-area">${err}</pre>`;
          });
      }

      function renderLogs() {
        tabContent.innerHTML = '';
        const prog = document.createElement('div');
        prog.id = 'progress';
        prog.className = 'progress hidden';
        prog.innerHTML = '<div id="progress-bar" class="bar"></div>';
        const pre = document.createElement('pre');
        pre.id = 'live-output';
        pre.className = 'log-area';
        pre.style.maxHeight = '320px';
        pre.style.overflow = 'auto';
        tabContent.appendChild(prog);
        tabContent.appendChild(pre);
      }

      function renderTimeline(summary) {
        tabContent.innerHTML = '';
        const note = document.createElement('div');
        note.style.marginBottom = '8px';
        note.style.color = 'var(--text-3)';
        note.style.fontSize = '13px';
        note.innerHTML = 'Timeline uses <code>_time</code> min/max per sourcetype (not raw fields).';
        const wrapper = document.createElement('div');
        wrapper.className = 'tabs-vertical';
        const rail = document.createElement('div');
        rail.className = 'tab-rail';
        rail.id = 'st-rail';
        rail.style.maxHeight = '320px';
        rail.style.overflowY = 'auto';
        const list = document.createElement('div');
        list.id = 'st-list';
        rail.appendChild(list);
        const content = document.createElement('div');
        content.className = 'tab-content';
        const canvas = document.createElement('canvas');
        canvas.id = 'chart';
        canvas.height = 260;
        content.appendChild(canvas);
        wrapper.appendChild(rail);
        wrapper.appendChild(content);
        tabContent.appendChild(note);
        tabContent.appendChild(wrapper);
        renderControls(summary, currentSelected);
        renderChart(summary, currentSelected);
      }

      function renderTable(summary) {
        tabContent.innerHTML = '';
        const table = document.createElement('table');
        table.id = 'summary-table';
        table.innerHTML = '<thead><tr><th style="text-align:left;">Source</th><th>Sourcetype</th><th>Events</th></tr></thead><tbody></tbody>';
        tabContent.appendChild(table);
        const tbody = table.querySelector('tbody');
        tbody.innerHTML = '';
        const source = summary.input_glob || '';
        summary.sourcetypes.forEach(st => {
          const tr = document.createElement('tr');
          tr.innerHTML = `<td>${source}</td><td>${st.sourcetype}</td><td>${st.total_events || 0}</td>`;
          tbody.appendChild(tr);
        });
      }

      function renderControls(summary, selected) {
        const div = document.getElementById('st-list');
        if (!div) return;
        div.innerHTML = '';
        summary.sourcetypes.forEach(st => {
          const btn = document.createElement('button');
          btn.className = 'tab-btn' + (selected.has(st.sourcetype)?' active':'');
          btn.type = 'button';
          btn.textContent = st.sourcetype;
          btn.addEventListener('click', () => {
            if (selected.has(st.sourcetype)) selected.delete(st.sourcetype); else selected.add(st.sourcetype);
            renderControls(summary, selected);
            renderChart(summary, selected);
          });
          div.appendChild(btn);
        });
      }

      function renderChart(summary, selected) {
        const canvas = document.getElementById('chart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const datasets = [];
        const colors = ['#2fd4ff','#ff8a3d','#ff5fa8','#7ee0a3','#c084fc','#5eead4','#f2d16b','#8ab4f8','#ffa7c4','#fbbf24'];
        let ci = 0, globalMin = Infinity, globalMax = -Infinity;
        summary.sourcetypes.forEach(st => {
          if (!selected.has(st.sourcetype)) return;
          if (!st.time_range.min || !st.time_range.max) return;
          const start = new Date(st.time_range.min).getTime();
          const end = new Date(st.time_range.max).getTime();
          globalMin = Math.min(globalMin, start);
          globalMax = Math.max(globalMax, end);
          datasets.push({
            label: st.sourcetype,
            data: [{x:start,y:st.sourcetype},{x:end,y:st.sourcetype}],
            borderColor: colors[ci % colors.length],
            backgroundColor: colors[ci % colors.length],
            borderWidth: 8,
            pointRadius: 0,
            showLine: true,
          });
          ci++;
        });
        if (window.timelineChart) window.timelineChart.destroy();
        if (!datasets.length) return;
        const pad = (globalMax>globalMin) ? (globalMax-globalMin)*0.05 : 86400000;
        window.timelineChart = new Chart(ctx, {
          type:'line',
          data:{datasets},
          options:{
            animation:false,
            scales:{
              x:{type:'time', min:globalMin-pad, max:globalMax+pad, time:{unit:'day',displayFormats:{day:'MMM d yyyy',month:'MMM yyyy',year:'yyyy'}}, title:{display:true,text:'Time'}},
              y:{type:'category', title:{display:true,text:'Sourcetype'}}
            },
            plugins:{legend:{display:false}}
          }
        });
      }

      function renderActive() {
        const tab = activeTab || 'logs';
        if (tab === 'logs') { renderLogs(); return; }
        if (tab === 'timeline') { ensureSummary(renderTimeline); return; }
        if (tab === 'table') { ensureSummary(renderTable); return; }
        renderLogs();
      }

      if (tabs.logs) tabs.logs.addEventListener('click', () => setActiveTab('logs'));
      if (tabs.timeline) tabs.timeline.addEventListener('click', () => setActiveTab('timeline'));
      if (tabs.table) tabs.table.addEventListener('click', () => setActiveTab('table'));
      setActiveTab('logs');
    </script>
    """

# Flask app
app = Flask(__name__)

# Routes
@app.route("/")
def home():
    intro = """
    <div class="panel" style="margin-top:12px;">
      <div class="panel__header">What _timewarp does</div>
      <div class="panel__body">
        <p>_timewarp is a focused toolkit for moving Splunk data through a four-step loop: pull, inspect, shift, and push back.</p>
        <ul>
          <li><strong>Download</strong> &mdash; `import_data.py` runs REST searches (management port 8089) and writes JSONL windows to your export directory.</li>
          <li><strong>Time-scan</strong> &mdash; `scan_sourcetypes_and_dates.py` checks sourcetypes, earliest/latest ranges, and raw timestamp fields.</li>
          <li><strong>Time-shift</strong> &mdash; `shift_timestamps.py` pins the earliest event to a target time (or now-minus-N days) and adjusts known timestamp fields.</li>
          <li><strong>Upload</strong> &mdash; `upload_data_hec.py` streams shifted JSONL files into Splunk HEC (usually port 8088) with automatic checkpointing.</li>
        </ul>
        <p>Use the sidebar to jump into any step. The Time-scan page also shows live logs plus a timeline and table of sourcetype ranges so you can sanity-check that your data scanned correctly.</p>
      </div>
    </div>
    """
    return render_page("Welcome", fields=None, action=None, extra_html=intro)


@app.route("/download", methods=["GET", "POST"])
def download():
    fields = prefill([
        {"name": "SPLUNK_BASE_URL", "label": "Splunk Base URL", "type": "text", "hint": "https://<splunk-host>:8089 (management port for REST search)"},
        {"name": "SPLUNK_USERNAME", "label": "Username", "type": "text", "hint": "Splunk user with search privileges"},
        {"name": "SPLUNK_PASSWORD", "label": "Password", "type": "password", "hint": "Password for the Splunk search user"},
        {"name": "SPLUNK_SEARCH", "label": "Base search (no 'search')", "type": "text", "hint": "Base SPL (e.g. index=foo sourcetype=bar); _time bounds are injected automatically"},
        {"name": "SPLUNK_FIELDS", "label": "Fields (space separated)", "type": "text", "hint": "Optional list for '| fields'; leave blank to keep defaults"},
        {"name": "SPLUNK_EXPORT_DIR", "label": "Export dir", "type": "text", "hint": "Folder to write JSONL windows"},
        {"name": "SPLUNK_EARLIEST", "label": "Earliest (ISO)", "type": "text", "hint": "Start of the export window, ISO (YYYY-MM-DDTHH:MM:SS)"},
        {"name": "SPLUNK_LATEST", "label": "Latest (ISO)", "type": "text", "hint": "End of the export window, ISO (YYYY-MM-DDTHH:MM:SS)"},
        {"name": "SPLUNK_STEP_HOURS", "label": "Window size (hours)", "type": "number", "step": "0.25", "hint": "Duration of each search slice in hours"},
    ])
    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("download", "import_data.py", env_updates)
    return render_page("Download", fields, "Run download", task_slug="download", show_tabs=False)


@app.route("/scan", methods=["GET", "POST"])
def scan():
    fields = prefill([
        {"name": "SCAN_INPUT_GLOB", "label": "Input glob", "type": "text", "hint": "Glob for JSONL files to scan (e.g., export/*.jsonl)"},
    ])
    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("scan", "scan_sourcetypes_and_dates.py", env_updates)
    return render_page("Time-scan", fields, "Run scan", task_slug="scan", extra_html=build_scan_extra(), show_tabs=True)


@app.route("/shift", methods=["GET", "POST"])
def shift():
    fields = prefill([
        {"name": "SHIFT_INPUT_GLOB", "label": "Input glob", "type": "text", "hint": "Shift source files (JSONL)"},
        {"name": "SHIFT_OUTPUT_DIR", "label": "Output dir", "type": "text", "hint": "Where shifted files are written"},
        {"name": "SHIFT_TARGET_EARLIEST_ISO", "label": "Target earliest ISO (optional)", "type": "text", "hint": "Pin earliest _time to this ISO (e.g., 2024-01-01T00:00:00Z)"},
        {"name": "SHIFT_TARGET_EARLIEST_NOW_MINUS_DAYS", "label": "If no target ISO, use now minus N days", "type": "number", "step": "1", "hint": "Fallback offset if target ISO is empty"},
        {"name": "SHIFT_AGGRESSIVE_RAW_REWRITE", "label": "Aggressive raw rewrite", "type": "checkbox", "hint": "Also rewrite ISO-like strings in _raw beyond known keys"},
    ])
    if request.method == "POST":
        env_updates = {}
        for f in fields:
            if f["type"] == "checkbox":
                env_updates[f["name"]] = "true" if request.form.get(f["name"]) else "false"
            else:
                env_updates[f["name"]] = request.form.get(f["name"], "")
        start_task("shift", "shift_timestamps.py", env_updates)
    return render_page("Time-shift", fields, "Run shift", task_slug="shift", show_tabs=False)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    fields = prefill([
        {"name": "HEC_INPUT_GLOB", "label": "Input glob", "type": "text", "hint": "Shifted JSONL files to upload"},
        {"name": "SPLUNK_HEC_BASE_URL", "label": "HEC Base URL", "type": "text", "hint": "https://<hec-host>:8088"},
        {"name": "SPLUNK_HEC_TOKEN", "label": "HEC Token", "type": "password", "hint": "Splunk HEC token with ingest rights"},
    ])
    if request.method == "POST":
        env_updates = {f["name"]: request.form.get(f["name"], "") for f in fields}
        start_task("upload", "upload_data_hec.py", env_updates)
    return render_page("Upload", fields, "Run upload", task_slug="upload", show_tabs=False)

@app.route("/robbybird")
def robbybird():
    sprite_uri = None
    try:
        sprite_path = BASE_DIR / "static" / "robbybird.png"
        if sprite_path.exists():
            sprite_uri = "data:image/png;base64," + b64encode(sprite_path.read_bytes()).decode("ascii")
    except Exception:
        sprite_uri = None
    game_html = """
    <div class="panel" style="margin-top:12px;">
      <div class="panel__header">Robbybird</div>
      <div class="panel__body">
        <canvas id="game" width="800" height="540" style="border:1px solid var(--border-1); background: var(--surface-2); display:block; margin:auto;"></canvas>
        <div style="margin-top:8px; text-align:center; color:var(--text-3);">Click or press Space to flap. Avoid the pipes.</div>
        <div id="game-status" style="margin-top:6px;text-align:center;color:var(--text-3);font-size:12px;">Init...</div>
      </div>
    </div>
    <script>
      (() => {
        const sprite = "__SPRITE_URL__";
        const gravity = 0.20;
        const flapVel = -5.2;
        const pipeGap = 260;
        const pipeSpacing = 220; // frames

        function start() {
          const canvas = document.getElementById('game');
          if (!canvas) return;
          const ctx = canvas.getContext('2d');
          if (!ctx) { console.error('Canvas getContext failed'); return; }
          const status = document.getElementById('game-status');
          const setStatus = (msg) => { if (status) status.textContent = msg; };
          setStatus('Canvas ready; loading sprite...');
          console.log('Robbybird: start()');

          // Quick visibility check.
          const drawTest = () => {
            ctx.clearRect(0,0,canvas.width,canvas.height);
            ctx.fillStyle = '#ff5fa8';
            ctx.fillRect(20,20,120,60);
            ctx.strokeStyle = '#ff8a3d';
            ctx.lineWidth = 4;
            ctx.strokeRect(20,20,120,60);
            ctx.fillStyle = '#ffffff';
            ctx.font = '14px sans-serif';
            ctx.fillText('Canvas OK', 32, 55);
          };
          drawTest();

          const birdImg = new Image();
          let birdLoaded = false;
          birdImg.onload = function() { birdLoaded = true; setStatus('Sprite loaded. Click/space to flap.'); };
          birdImg.onerror = function(e) { setStatus('Sprite failed; using block instead.'); console.error('Sprite load error', e, 'URL:', sprite); };
          birdImg.src = sprite;

          let bird = { x: 100, y: canvas.height / 2, v: 0, w: 128, h: 96 };
          let pipes = [];
          let frame = 0;
          let alive = true;
          let loopStarted = false;
          let score = 0;

          const reset = () => {
            bird = { ...bird, y: canvas.height / 2, v: 0 };
            pipes = [];
            frame = 0;
            alive = true;
            score = 0;
            setStatus('Sprite loaded. Click/space to flap.');
          };

          const spawnPipe = () => {
            const top = 40 + Math.random() * (canvas.height - pipeGap - 120);
            pipes.push({ x: canvas.width, top, bottom: top + pipeGap, w: 54, passed:false });
            // cap simultaneous pipes to keep density low (drop newest if over cap)
            if (pipes.length > 3) pipes.pop();
          };

          const drawBg = () => {
            const grd = ctx.createLinearGradient(0, 0, 0, canvas.height);
            grd.addColorStop(0, '#101b2f');
            grd.addColorStop(1, '#0c1324');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
          };

          const drawBird = () => {
            if (birdLoaded && birdImg.naturalWidth) {
              ctx.drawImage(birdImg, bird.x, bird.y, bird.w, bird.h);
            } else {
              ctx.fillStyle = '#f6d14a';
              ctx.fillRect(bird.x, bird.y, bird.w, bird.h);
            }
          };

          const drawPipes = () => {
            ctx.fillStyle = '#1f3047';
            pipes.forEach(p => {
              ctx.fillRect(p.x, 0, p.w, p.top);
              ctx.fillRect(p.x, p.bottom, p.w, canvas.height - p.bottom);
            });
          };

          const collide = () => {
            // Use a smaller hitbox than the full sprite for friendlier play.
            const hb = {
              x: bird.x + bird.w * 0.2,
              y: bird.y + bird.h * 0.2,
              w: bird.w * 0.6,
              h: bird.h * 0.6,
            };
            if (hb.y < 0 || hb.y + hb.h > canvas.height) return true;
            return pipes.some(p => {
              const hitX = hb.x + hb.w > p.x && hb.x < p.x + p.w;
              const hitY = hb.y < p.top || hb.y + hb.h > p.bottom;
              return hitX && hitY;
            });
          };

          const loop = () => {
            loopStarted = true;
            frame++;
            bird.v += gravity;
            bird.y += bird.v;
            if (frame === 1 || frame % pipeSpacing === 0) spawnPipe();
            pipes.forEach(p => {
              p.x -= 0.95;
              if (!p.passed && bird.x > p.x + p.w) { p.passed = true; score += 1; }
            });
            pipes = pipes.filter(p => p.x + p.w > -60);

            drawBg();
            drawPipes();
            drawBird();
            // HUD
            ctx.fillStyle = '#e9eefc';
            ctx.font = '18px Inter, sans-serif';
            ctx.fillText('Score: ' + score, canvas.width - 140, 28);
            if (frame % 60 === 0) setStatus('Frame ' + frame + ' | Score ' + score);

            if (collide()) {
              alive = false;
              ctx.fillStyle = 'rgba(0,0,0,0.5)';
              ctx.fillRect(0, 0, canvas.width, canvas.height);
              ctx.fillStyle = '#fff';
              ctx.font = '16px Inter, sans-serif';
              ctx.fillText('Game over - click to restart', 60, canvas.height / 2);
              setStatus('Game over at frame ' + frame + '. Click/space to restart.');
              return;
            }
            requestAnimationFrame(loop);
          };

          const flap = () => {
            if (!alive) { reset(); requestAnimationFrame(loop); return; }
            bird.v = flapVel;
          };

          canvas.addEventListener('mousedown', flap);
          window.addEventListener('keydown', (e) => {
            if (e.code === 'Space') { e.preventDefault(); flap(); }
          });

          requestAnimationFrame(loop);

          // Watchdog: if loop never starts, run a fallback animation so user sees movement.
          setTimeout(() => {
            if (loopStarted) return;
            console.warn('Robbybird: main loop did not start, running fallback.');
            setStatus('Fallback animation running (main loop blocked)');
            let t = 0;
            (function fallback() {
              t++;
              ctx.clearRect(0,0,canvas.width,canvas.height);
              ctx.fillStyle = '#0f192a';
              ctx.fillRect(0,0,canvas.width,canvas.height);
              const g = ctx.createLinearGradient(0,0,canvas.width,0);
              g.addColorStop(0,'#ff5fa8'); g.addColorStop(1,'#2fd4ff');
              ctx.fillStyle = g;
              ctx.fillRect(0,30,canvas.width,40);
              ctx.fillStyle = '#ff8a3d';
              ctx.fillRect((t*3)%(canvas.width-50), 120, 50, 50);
              ctx.fillStyle = '#fff';
              ctx.font = '14px sans-serif';
              ctx.fillText('Fallback frame ' + t, 20, 210);
              requestAnimationFrame(fallback);
            })();
          }, 600);
        }

        function safeStart() {
          try { start(); }
          catch (err) {
            console.error(err);
            const status = document.getElementById('game-status');
            if (status) status.textContent = 'Error: ' + err;
          }
        }

        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', safeStart);
        } else {
          safeStart();
        }
      })();
    </script>
    """
    game_html = game_html.replace("__SPRITE_URL__", sprite_uri or url_for('static', filename='robbybird.png'))
    return render_page("Robbybird", fields=None, action=None, extra_html=game_html, show_tabs=False)


@app.route("/<task>/progress")
def task_progress_route(task: str):
    if task not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    return jsonify(task_progress(task))


@app.route("/<task>/stop", methods=["POST"])
def task_stop_route(task: str):
    if task not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    stop_task(task)
    return jsonify({"stopped": True})


@app.route("/scan/summary")
def scan_summary_route():
    if "scan" not in TASKS:
        return jsonify({"error": "unknown task"}), 404
    state = TASKS["scan"]
    with state["lock"]:
        summary = state.get("summary")
        if summary is None:
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
