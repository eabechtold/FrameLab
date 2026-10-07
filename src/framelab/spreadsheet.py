"""Minimal spreadsheet helpers for exchanging timestamp tables with Excel.

Only what FrameLab needs: write/read a single-sheet .xlsx using the standard
library, and place a table on the Windows clipboard as both plain text and
HTML so Excel applies a ``0.000`` number format to the seconds column.
"""

import ctypes
import ctypes.wintypes
import html
import os
import re
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

SECONDS_NUMBER_FORMAT = "0.000"

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_letter(index):
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _column_index(cell_reference):
    letters = re.match(r"[A-Za-z]+", cell_reference).group(0).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def write_xlsx(path, rows, seconds_column=None, column_widths=None):
    """Write ``rows`` (first row is a header) to a single-sheet workbook.

    Floats in ``seconds_column`` are stored as numbers formatted ``0.000`` so
    trailing zeros show in Excel (10.000 rather than 10).
    """
    column_widths = column_widths or []
    sheet_rows = []
    for row_index, row in enumerate(rows):
        cells = []
        for column, value in enumerate(row):
            ref = f"{_column_letter(column)}{row_index + 1}"
            if row_index == 0:
                cells.append(f'<c r="{ref}" s="2" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            elif isinstance(value, (int, float)):
                style = ' s="1"' if column == seconds_column else ""
                cells.append(f'<c r="{ref}"{style}><v>{value!r}</v></c>')
            else:
                text = escape(str(value))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index + 1}">{"".join(cells)}</row>')

    columns = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>'
        for i, width in enumerate(column_widths)
    )
    cols_xml = f"<cols>{columns}</cols>" if columns else ""
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'{cols_xml}<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="1"><numFmt numFmtId="164" formatCode="{SECONDS_NUMBER_FORMAT}"/></numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '</cellXfs></styleSheet>'
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Timestamps" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'
        ),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def read_xlsx(path):
    """Return the first worksheet as a list of rows (strings and floats; blanks are None)."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter(f"{_MAIN_NS}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{_MAIN_NS}t")))
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in names:
            candidates = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
            if not candidates:
                raise ValueError("The workbook has no worksheets.")
            sheet_name = candidates[0]
        root = ElementTree.fromstring(archive.read(sheet_name))

    rows = []
    for row in root.iter(f"{_MAIN_NS}row"):
        values = []
        for cell in row.iter(f"{_MAIN_NS}c"):
            column = _column_index(cell.get("r", "A1"))
            while len(values) <= column:
                values.append(None)
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{_MAIN_NS}t"))
            else:
                node = cell.find(f"{_MAIN_NS}v")
                raw = node.text if node is not None else None
                if raw is None:
                    value = None
                elif kind == "s":
                    value = shared[int(raw)]
                elif kind in ("str", "e", "b"):
                    value = raw
                else:
                    value = float(raw)
            values[column] = value
        rows.append(values)
    return rows


def set_windows_clipboard_table(rows, seconds_column=None):
    """Put ``rows`` on the clipboard as plain text and HTML (Excel-friendly).

    Plain text is tab-separated. The HTML copy marks the seconds column with
    a ``0.000`` number format, which Excel honors on paste.
    """
    if os.name != "nt":
        raise RuntimeError("Clipboard table copy is implemented for Windows only.")

    def cell_text(row_index, column, value):
        if column == seconds_column and row_index > 0:
            return f"{value:.3f}"
        return str(value)

    text = "\n".join(
        "\t".join(cell_text(i, c, v) for c, v in enumerate(row)) for i, row in enumerate(rows)
    ) + "\n"

    body = ["<table>"]
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        cells = []
        for c, value in enumerate(row):
            style = ""
            if c == seconds_column and i > 0:
                style = f' style=\'mso-number-format:"{SECONDS_NUMBER_FORMAT.replace(".", chr(92) + ".")}"\''
            cells.append(f"<{tag}{style}>{html.escape(cell_text(i, c, value))}</{tag}>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    body.append("</table>")
    fragment = "".join(body)

    prefix = "<html><body><!--StartFragment-->"
    suffix = "<!--EndFragment--></body></html>"
    header_template = (
        "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
        "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
    )
    header_len = len(header_template.format(0, 0, 0, 0))
    start_html = header_len
    start_fragment = start_html + len(prefix.encode("utf-8"))
    end_fragment = start_fragment + len(fragment.encode("utf-8"))
    end_html = end_fragment + len(suffix.encode("utf-8"))
    cf_html = (
        header_template.format(start_html, end_html, start_fragment, end_fragment) + prefix + fragment + suffix
    ).encode("utf-8")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
    user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
    user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [ctypes.wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = ctypes.wintypes.UINT
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]

    cf_unicode_text = 13
    cf_html_format = user32.RegisterClipboardFormatW("HTML Format")
    items = (
        (cf_unicode_text, text.encode("utf-16-le") + b"\x00\x00"),
        (cf_html_format, cf_html + b"\x00"),
    )

    if not user32.OpenClipboard(None):
        raise RuntimeError("Could not open Windows clipboard.")
    try:
        user32.EmptyClipboard()
        for fmt, data in items:
            handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            if not handle:
                raise RuntimeError("GlobalAlloc failed while copying to clipboard.")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise RuntimeError("GlobalLock failed while copying to clipboard.")
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                raise RuntimeError("SetClipboardData failed while copying to clipboard.")
    finally:
        user32.CloseClipboard()
