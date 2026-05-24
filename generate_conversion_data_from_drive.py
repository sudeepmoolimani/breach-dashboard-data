import csv
import html
import io
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path


FOLDER_ID = os.environ.get("CONVERSION_DRIVE_FOLDER_ID", "1F2YPnY2PbFaR8MY-mXn8pqM_B6xd9Ejt")
OUT_DIR = Path("public")
OUT_FILE = OUT_DIR / "conversion-data.js"
SEP = "\u001f"


def clean(value):
    return str(value or "").strip()


def header_norm(value):
    return str(value or "").lower().replace("_", " ").replace("-", " ").strip()


def get(row, idx, *names):
    for name in names:
        pos = idx.get(header_norm(name))
        if pos is not None and 0 <= pos < len(row):
            return row[pos]
    return ""


def date_key(value, fallback):
    value = clean(value).strip('"')
    if len(value) >= 10 and value[2] == "/" and value[5] == "/":
        return f"{value[6:10]}-{value[3:5]}-{value[0:2]}"
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return fallback


def file_date(name):
    match = re.search(r"(20\d{6})", Path(name).stem)
    if not match:
        return "Unknown"
    value = match.group(1)
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def week_key(date_value):
    try:
        day = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return "Unknown"
    start = datetime(2026, 1, 1).date()
    first_sunday_offset = (6 - start.weekday()) % 7
    first_sunday = start.fromordinal(start.toordinal() + first_sunday_offset)
    if day < first_sunday:
        return "Week 1"
    return f"Week {(((day - first_sunday).days) // 7) + 2}"


def list_public_drive_files(folder_id):
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    with urllib.request.urlopen(url, timeout=60) as response:
        text = response.read().decode("utf-8", errors="ignore")
    pattern = (
        r"\\x5b\\x22([^\\]+)\\x22,\\x5b\\x22"
        + re.escape(folder_id)
        + r"\\x22\\x5d,\\x22([^\\]+\.csv)\\x22.*?,null,null,(\d+),\\x5b"
    )
    seen = set()
    files = []
    for match in re.finditer(pattern, text):
        file_id, name, size = match.groups()
        if file_id in seen:
            continue
        seen.add(file_id)
        files.append({"id": file_id, "name": html.unescape(name), "size": int(size)})
    if not files:
        raise RuntimeError("No .csv files found. Make sure the Conversion Drive folder is public.")
    return sorted(files, key=lambda item: item["name"])


def drive_response(file_id):
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    return urllib.request.urlopen(url, timeout=300)


def add_group(groups, incoming):
    key = SEP.join(
        [
            incoming["date"],
            incoming["zone"],
            incoming["client"],
            incoming["clientCategory"],
            incoming["seller"],
            incoming["hub"],
        ]
    )
    group = groups.get(key)
    if not group:
        group = {
            "date": incoming["date"],
            "week": week_key(incoming["date"]),
            "zone": incoming["zone"],
            "client": incoming["client"],
            "clientCategory": incoming["clientCategory"],
            "seller": incoming["seller"],
            "hub": incoming["hub"],
            "total": 0,
            "picked": 0,
            "unpicked": 0,
            "firstAttemptSuccess": 0,
        }
        groups[key] = group
    group["total"] += 1
    group["picked"] += incoming["picked"]
    group["unpicked"] += incoming["unpicked"]
    group["firstAttemptSuccess"] += incoming["firstAttemptSuccess"]


def process_csv_file(item, groups):
    fallback_date = file_date(item["name"])
    rows = 0
    print(f"Reading {item['name']} ({item['size']:,} bytes)")
    with drive_response(item["id"]) as response:
        text_stream = io.TextIOWrapper(response, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text_stream)
        try:
            headers = next(reader)
        except StopIteration:
            return 0
        idx = {header_norm(value): i for i, value in enumerate(headers)}
        for row in reader:
            picked_raw = clean(get(row, idx, "Picked")).lower()
            dsr1_raw = clean(get(row, idx, "DSR1")).lower()
            incoming = {
                "date": date_key(get(row, idx, "request_date", "Date"), fallback_date) or "Unknown",
                "zone": clean(get(row, idx, "Zone")) or "Unknown",
                "client": clean(get(row, idx, "client", "client_name")) or "Unknown",
                "clientCategory": clean(get(row, idx, "client_category")) or "Unknown",
                "seller": clean(get(row, idx, "seller_name")) or "Unknown",
                "hub": clean(get(row, idx, "hub")) or "Unknown",
                "picked": 1 if picked_raw == "yes" else 0,
                "unpicked": 1 if picked_raw == "no" else 0,
                "firstAttemptSuccess": 1 if dsr1_raw == "success" else 0,
            }
            add_group(groups, incoming)
            rows += 1
            if rows % 500000 == 0:
                print(f"  {rows:,} rows...")
    return rows


def main():
    groups = {}
    row_count = 0
    files = list_public_drive_files(FOLDER_ID)
    for item in files:
        row_count += process_csv_file(item, groups)

    records = sorted(groups.values(), key=lambda r: (r["date"], r["hub"]))
    payload = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sourceFolder": f"google-drive-public:{FOLDER_ID}",
        "fileCount": len(files),
        "rowCount": row_count,
        "records": records,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        "window.CONVERSION_DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";",
        encoding="utf-8",
    )
    print(f"Generated {OUT_FILE} with {len(records):,} grouped records from {row_count:,} rows")


if __name__ == "__main__":
    main()
