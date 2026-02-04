import json
import os
import requests
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep

from env_utils import load_env, require_env


load_env()

def parse_dt_env(var: str, default: datetime) -> datetime:
    """
    Parse an ISO 8601 datetime from env (YYYY-MM-DDTHH:MM:SS); fallback to default if unset.
    """
    value = os.getenv(var)
    if not value:
        return default
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable '{var}' must be in ISO format YYYY-MM-DDTHH:MM:SS (got: {value})"
        ) from exc

# Base REST API URL and credentials are now sourced from the environment
BASE = require_env("SPLUNK_BASE_URL")
AUTH = (require_env("SPLUNK_USERNAME"), require_env("SPLUNK_PASSWORD"))
SEARCH_TEMPLATE = require_env("SPLUNK_SEARCH")  # Base search; time bounds injected per window
FIELDS_CLAUSE = os.getenv("SPLUNK_FIELDS", "").strip()  # Optional field list appended as "| fields ..."

# Configurable output directory for exports
EXPORTDIR = Path(os.getenv("SPLUNK_EXPORT_DIR", "export_bots_data4"))
START = parse_dt_env("SPLUNK_EARLIEST", datetime(2020, 7, 18, 0, 0, 0))
END = parse_dt_env("SPLUNK_LATEST", datetime(2020, 9, 29, 0, 0, 0))
STEP_HOURS = float(os.getenv("SPLUNK_STEP_HOURS", "1"))
if STEP_HOURS <= 0:
    raise RuntimeError("SPLUNK_STEP_HOURS must be a positive number of hours")
STEP = timedelta(hours=STEP_HOURS)
MAX_RESULTS_PER_PAGE = 50000  # unused in export mode, kept for backward compatibility

requests.packages.urllib3.disable_warnings()  # disable TLS warnings for self-signed Splunk certs


def parse_keys(xml_text: str) -> dict:
    """Parse key/value pairs from a Splunk XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    data = {}
    for key in root.findall(".//{*}key"):
        name = key.attrib.get("name")
        if name:
            data[name] = key.text
    return data


def parse_messages(xml_text: str) -> list[str]:
    """Extract messages from a Splunk XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    messages = []
    for msg in root.findall(".//{*}msg"):
        text = (msg.text or "").strip()
        msg_type = msg.attrib.get("type")
        messages.append(f"{msg_type}: {text}" if msg_type else text)
    return messages


def format_percent(value: str | None) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_splunk_time(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y:%H:%M:%S")


def build_search(start: datetime, end: datetime) -> str:
    """
    Combine base search string with per-window earliest/latest bounds,
    inserting the bounds before the first pipe if present, and append fields clause if set.
    """
    earliest = format_splunk_time(start)
    latest = format_splunk_time(end)

    base = SEARCH_TEMPLATE.strip()
    # Ensure a single leading "search" keyword regardless of what the env contains
    if base.lower().startswith("search "):
        base = base.split(" ", 1)[1].lstrip()
    base = f"search {base}"

    if "|" in base:
        prefix, suffix = base.split("|", 1)
        prefix = prefix.rstrip()
        suffix = "| " + suffix.lstrip()
    else:
        prefix, suffix = base, ""

    # Append fields clause (ensure single leading pipe); env should only contain field names
    fields_clause = FIELDS_CLAUSE
    if fields_clause:
        # If user accidentally included the word "fields", strip it
        if fields_clause.lower().startswith("fields "):
            fields_clause = fields_clause.split(" ", 1)[1].strip()
        fields_clause = " ".join(fields_clause.split())  # normalize spacing
        suffix = f"{suffix} | fields {fields_clause}".strip()

    return f"{prefix} earliest={earliest} latest={latest} {suffix}".strip()


def filename_for_window(start: datetime, end: datetime) -> Path:
    s = start.strftime("%Y%m%d_%H%M")
    e = end.strftime("%Y%m%d_%H%M")
    return EXPORTDIR / f"events_{s}_to_{e}.jsonl"


def run_window(start: datetime, end: datetime, idx: int, total: int) -> int:
    search = build_search(start, end)

    EXPORTDIR.mkdir(exist_ok=True)

    out_path = filename_for_window(start, end)
    meta_path = out_path.with_suffix(".meta.json")
    print(f"[{idx}/{total}] Streaming results to {out_path} via /search/jobs/export ...")
    export_url = f"{BASE}/services/search/jobs/export"
    total_rows = 0
    messages: list[str] = []
    metadata: dict = {"mode": "export"}
    with requests.post(
        export_url,
        auth=AUTH,
        data={
            "search": search,
            "output_mode": "json",
        },
        stream=True,
        verify=False,
    ) as resp:
        resp.raise_for_status()
        with open(out_path, "w", encoding="utf-8") as f:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # HEC-style export returns {"preview":false,"result":{...}} lines
                if "result" in payload:
                    json.dump(payload["result"], f)
                    f.write("\n")
                    total_rows += 1
                elif "messages" in payload:
                    messages.extend(payload.get("messages") or [])

    if total_rows == 0:
        print(f"[{idx}/{total}] No events for window; skipping file write.")
        if out_path.exists():
            out_path.unlink()
        return 0

    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(
            {
                "metadata": metadata,
                "messages": messages,
                "total_rows": total_rows,
                "search": search,
            },
            mf,
        )

    print(f"[{idx}/{total}] Download complete: {out_path}")
    return total_rows


def main() -> None:
    windows = []
    current = START
    while current < END:
        nxt = min(current + STEP, END)
        windows.append((current, nxt))
        current = nxt

    total = len(windows)
    print(f"Preparing {total} windows between {START} and {END}...")

    grand_total = 0
    for i, (start, end) in enumerate(windows, start=1):
        grand_total += run_window(start, end, i, total)

    print(f"All windows processed. Total events downloaded: {grand_total}")


if __name__ == "__main__":
    main()
