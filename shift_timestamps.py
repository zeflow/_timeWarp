#!/usr/bin/env python3
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from env_utils import load_env

load_env()

# =========================
# CONFIG (edit these only)
# =========================

# Input/output
INPUT_GLOB = os.getenv("SHIFT_INPUT_GLOB", "export_bots_data4/*.jsonl")
OUT_DIR = os.getenv("SHIFT_OUTPUT_DIR", "shifted")

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


# How to choose the shift (env-configurable):
# Option A: pin earliest event to this absolute time (ISO)
# Example env: SHIFT_TARGET_EARLIEST_ISO=2025-12-01T00:00:00Z
TARGET_EARLIEST_TIME_ISO = os.getenv("SHIFT_TARGET_EARLIEST_ISO")

# Option B: pin earliest event to "now minus N days"
# Used when TARGET_EARLIEST_TIME_ISO is None
TARGET_EARLIEST_NOW_MINUS_DAYS = int(os.getenv("SHIFT_TARGET_EARLIEST_NOW_MINUS_DAYS", "7"))

# Raw timestamp rewriting behavior:
# False = only rewrite known timestamp keys (safer)
# True  = also rewrite any ISO-like string in raw JSON, and more XML Data fields (more aggressive)
AGGRESSIVE_RAW_REWRITE = env_bool("SHIFT_AGGRESSIVE_RAW_REWRITE", False)

# Keys in raw JSON that should be treated as timestamps (case-sensitive)
RAW_TIME_KEYS = {
    # Stream
    "timestamp", "endtime", "starttime", "time", "event_time",
    "start_time", "end_time", "join_time", "leave_time",
    "recording_start", "recording_end",

    # Azure / AAD
    "createdDateTime", "authenticationStepDateTime",

    # EventHub-ish
    "originalEventTimestamp", "StartTimeUtc", "EndTimeUtc",
    "TimeGenerated", "ProcessingEndTime",

    # O365
    "PublishedTime", "LastUpdatedTime", "StartTime", "EndTime", "MilestoneDate",
    "StatusTime",

    # GApps / Directory
    "lastLoginTime", "creationTime", "viewedByMeTime", "createdTime",
    "modifiedTime", "modifiedByMeTime",

    # Generic
    "date_time", "datetime",
}

# =========================
# END CONFIG
# =========================

ISO_STRICT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

WIN_SYSTEMTIME_RE = re.compile(
    r"""(<TimeCreated\b[^>]*\bSystemTime=)(['"])([^'"]+)(\2)""",
    re.IGNORECASE,
)

WIN_DATA_RE = re.compile(
    r"""(<Data\b[^>]*Name=)(['"])([^'"]+)(\2)([^>]*>)([^<]+)(</Data>)""",
    re.IGNORECASE,
)

def parse_iso_any(s: str) -> Optional[datetime]:
    s = s.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def format_like_input(dt: datetime, original: str) -> str:
    dt = dt.astimezone(timezone.utc)
    if original.endswith("Z"):
        if "." in original:
            return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    # Preserve microseconds if the original looks microsecond-ish (e.g. ...274000+00:00)
    if "." in original:
        frac = original.split(".")[-1].split("+")[0].split("-")[0]
        if len(frac) > 3:
            return dt.isoformat(timespec="microseconds")
    return dt.isoformat(timespec="milliseconds")

def shift_time_str(s: str, delta: timedelta) -> Optional[str]:
    dt = parse_iso_any(s)
    if dt is None:
        return None
    return format_like_input(dt + delta, s)

def walk_and_shift_json(obj: Any, delta: timedelta) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str):
                do = (k in RAW_TIME_KEYS) or (AGGRESSIVE_RAW_REWRITE and ISO_STRICT_RE.match(v))
                if do:
                    nv = shift_time_str(v, delta)
                    if nv is not None:
                        out[k] = nv
                        continue
            out[k] = walk_and_shift_json(v, delta)
        return out
    if isinstance(obj, list):
        return [walk_and_shift_json(x, delta) for x in obj]
    return obj

