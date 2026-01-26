#!/usr/bin/env python3
import glob
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # suppress SSL warnings when VERIFY_SSL is False
import gzip

from env_utils import load_env, require_env

load_env()

# =========================
# CONFIG (edit these only)
# =========================

# Input files
INPUT_GLOB = os.getenv("HEC_INPUT_GLOB", "shifted/*.jsonl")

# HEC endpoint (usually https://<splunk-host>:8088)
HEC_BASE = require_env("SPLUNK_HEC_BASE_URL")
HEC_TOKEN = require_env("SPLUNK_HEC_TOKEN")

# HEC path (most common)
HEC_PATH = "/services/collector/event"

# Default index (optional). If None, HEC token's default index must be set in Splunk.
DEFAULT_INDEX = None  # e.g. "main"

# SSL verification
# Set to False to skip certificate validation (useful for IP-based/self-signed HEC).
VERIFY_SSL = False
REQUEST_TIMEOUT = (10, 30)  # (connect timeout, read timeout)

# Sending behavior
BATCH_SIZE = 5000         # number of events per request (tune per env/limits)
MAX_PAYLOAD_BYTES = 5_000_000  # safety cap per request before gzip (HEC default max is often ~1MB; increase if allowed)
ENABLE_GZIP = True        # compress payload to push more events per request
SLEEP_ON_429_SECONDS = 2  # backoff on HEC throttling
MAX_RETRIES = 5

# What to send as "event" to HEC:
# - "raw": send the original _raw string (recommended for sourcetype-based parsing)
# - "json": if _raw is JSON, send parsed object; else send raw string
EVENT_MODE = "raw"  # "raw" or "json"

# Checkpoint/resume
CHECKPOINT_FILE = ".hec_checkpoint.json"
FLUSH_CHECKPOINT_EVERY_BATCH = True

# Optional: drop Splunk internal metadata fields before sending (keeps payload smaller)
DROP_FIELDS = {"_bkt", "_cd", "_indextime", "_serial", "_si", "_sourcetype", "_subsecond"}

# =========================
# END CONFIG
# =========================


