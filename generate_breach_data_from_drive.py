import html
import io
import json
import os
import re
import struct
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1yg06zp_7OMYZzkNnHDovUSHF7zdW8mTc")
OUT_DIR = Path("public")
OUT_FILE = OUT_DIR / "breach-data.js"


def norm(value):
    text = str(value or "").lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def read_varint(stream):
    shift = 0
    value = 0
    while True:
        raw = stream.read(1)
        if not raw:
            return None
        b = raw[0]
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value
        shift += 7


def read_header(stream):
    raw = stream.read(1)
    if not raw:
        return None
    first = raw[0]
    rec_type = first & 0x7F
    if first & 0x80:
        second = stream.read(1)
        if not second:
            return None
        rec_type |= second[0] << 7
    length = read_varint(stream)
    if length is None:
        return None
    return rec_type, length


def sheet_names(zf):
    try:
        raw = zf.read("docProps/app.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    ns = {
        "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
        "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    }
    return [node.text or "" for node in root.findall(".//ep:TitlesOfParts/vt:vector/vt:lpstr", ns)]


def sheet_entries(zf):
    names = [name for name in zf.namelist() if re.match(r"^xl/worksheets/sheet\d+\.bin$", name)]
    return sorted(names, key=lambda n: int(re.search(r"sheet(\d+)\.bin", n).group(1)))


def shared_strings(zf):
    try:
        data = zf.read("xl/sharedStrings.bin")
    except KeyError:
        return []
    out = []
    stream = io.BytesIO(data)
    while True:
        header = read_header(stream)
        if header is None:
            break
        rec_type, length = header
        payload = stream.read(length)
        if rec_type == 19 and len(payload) >= 5:
            chars = struct.unpack_from("<I", payload, 1)[0]
            byte_count = min(chars * 2, len(payload) - 5)
            out.append(payload[5 : 5 + byte_count].decode("utf-16le", errors="ignore"))
    return out


def cell_value(rec_type, payload, sst):
    if len(payload) < 12:
        return None
    if rec_type == 7:
        idx = struct.unpack_from("<I", payload, 8)[0]
        return sst[idx] if idx < len(sst) else ""
    if rec_type == 5 and len(payload) >= 16:
        return struct.unpack_from("<d", payload, 8)[0]
    if rec_type == 2 and len(payload) >= 12:
        rk = struct.unpack_from("<I", payload, 8)[0]
        if rk & 0x02:
            value = struct.unpack("<i", struct.pack("<I", rk))[0] >> 2
        else:
            bits = (rk & 0xFFFFFFFC) << 32
            value = struct.unpack("<d", struct.pack("<Q", bits))[0]
        return value / 100.0 if rk & 0x01 else value
    return None


def read_small_sheet(zf, entry_name, sst, max_rows):
    rows = {}
    current_row = -1
    stream = io.BytesIO(zf.read(entry_name))
    while True:
        header = read_header(stream)
        if header is None:
            break
        rec_type, length = header
        payload = stream.read(length)
        if rec_type == 0:
            if len(payload) >= 4:
                current_row = struct.unpack_from("<I", payload, 0)[0]
                if current_row > max_rows:
                    break
                rows.setdefault(current_row, {})
        elif rec_type in (7, 5, 2):
            if current_row <= max_rows and len(payload) >= 4:
                col = struct.unpack_from("<I", payload, 0)[0]
                value = cell_value(rec_type, payload, sst)
                if value is not None:
                    rows.setdefault(current_row, {})[col] = value
    return rows


def to_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value or "").replace(",", ""))
    except ValueError:
        return 0.0


def grand_total_load(zf, summary_entry, sst):
    if not summary_entry:
        return 0
    rows = read_small_sheet(zf, summary_entry, sst, 2500)
    best = 0
    for _, row in sorted(rows.items()):
        grand_col = None
        for col, value in sorted(row.items()):
            if norm(value) == "grand summary:":
                grand_col = col
                break
        if grand_col is None:
            continue
        for col, value in sorted(row.items()):
            if col <= grand_col:
                continue
            num = to_number(value)
            if num > 0:
                best = max(best, num)
                break
    return round(best)