def shift_windows_xml(raw: str, delta: timedelta) -> str:
    def repl_system(m: re.Match) -> str:
        prefix, quote, t, _ = m.group(1), m.group(2), m.group(3), m.group(4)
        nt = shift_time_str(t, delta)
        if nt is None:
            return m.group(0)
        return f"{prefix}{quote}{nt}{quote}"

    raw2 = WIN_SYSTEMTIME_RE.sub(repl_system, raw)

    def repl_data(m: re.Match) -> str:
        pre, q1, name, q2, mid, val, post = m.groups()
        val_s = val.strip()
        if not ("T" in val_s and (val_s.endswith("Z") or "+" in val_s or "-" in val_s)):
            return m.group(0)

        # Safer default: only rewrite XML Data fields whose name looks time-ish
        if (not AGGRESSIVE_RAW_REWRITE) and (("time" not in name.lower()) and ("date" not in name.lower())):
            return m.group(0)

        nt = shift_time_str(val_s, delta)
        if nt is None:
            return m.group(0)
        return f"{pre}{q1}{name}{q2}{mid}{nt}{post}"

    return WIN_DATA_RE.sub(repl_data, raw2)

def adjust_event(ev: dict, delta: timedelta) -> dict:
    t = ev.get("_time")
    if isinstance(t, str):
        nt = shift_time_str(t, delta)
        if nt is not None:
            ev["_time"] = nt

    raw = ev.get("_raw")
    if not isinstance(raw, str) or not raw:
        return ev

    rs = raw.lstrip()

    # JSON raw
    if rs.startswith("{") and rs.endswith("}"):
        try:
            robj = json.loads(raw)
            robj2 = walk_and_shift_json(robj, delta)
            ev["_raw"] = json.dumps(robj2, ensure_ascii=False)
            return ev
        except Exception:
            pass

    # XML raw
    if "<Event" in raw or "TimeCreated" in raw:
        ev["_raw"] = shift_windows_xml(raw, delta)

    return ev

def find_min_time(files: list[str]) -> datetime:
    min_dt = None
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("_time")
                if isinstance(t, str):
                    dt = parse_iso_any(t)
                    if dt is not None:
                        min_dt = dt if min_dt is None else min(min_dt, dt)
    if min_dt is None:
        raise RuntimeError("Could not find any parsable _time in input.")
    return min_dt

def main():
    files = sorted(glob.glob(INPUT_GLOB))
    if not files:
        raise SystemExit(f"No files matched {INPUT_GLOB}")

    src_min = find_min_time(files)

    if TARGET_EARLIEST_TIME_ISO:
        target_min = parse_iso_any(TARGET_EARLIEST_TIME_ISO)
        if target_min is None:
            raise SystemExit(f"Could not parse TARGET_EARLIEST_TIME_ISO: {TARGET_EARLIEST_TIME_ISO}")
    else:
        target_min = datetime.now(timezone.utc) - timedelta(days=TARGET_EARLIEST_NOW_MINUS_DAYS)

    delta = target_min - src_min

    print(f"Input files: {len(files)}")
    print(f"Earliest _time in data: {src_min.isoformat()}")
    print(f"Target earliest _time:  {target_min.isoformat()}")
    print(f"Delta applied:          {delta}")
    print(f"AGGRESSIVE_RAW_REWRITE: {AGGRESSIVE_RAW_REWRITE}")
    print(f"Writing to:             {OUT_DIR}/")

    os.makedirs(OUT_DIR, exist_ok=True)

    total = 0
    for path in files:
        out_path = os.path.join(OUT_DIR, os.path.basename(path))
        print(f"Rewriting {path} -> {out_path}")

        with open(path, "r", encoding="utf-8") as inp, open(out_path, "w", encoding="utf-8") as out:
            for line in inp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ev2 = adjust_event(ev, delta)
                out.write(json.dumps(ev2, ensure_ascii=False) + "\n")
                total += 1

    print(f"Done. Rewrote {total} events.")

if __name__ == "__main__":
    main()