def iso_to_epoch_seconds(iso_str: str) -> Optional[float]:
    """
    Convert:
      2020-08-18T19:59:59.975+00:00  -> epoch seconds (float)
    """
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def hec_post(
    session: requests.Session,
    url: str,
    headers: dict,
    payload_bytes: bytes,
    use_gzip: bool = False,
) -> None:
    """
    POST with retries. Raises on final failure.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req_headers = dict(headers)
            if use_gzip:
                req_headers["Content-Encoding"] = "gzip"

            r = session.post(
                url,
                headers=req_headers,
                data=payload_bytes,
                timeout=REQUEST_TIMEOUT,
                verify=VERIFY_SSL,
            )

            # throttling
            if r.status_code == 429:
                time.sleep(SLEEP_ON_429_SECONDS)
                continue

            r.raise_for_status()

            # HEC returns {"text":"Success","code":0} per event batch
            # Some errors still return 200 with code != 0, so check body:
            try:
                resp = r.json()
                if isinstance(resp, dict) and resp.get("code", 0) != 0:
                    raise RuntimeError(f"HEC error: {resp}")
            except json.JSONDecodeError:
                # if it's not JSON, still accept 2xx unless you want strictness
                pass

            return

        except Exception as e:
            last_err = e
            # exponential-ish backoff
            time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(f"Failed to POST to HEC after {MAX_RETRIES} retries: {last_err}")


def load_checkpoint() -> Tuple[Optional[str], int]:
    if not os.path.exists(CHECKPOINT_FILE):
        return None, 0
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("file"), int(obj.get("line", 0))
    except Exception:
        return None, 0


def save_checkpoint(current_file: str, line_no: int) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"file": current_file, "line": line_no}, f)


def build_hec_event(ev: dict) -> dict:
    """
    Convert your JSONL event into a HEC event payload.
    """
    # Copy and optionally drop internal fields
    ev2 = {k: v for k, v in ev.items() if k not in DROP_FIELDS}

    st = ev2.get("sourcetype")
    src = ev2.get("source")
    host = ev2.get("host")
    t_iso = ev2.get("_time")

    # Event body
    raw = ev2.get("_raw", "")
    if EVENT_MODE == "json":
        # If _raw is JSON, parse it; otherwise keep string
        body = raw
        if isinstance(raw, str):
            s = raw.lstrip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    body = json.loads(raw)
                except Exception:
                    body = raw
    else:
        body = raw  # raw string

    hec = {
        "event": body,
    }

    # time (epoch seconds) – this is the timestamp Splunk will use
    if isinstance(t_iso, str):
        t_epoch = iso_to_epoch_seconds(t_iso)
        if t_epoch is not None:
            hec["time"] = t_epoch

    # metadata (Splunk uses these at index time)
    if host:
        hec["host"] = host
    if src:
        hec["source"] = src
    if st:
        hec["sourcetype"] = st
    if DEFAULT_INDEX:
        hec["index"] = DEFAULT_INDEX

    return hec


def main():
    files = sorted(glob.glob(INPUT_GLOB))
    if not files:
        raise SystemExit(f"No files matched {INPUT_GLOB}")

    hec_url = HEC_BASE.rstrip("/") + HEC_PATH
    headers = {
        "Authorization": f"Splunk {HEC_TOKEN}",
        "Content-Type": "application/json",
    }

    ck_file, ck_line = load_checkpoint()

    session = requests.Session()

    total_sent = 0
    for path in files:
        # resume logic
        if ck_file and path < ck_file:
            continue

        start_line = 1
        if ck_file == path:
            start_line = ck_line + 1

        print(f"\n== Processing {path} (starting at line {start_line})")

        batch_lines = []
        current_bytes = 0
        sent_this_file = 0

        def flush_batch(last_line_no: int) -> None:
            nonlocal batch_lines, current_bytes, total_sent, sent_this_file
            if not batch_lines:
                return
            payload_str = "\n".join(batch_lines)
            payload_bytes = payload_str.encode("utf-8")
            if ENABLE_GZIP:
                payload_bytes = gzip.compress(payload_bytes)

            hec_post(session, hec_url, headers, payload_bytes, use_gzip=ENABLE_GZIP)

            count = len(batch_lines)
            total_sent += count
            sent_this_file += count
            print(f"  sent {sent_this_file} (total {total_sent})", end="\r")

            if FLUSH_CHECKPOINT_EVERY_BATCH:
                save_checkpoint(path, last_line_no)

            batch_lines = []
            current_bytes = 0

        line_no = start_line - 1
        last_line_seen = start_line - 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if line_no < start_line:
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        ev = json.loads(line)
                    except Exception:
                        # skip bad lines
                        continue

                    last_line_seen = line_no

                    ev_str = json.dumps(build_hec_event(ev), ensure_ascii=False)
                    ev_size = len(ev_str.encode("utf-8")) + 1  # +1 for newline

                    # If adding this event would exceed limits, flush first
                    if batch_lines and (
                        len(batch_lines) >= BATCH_SIZE or current_bytes + ev_size > MAX_PAYLOAD_BYTES
                    ):
                        flush_batch(line_no - 1)

                    batch_lines.append(ev_str)
                    current_bytes += ev_size

                # flush remainder
                flush_batch(line_no)
        except KeyboardInterrupt:
            save_checkpoint(path, last_line_seen)
            print(f"\nKeyboardInterrupt detected. Checkpoint saved at {path}:{last_line_seen}. Rerun to resume.")
            raise SystemExit(130)

        # file complete → checkpoint to end-of-file
        save_checkpoint(path, line_no)
        print(f"\n  Done {path}: sent {sent_this_file} events")

    print(f"\nAll done. Total sent: {total_sent}")
    print(f"Checkpoint file: {CHECKPOINT_FILE}")


if __name__ == "__main__":
    main()