def date_key(file_name):
    match = re.search(r"(20\d{6})", os.path.splitext(file_name)[0])
    return match.group(1) if match else datetime.now().strftime("%Y%m%d")


def analyze_main(zf, main_entry, sst, record):
    hubs = {}
    remarks = {}
    rows = read_small_sheet(zf, main_entry, sst, 500)
    header_row = pickup_col = count_col = remarks_col = -1

    for row_idx, row in sorted(rows.items()):
        for col, value in sorted(row.items()):
            n = norm(value)
            if count_col < 0 and "count" in n and "awb" in n and n != "awbs count":
                header_row = row_idx
                count_col = col
                pickup_col = col - 1
            if n == "remarks":
                remarks_col = col
        if header_row > -1:
            break

    if header_row < 0:
        record.update({"totalBreach": 0, "hubs": [], "remarks": {}})
        return

    for row_idx, row in sorted(rows.items()):
        if row_idx <= header_row:
            continue
        pickup = str(row.get(pickup_col, "") or "").strip()
        if norm(pickup) == "grand total":
            break
        count = round(to_number(row.get(count_col)))
        if not pickup or count <= 0:
            continue
        remark = str(row.get(remarks_col, "") or "").strip() if remarks_col > -1 else ""
        remark = remark or "Unknown"
        hub = hubs.setdefault(pickup.lower(), {"pickupHub": pickup, "count": 0, "remarks": {}})
        hub["count"] += count
        hub["remarks"][remark] = hub["remarks"].get(remark, 0) + count
        remarks[remark] = remarks.get(remark, 0) + count

    hub_list = sorted(hubs.values(), key=lambda item: item["count"], reverse=True)
    record["totalBreach"] = sum(item["count"] for item in hub_list)
    record["hubs"] = hub_list
    record["remarks"] = remarks


def parse_workbook(file_name, content):
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = sheet_names(zf)
        entries = sheet_entries(zf)
        if not entries:
            raise ValueError(f"{file_name} does not contain xlsb worksheet binaries")
        sst = shared_strings(zf)
        by_name = {names[i].lower(): entries[i] for i in range(min(len(names), len(entries)))}
        record = {
            "fileName": file_name,
            "dateKey": date_key(file_name),
            "grandTotalLoad": grand_total_load(zf, by_name.get("summary"), sst),
            "totalBreach": 0,
            "hubs": [],
            "remarks": {},
        }
        main = by_name.get("main")
        if main:
            analyze_main(zf, main, sst, record)
        return record


def list_public_drive_files(folder_id):
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    with urllib.request.urlopen(url, timeout=60) as response:
        text = response.read().decode("utf-8", errors="ignore")
    pattern = (
        r"\\x5b\\x22([^\\]+)\\x22,\\x5b\\x22"
        + re.escape(folder_id)
        + r"\\x22\\x5d,\\x22([^\\]+\.(?:xlsb|xlsx))\\x22.*?,null,null,(\d+),\\x5b"
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
        raise RuntimeError("No .xlsb/.xlsx files found. Make sure the Drive folder is shared publicly.")
    return sorted(files, key=lambda item: item["name"])


def download_public_drive_file(file_id, expected_size):
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    with urllib.request.urlopen(url, timeout=240) as response:
        content = response.read()
    if expected_size and len(content) != expected_size:
        raise RuntimeError(f"Download size mismatch for {file_id}: expected {expected_size}, got {len(content)}")
    return content


def main():
    records = []
    files = list_public_drive_files(FOLDER_ID)
    for item in files:
        print(f"Reading {item['name']}")
        content = download_public_drive_file(item["id"], item["size"])
        records.append(parse_workbook(item["name"], content))
    records.sort(key=lambda r: str(r.get("dateKey", "")))
    root = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "sourceFolder": f"google-drive-public:{FOLDER_ID}",
        "records": records,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        "window.BREACH_DASHBOARD_DATA = " + json.dumps(root, ensure_ascii=False) + ";",
        encoding="utf-8",
    )
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><title>Breach Data</title><h1>Breach data generated</h1><p>Use breach-data.js from this site.</p>",
        encoding="utf-8",
    )
    print(f"Generated {OUT_FILE}")


if __name__ == "__main__":
    main()
