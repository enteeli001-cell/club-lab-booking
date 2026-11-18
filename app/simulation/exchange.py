"""One XLSX workbook or a single section-tagged CSV; identifiers stay text.

Bound uploads and ZIP expansion before openpyxl; never evaluate spreadsheet formulas.
"""
import csv
from io import BytesIO, StringIO
import json
import zipfile

from openpyxl import Workbook, load_workbook

from .schema import SimulationError, MAX_NODES, MAX_MEMBERS, DOFS, FORCES

TABLES = {
    "nodes": ("id", "x", "y", "z"),
    "members": ("id", "start", "end", "material", "section", "type", "roll"),
    "supports": ("node", *DOFS),
    "loads": ("node", *FORCES),
    "settings": ("displacement_limit",),
}
MAX_FILE = 1_000_000
MAX_ROWS = MAX_MEMBERS + 3 * MAX_NODES + 20


def export_model(model, file_format):
    data = {**model, "settings": [{"displacement_limit": model.get("displacement_limit", .005)}]}
    if file_format == "csv":
        output = StringIO(newline="")
        fields = ("table", "id", "node", "x", "y", "z", "start", "end", "material", "section", "type", "roll", *DOFS, *FORCES, "displacement_limit")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for name in TABLES:
            for row in data[name]:
                writer.writerow({"table": name, **row})
        return BytesIO(output.getvalue().encode("utf-8-sig"))
    workbook = Workbook(); workbook.remove(workbook.active)
    for name, fields in TABLES.items():
        ws = workbook.create_sheet(name); ws.append(list(fields)); ws.freeze_panes = "A2"
        for row in data[name]:
            ws.append([row.get(key, "") for key in fields])
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 18
        # Force text for strings so IDs such as '=...' cannot become Excel formulas.
        for row in ws:
            for cell in row:
                if isinstance(cell.value, str): cell.data_type = "s"
    output = BytesIO(); workbook.save(output); output.seek(0)
    return output


def import_model(upload):
    content = upload.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise SimulationError("size_limit", "导入文件过大，最大 1 MB。")
    result = {name: [] for name in TABLES}
    try:
        if upload.filename.lower().endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(StringIO(text))
            for index, row in enumerate(reader):
                if index >= MAX_ROWS: raise ValueError()
                table = row.get("table")
                if table not in TABLES: raise ValueError()
                result[table].append({key: row.get(key, "") for key in TABLES[table]})
        elif upload.filename.lower().endswith(".xlsx"):
            with zipfile.ZipFile(BytesIO(content)) as archive:
                if len(archive.infolist()) > 40 or sum(i.file_size for i in archive.infolist()) > 4_000_000:
                    raise ValueError()
            workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
            try:
                if set(workbook.sheetnames) != set(TABLES): raise ValueError()
                count = 0
                for table, fields in TABLES.items():
                    rows = workbook[table].iter_rows(values_only=True)
                    header = next(rows)
                    if tuple(header) != fields: raise ValueError()
                    for row in rows:
                        count += 1
                        if count > MAX_ROWS: raise ValueError()
                        if not any(value is not None for value in row): continue
                        if len(row) != len(fields): raise ValueError()
                        result[table].append(dict(zip(fields, ("" if x is None else x for x in row))))
            finally:
                workbook.close()
        else:
            raise ValueError()
        if len(result["settings"]) != 1: raise ValueError()
        result["displacement_limit"] = result.pop("settings")[0]["displacement_limit"]
        # Fill explicitly blank optional spreadsheet fields, not coordinates/IDs.
        for member in result["members"]:
            member["type"] = member["type"] or "frame"
            member["roll"] = member["roll"] or 0
        for key in ("supports", "loads"):
            for row in result[key]:
                for field in TABLES[key][1:]: row[field] = row[field] if row[field] != "" else 0
        return result
    except (ValueError, TypeError, KeyError, StopIteration, UnicodeError, zipfile.BadZipFile, OSError):
        raise SimulationError("import_error", "导入失败，请使用导出模板并检查文件格式、表头及行数。") from None
