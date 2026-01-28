#!/usr/bin/env python3
import json
import glob
import os
import re
from collections import defaultdict, Counter
from datetime import datetime, timezone

from env_utils import load_env

load_env()

# --------- config ----------
INPUT_GLOB = os.getenv("SCAN_INPUT_GLOB", "export_bots_data4/*.jsonl")     # point at your converted jsonl files
MAX_RAW_JSON_PARSE = 5_000_000  # skip JSON-parse of _raw if it's insanely large (safety)
SCAN_BUCKET_SECONDS = int(os.getenv("SCAN_BUCKET_SECONDS", "86400"))  # histogram bucket size (default: 1 day)
# ---------------------------

# ISO-ish timestamp patterns (Zulu + offsets)
ISO_LIKE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Windows Event XML: TimeCreated SystemTime='...'
WIN_SYSTEMTIME_RE = re.compile(
    r"""<TimeCreated\b[^>]*\bSystemTime=(['"])([^'"]+)\1""",
    re.IGNORECASE,
)

# Windows sometimes has other times inside <Data>...</Data> elements
WIN_DATA_TIME_RE = re.compile(
    r"""<Data\b[^>]*Name=(['"])([^'"]+)\1[^>]*>([^<]+)</Data>""",
    re.IGNORECASE,
)

RAW_JSON_TIME_KEYS = {
    # common in Stream and similar telemetry
    "timestamp", "endtime", "starttime", "time", "event_time",
    "datetime", "created_at", "updated_at",
}

def parse_iso_any(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    # normalize Z -> +00:00 for fromisoformat
    try:
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s2)
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def update_range(rng: dict, dt: datetime):
    if dt is None:
        return
    mn = rng.get("min")
    mx = rng.get("max")
    rng["min"] = dt if mn is None else min(mn, dt)
    rng["max"] = dt if mx is None else max(mx, dt)

