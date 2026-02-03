# _timewarp

Meet _timewarp: a straight-shooting, nerd-approved toolkit to pull Splunk data, inspect it, shift timestamps, and push it back in via HEC. Secrets and paths live in `.env` so the repo stays clean and shareable.

The toolchain comes in four parts:
1) Downloader – grabs your Splunk search results.
2) Sourcetype scanner – shows what’s inside and when.
3) Timeshifter – moves timestamps through time on purpose.
4) Uploader – sends it back via HEC.

## Prerequisites
- Python 3.12+
- Install deps: `pip install requests urllib3`

## Using uv (recommended)
`uv` keeps installs fast and isolated.
1) Install uv (one-time): `pip install uv`.
2) Create a virtualenv: `uv venv` then `source .venv/bin/activate`.
3) Install deps into that env: `uv pip install requests urllib3`.
4) Run the scripts via `uv run python import_data.py` (same for the other Python entrypoints).

## Quick start
1) Copy `.env.example` to `.env` and fill in your Splunk connection details and file globs.
2) Download from the Splunk REST API: `python import_data.py` (writes JSONL windows to `SPLUNK_EXPORT_DIR`).
3) Optional: scan sourcetypes and time ranges: `python scan_sourcetypes_and_dates.py`.
4) Time-shift event timestamps: `python shift_timestamps.py` (reads `SHIFT_INPUT_GLOB`, writes to `SHIFT_OUTPUT_DIR`).
5) Upload shifted data to HEC: `python upload_data_hec.py` (reads `HEC_INPUT_GLOB`, posts to `SPLUNK_HEC_BASE_URL` with `SPLUNK_HEC_TOKEN`).

## Web UI (all steps in one place)
- Start: `python webapp.py`
- Open: http://localhost:5000
- Use the sidebar to run Download → Scan → Time-shift → Upload without retyping paths/credentials; forms prefill from `.env`/`.env.example` and remembered overrides.

## Environment variables (from `.env`)
- Download (import_data.py): `SPLUNK_BASE_URL`, `SPLUNK_USERNAME`, `SPLUNK_PASSWORD`, `SPLUNK_EXPORT_DIR`
  - Query template: `SPLUNK_SEARCH` (base search string without the `search` keyword; code injects it plus per-window earliest/latest)
  - Optional fields clause: `SPLUNK_FIELDS` (space-separated field names; code prepends `fields`)
  - Window bounds: `SPLUNK_EARLIEST`, `SPLUNK_LATEST` (ISO `YYYY-MM-DDTHH:MM:SS`)
  - Window size: `SPLUNK_STEP_HOURS` (hours per search window)
- Scan (scan_sourcetypes_and_dates.py): `SCAN_INPUT_GLOB`
  - Histogram bucket size: `SCAN_BUCKET_SECONDS` (default 86400)
- Time-shift (shift_timestamps.py): `SHIFT_INPUT_GLOB`, `SHIFT_OUTPUT_DIR`
  - Shift targets: `SHIFT_TARGET_EARLIEST_ISO` (absolute) or `SHIFT_TARGET_EARLIEST_NOW_MINUS_DAYS` (relative fallback)
  - Rewrite aggressiveness: `SHIFT_AGGRESSIVE_RAW_REWRITE` (`true`/`false`)
- Upload (upload_data_hec.py): `SPLUNK_HEC_BASE_URL`, `SPLUNK_HEC_TOKEN`, `HEC_INPUT_GLOB`

## Web UI (Flask)
Prefer a browser? Run the same flows via `webapp.py`:
1) Install deps (once): `pip install -r <(python - <<'PY'\nimport tomllib,sys\nprint('\\n'.join(tomllib.load(open(\"pyproject.toml\",\"rb\"))['project']['dependencies']))\nPY)` or `pip install flask`.
2) Start the app: `python webapp.py` (defaults to port 5000).
3) Open `http://localhost:5000` for screens covering download, time-scan (with built-in Analyze tab), time-shift, and upload. The Analyze tab visualizes sourcetype ranges and raw timestamp detection without writing extra files.

## Notes
- `upload_data_hec.py` maintains a checkpoint in `.hec_checkpoint.json` so reruns resume where they left off.
- SSL verification to HEC is disabled by default in the script; set `VERIFY_SSL` to `True` in code if you use valid certs.
