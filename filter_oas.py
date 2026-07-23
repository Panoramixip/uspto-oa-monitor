#!/usr/bin/env python3
"""
USPTO Trademark Office Action monitor.

Daily job:
  1. Lists recent files of the USPTO bulk dataset TRTDXFAP
     (Trademark Full Text XML Data - Daily Applications) via the
     Open Data Portal API.
  2. Downloads any daily file not yet processed.
  3. Scans every case file for prosecution-history events indicating an
     office action was issued (mailed/e-mailed) recently.
  4. Keeps only applications whose owner is domiciled OUTSIDE the US and
     which have NO attorney of record (i.e. no US representative).
     Covers both direct US applications and Madrid Protocol s.66(a)
     designations (serial numbers beginning 79).
  5. Writes results to data/latest.json, data/recent.json, data/recent.csv
     and data/archive/YYYY-MM-DD.json, with dedupe state in data/state.json.

Requires env var USPTO_API_KEY (USPTO Open Data Portal API key).
Standard library only.
"""

import csv
import io
import json
import os
import re
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

PRODUCT_ID = "TRTDXFAP"
API_BASE = "https://api.uspto.gov/api/v1/datasets/products"
API_KEY = os.environ.get("USPTO_API_KEY", "").strip()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

# How many days back to look for unprocessed daily files.
LOOKBACK_DAYS = 8
# An OA event qualifies if dated within this many days of the file's date.
EVENT_WINDOW_DAYS = 4
# Keep dedupe keys / recent records for this many days.
STATE_RETENTION_DAYS = 30
RECENT_DAYS = 7

# Prosecution-history descriptions counted as an issued office action.
# Matched case-insensitively against description-text.
OA_INCLUDE = [
    r"ACTION\s+MAILED",          # NON-FINAL ACTION MAILED, FINAL ACTION MAILED,
                                 # PRIORITY ACTION MAILED, ...ACTION MAILED - REFUSAL SENT TO IB
    r"ACTION\s+E-?MAILED",       # e-mailed variants
    r"REFUSAL\s+MAILED",         # FINAL REFUSAL MAILED, SUBSEQUENT FINAL REFUSAL MAILED
    r"REFUSAL\s+E-?MAILED",
]
# Never count these even if they match an include pattern.
OA_EXCLUDE = [
    r"NOTICE\s+OF",              # notices are not office actions
    r"WITHDRAWN",
    r"VACATED",
]

OA_INCLUDE_RE = [re.compile(p, re.I) for p in OA_INCLUDE]
OA_EXCLUDE_RE = [re.compile(p, re.I) for p in OA_EXCLUDE]

US_COUNTRY_VALUES = {"", "US", "USA", "USX", "UNITED STATES", "UNITED STATES OF AMERICA"}


def log(msg):
    print(f"[filter_oas] {msg}", flush=True)


def api_request(url):
    req = urllib.request.Request(url)
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    req.add_header("Accept", "application/json")
    return urllib.request.urlopen(req, timeout=120)