def walk_json_for_times(obj, found: list):
    """Collect (keypath, timestamp_str) tuples."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if isinstance(v, str):
                if kl in RAW_JSON_TIME_KEYS or k in RAW_JSON_TIME_KEYS:
                    # fast accept if it looks ISO-ish
                    if "T" in v and (v.endswith("Z") or "+" in v or "-" in v):
                        found.append((k, v))
                # also catch arbitrary fields that look like ISO timestamps
                elif ISO_LIKE_RE.match(v):
                    found.append((k, v))
            elif isinstance(v, (dict, list)):
                walk_json_for_times(v, found)
    elif isinstance(obj, list):
        for it in obj:
            walk_json_for_times(it, found)

def extract_raw_timestamps(raw: str):
    """
    Returns list of (kind, key, timestr) where kind is 'json' or 'xml'
    """
    out = []
    if not raw:
        return out

    s = raw.lstrip()

    # JSON raw (Stream, etc.)
    if s.startswith("{") and s.endswith("}") and len(raw) <= MAX_RAW_JSON_PARSE:
        try:
            obj = json.loads(raw)
            found = []
            walk_json_for_times(obj, found)
            for k, v in found:
                out.append(("json", k, v))
            return out
        except Exception:
            pass  # fall through to XML scan

    # XML raw (Windows Event Log)
    if "<Event" in raw or "TimeCreated" in raw:
        for m in WIN_SYSTEMTIME_RE.finditer(raw):
            out.append(("xml", "TimeCreated.SystemTime", m.group(2)))

        # also collect any <Data Name="...">VALUE</Data> that looks like ISO time
        for m in WIN_DATA_TIME_RE.finditer(raw):
            name = m.group(2)
            val = m.group(3).strip()
            if "T" in val and (val.endswith("Z") or "+" in val or "-" in val):
                out.append(("xml", f"Data.{name}", val))

    return out

def main():
    files = sorted(glob.glob(INPUT_GLOB))
    if not files:
        print(f"No files matched {INPUT_GLOB}")
        return

    print(f"Found {len(files)} files to scan (glob: {INPUT_GLOB})", flush=True)

    # Per sourcetype aggregates
    outer_time_range = defaultdict(lambda: {"min": None, "max": None})
    raw_time_range = defaultdict(lambda: defaultdict(lambda: {"min": None, "max": None}))  # st -> rawkey -> range
    raw_key_counts = defaultdict(Counter)  # st -> Counter(rawkey)
    hist_buckets = defaultdict(Counter)  # st -> Counter(bucket_epoch -> count)
    total_events = 0

    for idx, path in enumerate(files, 1):
        events_in_file = 0
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                total_events += 1
                events_in_file += 1
                try:
                    ev = json.loads(line)
                except Exception:
                    # skip bad lines
                    continue

                st = ev.get("sourcetype", "(none)")
                t = ev.get("_time")
                if isinstance(t, str):
                    dt = parse_iso_any(t)
                    update_range(outer_time_range[st], dt)
                    if dt:
                        bucket = int(dt.timestamp()) // SCAN_BUCKET_SECONDS * SCAN_BUCKET_SECONDS
                        hist_buckets[st][bucket] += 1

                raw = ev.get("_raw")
                if isinstance(raw, str):
                    for kind, key, timestr in extract_raw_timestamps(raw):
                        dt = parse_iso_any(timestr)
                        raw_key = f"{kind}:{key}"
                        raw_key_counts[st][raw_key] += 1
                        update_range(raw_time_range[st][raw_key], dt)

                if events_in_file and events_in_file % 50000 == 0:
                    print(
                        f"[{idx}/{len(files)}] {path} ... {events_in_file} events so far (total {total_events})",
                        flush=True,
                    )

        print(f"[{idx}/{len(files)}] Finished {path} ({events_in_file} events, total {total_events})", flush=True)

    # Print summary
    print(f"Scanned events: {total_events}")
    print(f"Distinct sourcetypes: {len(outer_time_range)}\n")

    summary = {
        "input_glob": INPUT_GLOB,
        "total_events": total_events,
        "bucket_seconds": SCAN_BUCKET_SECONDS,
        "sourcetypes": [],
    }

    for st in sorted(outer_time_range.keys()):
        o = outer_time_range[st]
        omin_dt = o["min"]
        omax_dt = o["max"]
        omin = omin_dt.isoformat() if omin_dt else None
        omax = omax_dt.isoformat() if omax_dt else None

        st_entry = {
            "sourcetype": st,
            "time_range": {"min": omin, "max": omax},
            "raw_fields": [],
            "histogram": [],
            "total_events": 0,
        }

        if st in hist_buckets:
            # Convert buckets to sorted list of dicts
            buckets = hist_buckets[st]
            st_entry["total_events"] = sum(buckets.values())
            st_entry["histogram"] = [
                {"ts": int(ts), "count": buckets[ts]} for ts in sorted(buckets.keys())
            ]

        # raw keys
        if st in raw_key_counts and raw_key_counts[st]:
            for raw_key, cnt in raw_key_counts[st].most_common():
                rr = raw_time_range[st][raw_key]
                rmin = rr["min"].isoformat() if rr["min"] else None
                rmax = rr["max"].isoformat() if rr["max"] else None
                st_entry["raw_fields"].append(
                    {
                        "field": raw_key,
                        "count": cnt,
                        "range": {"min": rmin, "max": rmax},
                    }
                )

        summary["sourcetypes"].append(st_entry)

        # Console summary (short)
        print(f"== sourcetype: {st}")
        print(f"  _time range: {omin or '?'} .. {omax or '?'}")
        if st_entry["raw_fields"]:
            top = st_entry["raw_fields"][:10]
            print("  raw timestamp fields (top):")
            for rf in top:
                rmin = rf['range']['min'] or "?"
                rmax = rf['range']['max'] or "?"
                print(f"    {rf['field']}  count={rf['count']}  range={rmin} .. {rmax}")
        else:
            print("  raw timestamp fields: (none detected)")
        print()

    # Emit machine-readable summary marker for consumers (web UI captures this line)
    print("SCAN_SUMMARY_JSON::" + json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
