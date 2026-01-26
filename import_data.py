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

# Base REST API URL and credentials are now sourced from the environment
BASE = require_env("SPLUNK_BASE_URL")
AUTH = (require_env("SPLUNK_USERNAME"), require_env("SPLUNK_PASSWORD"))

# Configurable output directory for exports
EXPORTDIR = Path(os.getenv("SPLUNK_EXPORT_DIR", "export_bots_data4"))
START = datetime(2020, 7, 18, 0, 0, 0)
END = datetime(2020, 9, 29, 0, 0, 0)
STEP = timedelta(hours=1)
MAX_RESULTS_PER_PAGE = 50000  # Splunk REST API caps a single page at 50k rows

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


def filename_for_window(start: datetime, end: datetime) -> Path:
    s = start.strftime("%Y%m%d_%H%M")
    e = end.strftime("%Y%m%d_%H%M")
    return EXPORTDIR / f"events_{s}_to_{e}.jsonl"


def run_window(start: datetime, end: datetime, idx: int, total: int) -> int:
    search = (
        f"search index=main earliest={format_splunk_time(start)} latest={format_splunk_time(end)} "
        "| fields _time host source sourcetype _raw"
    )

    EXPORTDIR.mkdir(exist_ok=True)

    print(f"[{idx}/{total}] Creating search job for {start} -> {end}")
    job_creation_url = f"{BASE}/services/search/jobs"
    print(f"POST {job_creation_url}")
    r = requests.post(
        job_creation_url,
        auth=AUTH,
        data={"search": search, "exec_mode": "normal"},
        verify=False,
    )
    r.raise_for_status()
    sid = r.text.split("<sid>")[1].split("</sid>")[0]
    print(f"[{idx}/{total}] Job SID: {sid}")

    # wait
    print(f"[{idx}/{total}] Polling job status...")
    while True:
        status_url = f"{BASE}/services/search/jobs/{sid}"
        print(f"GET {status_url}")
        s = requests.get(
            status_url,
            auth=AUTH,
            verify=False,
        ).text
        info = parse_keys(s)
        progress = format_percent(info.get("doneProgress"))
        state = info.get("dispatchState", "unknown")
        run_duration = info.get("runDuration", "?")
        print(f"[{idx}/{total}] State={state}, progress={progress}, runtime={run_duration}s")

        if info.get("isDone") == "1":
            break

        sleep(2)

    if info.get("isFailed") == "1" or state.upper() == "FAILED":
        print(f"[{idx}/{total}] Job failed.")
        log_url = f"{BASE}/services/search/jobs/{sid}/search.log"
        print(f"GET {log_url}")
        log_resp = requests.get(
            log_url,
            auth=AUTH,
            verify=False,
        )
        if log_resp.ok:
            print("Tail of search.log:")
            for line in log_resp.text.strip().splitlines()[-20:]:
                print(line)
        else:
            print(f"Could not fetch search.log (HTTP {log_resp.status_code})")
        return

    out_path = filename_for_window(start, end)
    meta_path = out_path.with_suffix(".meta.json")
    print(f"[{idx}/{total}] Job finished, downloading results to {out_path} (JSONL)...")
    results_url = f"{BASE}/services/search/jobs/{sid}/results"
    print(f"GET {results_url} (paged to avoid 50k row limit)")

    offset = 0
    total_rows = 0
    messages: list[dict] = []
    metadata: dict = {}

    f = None
    try:
        while True:
            params = {
                "output_mode": "json",
                "count": MAX_RESULTS_PER_PAGE,
                "offset": offset,
            }
            r = requests.get(
                results_url,
                auth=AUTH,
                params=params,
                verify=False,
            )
            if not r.ok:
                print(f"[{idx}/{total}] Failed at offset {offset}: HTTP {r.status_code}")
                print(r.text)
                break

            payload = r.json()
            if not metadata:
                metadata = {k: v for k, v in payload.items() if k not in {"results", "messages"}}

            messages.extend(payload.get("messages") or [])

            batch = payload.get("results") or []
            if not batch:
                break

            if f is None:
                f = open(out_path, "w", encoding="utf-8")

            for row in batch:
                json.dump(row, f)
                f.write("\n")

            batch_size = len(batch)
            total_rows += batch_size
            offset += batch_size
            print(f"[{idx}/{total}] Downloaded {total_rows} rows so far...")

            if batch_size < MAX_RESULTS_PER_PAGE:
                break  # last page
    finally:
        if f:
            f.close()

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
    print(f"Preparing {total} four-hour windows between {START} and {END}...")

    grand_total = 0
    for i, (start, end) in enumerate(windows, start=1):
        grand_total += run_window(start, end, i, total)

    print(f"All windows processed. Total events downloaded: {grand_total}")


if __name__ == "__main__":
    main()