def list_recent_files():
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    to = today.isoformat()
    url = (f"{API_BASE}/{PRODUCT_ID}?fileDataFromDate={frm}&fileDataToDate={to}"
           f"&includeFiles=true&limit=100")
    log(f"Listing product files: {url}")
    with api_request(url) as resp:
        payload = json.load(resp)
    files = []

    def walk(obj):
        if isinstance(obj, dict):
            if "fileDataBag" in obj and isinstance(obj["fileDataBag"], list):
                files.extend(obj["fileDataBag"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    out = []
    for f in files:
        name = f.get("fileName") or ""
        if not name.lower().endswith(".zip"):
            continue
        out.append({
            "fileName": name,
            "fileDate": f.get("fileDate") or f.get("fileDataToDate") or "",
            "uri": f.get("fileDownloadURI") or f"{API_BASE}/files/{PRODUCT_ID}/{name}",
            "size": f.get("fileSize"),
        })
    out.sort(key=lambda x: (x["fileDate"], x["fileName"]))
    log(f"Found {len(out)} candidate files: {[f['fileName'] for f in out]}")
    return out


def download_file(f, dest_dir):
    path = os.path.join(dest_dir, f["fileName"])
    log(f"Downloading {f['fileName']} ({f.get('size')}) from {f['uri']}")
    req = urllib.request.Request(f["uri"])
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=600) as resp, open(path, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    log(f"Downloaded to {path} ({os.path.getsize(path)} bytes)")
    return path


def text(el, path):
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_date(s):
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def is_office_action(desc):
    if not desc:
        return False
    for rx in OA_EXCLUDE_RE:
        if rx.search(desc):
            return False
    return any(rx.search(desc) for rx in OA_INCLUDE_RE)


def owner_info(case_file):
    """Return (name, country_raw, is_foreign) using the first listed owner."""
    owners = case_file.findall("./case-file-owners/case-file-owner")
    if not owners:
        return "", "", False
    o = owners[0]
    name = text(o, "./party-name")
    country = text(o, "./address-1/country") or text(o, "./country")
    nationality = text(o, "./nationality/country")
    state = text(o, "./address-1/state") or text(o, "./state")
    c = (country or nationality or "").upper()
    if c in US_COUNTRY_VALUES:
        # Empty country + a US state ⇒ US-domiciled. Empty everything ⇒ unknown, skip.
        return name, c, False
    return name, c, True


def extract_hits(zip_path, file_date, seen, debug_descriptions):
    """Yield qualifying records from one daily zip."""
    hits = []
    window_start = file_date - timedelta(days=EVENT_WINDOW_DAYS)
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".xml")]
        for member in members:
            with zf.open(member) as fh:
                # iterparse to keep memory bounded
                context = ET.iterparse(io.BufferedReader(fh), events=("end",))
                for _, el in context:
                    if el.tag != "case-file":
                        continue
                    try:
                        record = examine_case_file(
                            el, window_start, file_date, seen, debug_descriptions)
                        if record:
                            hits.append(record)
                    finally:
                        el.clear()
    return hits


def examine_case_file(cf, window_start, file_date, seen, debug_descriptions):
    events = cf.findall(
        "./case-file-event-statements/case-file-event-statement")
    oa_events = []
    for ev in events:
        desc = text(ev, "./description-text")
        d = parse_date(text(ev, "./date"))
        if d and window_start <= d <= file_date:
            key = desc.upper()
            debug_descriptions[key] = debug_descriptions.get(key, 0) + 1
        if d and is_office_action(desc) and window_start <= d <= file_date:
            oa_events.append((d, desc, text(ev, "./code")))
    if not oa_events:
        return None

    serial = text(cf, "./serial-number")
    attorney = text(cf, "./case-file-header/attorney-name")
    if attorney:
        return None  # has a representative

    name, country, foreign = owner_info(cf)
    if not foreign:
        return None

    oa_events.sort()
    oa_date, oa_desc, oa_code = oa_events[-1]
    dedupe_key = f"{serial}|{oa_date.isoformat()}|{oa_desc.upper()}"
    if dedupe_key in seen:
        return None
    seen[dedupe_key] = datetime.now(timezone.utc).date().isoformat()

    mark = text(cf, "./case-file-header/mark-identification")
    filing_date = parse_date(text(cf, "./case-file-header/filing-date"))
    status_code = text(cf, "./case-file-header/status-code")
    intl_reg = text(cf, "./madrid-international-filing-requests/"
                        "madrid-international-filing-record/international-registration-number") \
        or text(cf, "./international-registration-number")
    is_madrid = serial.startswith("79")

    return {
        "serial": serial,
        "mark": mark,
        "applicant": name,
        "applicant_country": country,
        "route": "Madrid (66(a) designation)" if is_madrid else "Direct US application",
        "international_registration": intl_reg,
        "office_action": oa_desc,
        "office_action_code": oa_code,
        "office_action_date": oa_date.isoformat(),
        "filing_date": filing_date.isoformat() if filing_date else "",
        "status_code": status_code,
        "tsdr_status": f"https://tsdr.uspto.gov/#caseNumber={serial}&caseType=SERIAL_NO&searchType=statusSearch",
        "tsdr_documents": f"https://tsdr.uspto.gov/documentviewer?caseId=sn{serial}",
        "found_date": datetime.now(timezone.utc).date().isoformat(),
    }


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            return json.load(fh)
    return {"processed_files": [], "seen": {}}


