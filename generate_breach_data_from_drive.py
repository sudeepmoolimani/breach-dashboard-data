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


def xlsx_sheet_entries(zf):
    names = [name for name in zf.namelist() if re.match(r"^xl/worksheets/sheet\d+\.xml$", name)]
    return sorted(names, key=lambda n: int(re.search(r"sheet(\d+)\.xml", n).group(1)))


def xlsx_shared_strings(zf):
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out = []
    for si in root.findall("a:si", ns):
        parts = [node.text or "" for node in si.findall(".//a:t", ns)]
        out.append("".join(parts))
    return out


def col_index_from_ref(ref):
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    value = 0
    for ch in letters.group(1):
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def read_small_sheet_xlsx(zf, entry, sst, max_rows=500):
    rows = {}
    root = ET.fromstring(zf.read(entry))
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    for row_node in root.findall(".//a:sheetData/a:row", ns):
        row_idx = int(row_node.get("r", "0") or "0") - 1
        if row_idx < 0 or row_idx >= max_rows:
            continue
        row = {}
        for cell in row_node.findall("a:c", ns):
            col_idx = col_index_from_ref(cell.get("r", ""))
            cell_type = cell.get("t", "")
            value = ""
            if cell_type == "s":
                v = cell.find("a:v", ns)
                try:
                    value = sst[int(v.text)] if v is not None and v.text is not None else ""
                except (ValueError, IndexError):
                    value = ""
            elif cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//a:t", ns))
            else:
                v = cell.find("a:v", ns)
                value = v.text if v is not None and v.text is not None else ""
            if value != "":
                row[col_idx] = value
        if row:
            rows[row_idx] = row
    return rows


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



def grand_total_load_from_rows(rows):
    best = 0
    for _, row in sorted(rows.items()):
        grand_col = None
        for col, value in sorted(row.items()):
            if norm(value) in ("grand summary:", "grand total"):
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


def analyze_main_from_rows(rows, record):
    hubs = {}
    remarks = {}
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
        bin_entries = sheet_entries(zf)
        xml_entries = xlsx_sheet_entries(zf)
        record = {
            "fileName": file_name,
            "dateKey": date_key(file_name),
            "grandTotalLoad": 0,
            "totalBreach": 0,
            "hubs": [],
            "remarks": {},
        }
        if bin_entries:
            sst = shared_strings(zf)
            by_name = {names[i].lower(): bin_entries[i] for i in range(min(len(names), len(bin_entries)))}
            record["grandTotalLoad"] = grand_total_load(zf, by_name.get("summary"), sst)
            main = by_name.get("main")
            if main:
                analyze_main(zf, main, sst, record)
            return record
        if xml_entries:
            sst = xlsx_shared_strings(zf)
            by_name = {names[i].lower(): xml_entries[i] for i in range(min(len(names), len(xml_entries)))}
            summary = by_name.get("summary")
            if summary:
                record["grandTotalLoad"] = grand_total_load_from_rows(read_small_sheet_xlsx(zf, summary, sst, 400))
            main = by_name.get("main")
            if main:
                analyze_main_from_rows(read_small_sheet_xlsx(zf, main, sst, 500), record)
            return record
        raise ValueError(f"{file_name} does not contain supported xlsb/xlsx worksheets")


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