def save_state(state):
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["processed_files"] = state["processed_files"][-60:]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def load_recent():
    path = os.path.join(DATA_DIR, "recent.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh).get("records", [])
    return []


def write_outputs(new_records, processed_names, debug_descriptions):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).date()

    latest = {
        "run_date": today.isoformat(),
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": processed_names,
        "new_record_count": len(new_records),
        "records": new_records,
        "debug_event_descriptions_in_window": dict(
            sorted(debug_descriptions.items(), key=lambda kv: -kv[1])[:80]),
    }
    with open(os.path.join(DATA_DIR, "latest.json"), "w") as fh:
        json.dump(latest, fh, indent=1)

    cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()
    recent = [r for r in load_recent() if r.get("found_date", "") >= cutoff]
    existing_keys = {(r["serial"], r["office_action_date"]) for r in recent}
    for r in new_records:
        if (r["serial"], r["office_action_date"]) not in existing_keys:
            recent.append(r)
    recent.sort(key=lambda r: (r["found_date"], r["applicant_country"], r["serial"]),
                reverse=True)
    with open(os.path.join(DATA_DIR, "recent.json"), "w") as fh:
        json.dump({"generated": today.isoformat(), "records": recent}, fh, indent=1)

    if new_records:
        with open(os.path.join(ARCHIVE_DIR, f"{today.isoformat()}.json"), "w") as fh:
            json.dump(latest, fh, indent=1)

    cols = ["found_date", "serial", "mark", "applicant", "applicant_country",
            "route", "international_registration", "office_action",
            "office_action_date", "filing_date", "tsdr_status"]
    with open(os.path.join(DATA_DIR, "recent.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recent:
            w.writerow(r)

    log(f"Wrote outputs: {len(new_records)} new record(s), "
        f"{len(recent)} in rolling 7-day window.")


def main():
    if not API_KEY:
        log("WARNING: USPTO_API_KEY not set - attempting anonymous access "
            "(likely to fail with 401).")
    state = load_state()
    try:
        files = list_recent_files()
    except urllib.error.HTTPError as e:
        log(f"ERROR listing product files: HTTP {e.code} - {e.read()[:500]!r}")
        sys.exit(1)

    todo = [f for f in files if f["fileName"] not in state["processed_files"]]
    if not todo:
        log("No new daily files to process.")
        write_outputs([], [], {})
        save_state(state)
        return

    all_new = []
    debug_descriptions = {}
    processed_names = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in todo:
            fdate = None
            for fmt in ("%Y-%m-%d",):
                try:
                    fdate = datetime.strptime((f["fileDate"] or "")[:10], fmt).date()
                except ValueError:
                    pass
            if fdate is None:
                m = re.search(r"(\d{6})", f["fileName"])
                fdate = (datetime.strptime(m.group(1), "%y%m%d").date()
                         if m else datetime.now(timezone.utc).date())
            try:
                path = download_file(f, tmp)
            except urllib.error.HTTPError as e:
                log(f"ERROR downloading {f['fileName']}: HTTP {e.code}; skipping.")
                continue
            hits = extract_hits(path, fdate, state["seen"], debug_descriptions)
            log(f"{f['fileName']}: {len(hits)} qualifying record(s).")
            all_new.extend(hits)
            state["processed_files"].append(f["fileName"])
            processed_names.append(f["fileName"])
            os.remove(path)

    write_outputs(all_new, processed_names, debug_descriptions)
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
