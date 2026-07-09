# -*- coding: utf-8 -*-
"""XLSX summary export — formatted Excel workbook, one row per clash.

Companion to bcf.py for the Reports tool. BCF goes to outside coordination
tools; XLSX is for the dbHMS internal team — quick to scan in Excel,
easy to email, easy to save-as-PDF if anyone wants a non-editable version.

Why manual XLSX rather than openpyxl: pyRevit's IronPython 2.7 doesn't
ship openpyxl and bundling it adds a meaningful dependency for a single
feature. XLSX is a documented zip of XML files (the Office Open XML spec)
and we only need a small subset (one worksheet, ~15 cell styles, shared
strings, auto-filter, frozen pane). Manual construction keeps the
extension dependency-free and stays pure-Python so the whole module
runs in the CPython test suite.

Formatting choices match the dbHMS palette / Browser pill colors:
  * Header row — bold white text on dark slate (#2D3748), frozen
  * Status column — colored pill matching the Browser's status pills
        (Open=red, Reviewed=amber, Approved=blue, Resolved=green)
  * Trade column — colored pill matching the Browser's trade pills
        (one shade per trade)
  * Numbers (#, IDs, midpoint coords) — right-aligned with thin border
  * Long text (Latest comment) — wrap-on
  * Auto-filter dropdowns on every header so users can sort/filter
  * Sensible per-column widths

References:
    - ECMA-376 Office Open XML File Formats (the XLSX spec)
    - We only use the SpreadsheetML subset
"""

import os
import zipfile
from xml.sax.saxutils import escape

from . import report_model as rm


# ---------------------------------------------------------------------------
# Columns + widths
# ---------------------------------------------------------------------------

COLUMNS = [
    'Clash #',
    'Importance',
    'Score',
    'Status',
    'Trade',
    'Issue',
    'Test',
    'Kind',
    'Why it ranks',
    'Discipline pair',
    'Element A name',
    'Element A ID',
    'Element A category',
    'Element A source',
    'Element B name',
    'Element B ID',
    'Element B category',
    'Element B source',
    'Level',
    'Midpoint X (ft)',
    'Midpoint Y (ft)',
    'Midpoint Z (ft)',
    'First seen',
    'Last seen',
    'Comment count',
    'Latest comment',
    'Latest comment author',
    'Clash ID',
]

# Column widths in Excel "characters" (the unit Excel itself uses for col
# widths). Tuned so each column fits typical content without squashing
# names but doesn't waste space on numeric columns.
COL_WIDTHS = [
    8,   # Clash #
    12,  # Importance (band pill)
    7,   # Score
    12,  # Status (centered pill)
    18,  # Trade (centered pill)
    10,  # Issue
    26,  # Test
    7,   # Kind
    52,  # Why it ranks (wrap)
    26,  # Discipline pair
    26,  # Element A name
    12,  # Element A ID
    18,  # Element A category
    18,  # Element A source
    26,  # Element B name
    12,  # Element B ID
    18,  # Element B category
    18,  # Element B source
    14,  # Level
    14,  # Midpoint X
    14,  # Midpoint Y
    14,  # Midpoint Z
    22,  # First seen (ISO date)
    22,  # Last seen
    14,  # Comment count
    50,  # Latest comment (wrap)
    18,  # Latest comment author
    36,  # Clash ID (UUID)
]


# ---------------------------------------------------------------------------
# Style indices (must match the order styles get emitted in styles.xml below)
# ---------------------------------------------------------------------------

STYLE_DEFAULT          = 0
STYLE_HEADER           = 1
STYLE_BODY             = 2
STYLE_NUMBER           = 3
STYLE_STATUS_OPEN      = 4
STYLE_STATUS_REVIEWED  = 5
STYLE_STATUS_APPROVED  = 6
STYLE_STATUS_RESOLVED  = 7
STYLE_TRADE_MECHANICAL = 8
STYLE_TRADE_ELECTRICAL = 9
STYLE_TRADE_PLUMBING   = 10
STYLE_TRADE_FP         = 11
STYLE_TRADE_TECH       = 12
STYLE_TRADE_ARCH       = 13
STYLE_TRADE_STRUCT     = 14
STYLE_BAND_CRITICAL    = 15
STYLE_BAND_MAJOR       = 16
STYLE_BAND_MINOR       = 17

_STATUS_TO_STYLE = {
    'Open':     STYLE_STATUS_OPEN,
    'Reviewed': STYLE_STATUS_REVIEWED,
    'Approved': STYLE_STATUS_APPROVED,
    'Resolved': STYLE_STATUS_RESOLVED,
}

_TRADE_TO_STYLE = {
    'Mechanical':      STYLE_TRADE_MECHANICAL,
    'Electrical':      STYLE_TRADE_ELECTRICAL,
    'Plumbing':        STYLE_TRADE_PLUMBING,
    'Fire Protection': STYLE_TRADE_FP,
    'Technology':      STYLE_TRADE_TECH,
    'Architectural':   STYLE_TRADE_ARCH,
    'Structural':      STYLE_TRADE_STRUCT,
}

_BAND_TO_STYLE = {
    'Critical': STYLE_BAND_CRITICAL,
    'Major':    STYLE_BAND_MAJOR,
    'Minor':    STYLE_BAND_MINOR,
}


# Column names that hold numeric data (right-aligned with the NUMBER style).
_NUMBER_COLUMNS = {
    'Clash #', 'Score', 'Element A ID', 'Element B ID', 'Comment count',
    'Midpoint X (ft)', 'Midpoint Y (ft)', 'Midpoint Z (ft)',
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_xlsx(clashes, out_path, filter_predicate=None,
               test_name_lookup=None, group_lookup=None):
    """Write an XLSX summary of `clashes` to `out_path`.

    Returns the count of data rows written (excludes the header).

    `filter_predicate(clash) -> bool` lets the caller drop clashes (same
    contract as bcf.build_bcf_zip). `test_name_lookup` maps test_id ->
    display name; `group_lookup` maps group_id -> a short issue label
    (e.g. "#4" or the issue title). Both optional.

    Atomic-write: writes to a sibling .tmp file first, then renames onto
    `out_path`. Same robustness pattern as the BCF builder.
    """
    if not out_path:
        raise ValueError("out_path is required")
    clashes = clashes or []
    if filter_predicate is not None:
        clashes = [c for c in clashes if c and filter_predicate(c)]
    else:
        clashes = [c for c in clashes if c]

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    tmp_path = out_path + '.tmp'

    try:
        # Build the dynamic XML parts in memory.
        sst = _SharedStringTable()
        sheet_xml = _build_sheet_xml(clashes, sst, test_name_lookup or {},
                                     group_lookup or {})
        shared_strings_xml = _build_shared_strings_xml(sst)

        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Static files first (don't depend on the data).
            zf.writestr('[Content_Types].xml', _CONTENT_TYPES_XML)
            zf.writestr('_rels/.rels', _ROOT_RELS_XML)
            zf.writestr('xl/workbook.xml', _WORKBOOK_XML)
            zf.writestr('xl/_rels/workbook.xml.rels', _WORKBOOK_RELS_XML)
            zf.writestr('xl/styles.xml', _STYLES_XML)
            # Dynamic files.
            zf.writestr('xl/sharedStrings.xml', shared_strings_xml)
            zf.writestr('xl/worksheets/sheet1.xml', sheet_xml)

        if os.path.exists(out_path):
            os.remove(out_path)
        os.rename(tmp_path, out_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

    return len(clashes)


# ---------------------------------------------------------------------------
# Shared string table — Excel's deduped-string mechanism
# ---------------------------------------------------------------------------

class _SharedStringTable(object):
    """XLSX stores most string cells by index into a shared strings list,
    so 100 cells with the same value (e.g. "Mechanical") share one entry.
    Smaller files, faster Excel parsing.
    """
    def __init__(self):
        self._ordered = []       # ordered list of unique strings
        self._index = {}         # string → index

    def add(self, s):
        if s in self._index:
            return self._index[s]
        idx = len(self._ordered)
        self._ordered.append(s)
        self._index[s] = idx
        return idx

    def all_strings(self):
        return self._ordered

    def __len__(self):
        return len(self._ordered)


# ---------------------------------------------------------------------------
# Sheet + shared-strings XML builders
# ---------------------------------------------------------------------------

def _build_sheet_xml(clashes, sst, test_name_lookup, group_lookup):
    """sheet1.xml — column widths, frozen-pane header, all rows, auto-filter."""
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    parts.append(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')

    # Frozen top row so the header stays visible while scrolling.
    parts.append('<sheetViews>')
    parts.append('<sheetView workbookViewId="0">')
    parts.append('<pane ySplit="1" topLeftCell="A2" '
                 'activePane="bottomLeft" state="frozen"/>')
    parts.append('</sheetView>')
    parts.append('</sheetViews>')

    parts.append('<sheetFormatPr defaultRowHeight="15"/>')

    # Per-column widths.
    parts.append('<cols>')
    for i, width in enumerate(COL_WIDTHS):
        parts.append('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'
                     .format(i + 1, width))
    parts.append('</cols>')

    parts.append('<sheetData>')

    # Header row — taller so the bold label has breathing room.
    parts.append('<row r="1" customHeight="1" ht="22">')
    for col_idx, header in enumerate(COLUMNS):
        cell_ref = _col_letter(col_idx + 1) + '1'
        s_idx = sst.add(header)
        parts.append('<c r="{0}" s="{1}" t="s"><v>{2}</v></c>'.format(
            cell_ref, STYLE_HEADER, s_idx))
    parts.append('</row>')

    # Data rows.
    for row_idx, clash in enumerate(clashes):
        row_num = row_idx + 2  # row 1 is header
        row_data = _row_for(clash, test_name_lookup, group_lookup)
        parts.append('<row r="{0}">'.format(row_num))
        for col_idx, value in enumerate(row_data):
            cell_ref = _col_letter(col_idx + 1) + str(row_num)
            parts.append(_cell_xml(cell_ref, value, col_idx, sst))
        parts.append('</row>')

    parts.append('</sheetData>')

    # AutoFilter — gives every column a dropdown for sort/filter in Excel.
    if clashes:
        last_col = _col_letter(len(COLUMNS))
        last_row = len(clashes) + 1
        parts.append('<autoFilter ref="A1:{0}{1}"/>'.format(last_col, last_row))
    else:
        parts.append('<autoFilter ref="A1:{0}1"/>'.format(_col_letter(len(COLUMNS))))

    parts.append('</worksheet>')
    return ''.join(parts)


def _build_shared_strings_xml(sst):
    """sharedStrings.xml — the deduped string table referenced by sheet1.xml."""
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    parts.append('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                 'count="{0}" uniqueCount="{0}">'.format(len(sst)))
    for s in sst.all_strings():
        # xml:space="preserve" so leading/trailing whitespace isn't stripped
        # by Excel (would break things like "  indented note").
        parts.append('<si><t xml:space="preserve">{0}</t></si>'.format(escape(s)))
    parts.append('</sst>')
    return ''.join(parts)


def _cell_xml(ref, value, col_idx, sst):
    """Emit one cell XML, choosing style based on the column AND value.

    Status / Trade columns get the colored-pill styles; numeric columns
    get the right-aligned NUMBER style; everything else gets the plain
    BODY style.

    Empty cells still get a style (for the border) but no value.
    """
    col_name = COLUMNS[col_idx]

    # Style selection
    if col_name == 'Importance':
        style = _BAND_TO_STYLE.get(_safe_str(value), STYLE_BODY)
    elif col_name == 'Status':
        style = _STATUS_TO_STYLE.get(_safe_str(value), STYLE_BODY)
    elif col_name == 'Trade':
        style = _TRADE_TO_STYLE.get(_safe_str(value), STYLE_BODY)
    elif col_name in _NUMBER_COLUMNS:
        style = STYLE_NUMBER
    else:
        style = STYLE_BODY

    # Empty cell: just the style attribute, no value.
    if value is None or value == '':
        return '<c r="{0}" s="{1}"/>'.format(ref, style)

    # Numeric path — write inline as <v>123</v> (Excel parses as number).
    if isinstance(value, bool):
        # bool is a subclass of int; treat as string to avoid TRUE/FALSE
        # rendering oddly.
        s_idx = sst.add(_safe_str(value))
        return '<c r="{0}" s="{1}" t="s"><v>{2}</v></c>'.format(ref, style, s_idx)
    if isinstance(value, (int, float)):
        return '<c r="{0}" s="{1}"><v>{2}</v></c>'.format(ref, style, value)
    # Strings that look numeric (e.g. element_id read from JSON might be
    # int already; this branch handles the str case)
    s_value = _safe_str(value)
    if col_name in _NUMBER_COLUMNS and _looks_numeric(s_value):
        return '<c r="{0}" s="{1}"><v>{2}</v></c>'.format(ref, style, s_value)

    # String path — via the shared strings table.
    s_idx = sst.add(s_value)
    return '<c r="{0}" s="{1}" t="s"><v>{2}</v></c>'.format(ref, style, s_idx)


def _row_for(clash, test_name_lookup=None, group_lookup=None):
    """Order MUST match COLUMNS above. Built on report_model so band,
    score, trade, pair and reason match the app + the HTML/PDF exports."""
    group_lookup = group_lookup or {}
    r = rm.row_for(clash, test_name_lookup or {})
    gid = r['group_id']
    issue = group_lookup.get(gid) or ('grouped' if gid else '-')

    return [
        r['seq'],
        r['band'],
        r['score'],
        r['status'],
        r['trade'] or '-',
        issue,
        r['test'],
        r['kind'],
        r['reason'],
        r['pair'],
        r['a_name'],
        r['a_id'],
        r['a_cat'],
        r['a_src'],
        r['b_name'],
        r['b_id'],
        r['b_cat'],
        r['b_src'],
        r['level'],
        r['x'],
        r['y'],
        r['z'],
        r['first_seen'],
        r['last_seen'],
        r['comment_count'],
        r['latest_comment'],
        r['latest_comment_author'],
        r['id'],
    ]


def _col_letter(n):
    """1-indexed column number → Excel column letter (1='A', 27='AA', etc.)."""
    result = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(ord('A') + r) + result
    return result


def _looks_numeric(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _coord(value):
    """Coordinate as a float (2 decimals' worth of precision); Excel
    stores the full float and respects column number-format on display."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _safe_str(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', 'replace')
        except Exception:
            return ''
    return u'{}'.format(value)


# ---------------------------------------------------------------------------
# Static XML files (don't depend on the data)
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    u'<Default Extension="rels" '
    u'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    u'<Default Extension="xml" ContentType="application/xml"/>'
    u'<Override PartName="/xl/workbook.xml" '
    u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    u'<Override PartName="/xl/worksheets/sheet1.xml" '
    u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    u'<Override PartName="/xl/styles.xml" '
    u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    u'<Override PartName="/xl/sharedStrings.xml" '
    u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    u'</Types>'
)

_ROOT_RELS_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    u'<Relationship Id="rId1" '
    u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    u'Target="xl/workbook.xml"/>'
    u'</Relationships>'
)

_WORKBOOK_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    u'<sheets>'
    u'<sheet name="Clashes" sheetId="1" r:id="rId1"/>'
    u'</sheets>'
    u'</workbook>'
)

_WORKBOOK_RELS_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    u'<Relationship Id="rId1" '
    u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    u'Target="worksheets/sheet1.xml"/>'
    u'<Relationship Id="rId2" '
    u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    u'Target="styles.xml"/>'
    u'<Relationship Id="rId3" '
    u'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
    u'Target="sharedStrings.xml"/>'
    u'</Relationships>'
)


# ---------------------------------------------------------------------------
# Styles XML — the heaviest static file. Every formatted cell references
# one of the cellXfs entries by index. Order MUST match the STYLE_*
# constants at the top of the file.
# ---------------------------------------------------------------------------

# Color palette (Excel ARGB hex — 8 chars, alpha first).
_C_HEADER_BG     = 'FF2D3748'   # dark slate (matches dbHMS form headers)
_C_WHITE         = 'FFFFFFFF'
_C_BLACK         = 'FF1A202C'
_C_BORDER        = 'FFE2E8F0'
_C_OPEN_BG       = 'FFE53E3E'
_C_REVIEWED_BG   = 'FFD69E2E'
_C_APPROVED_BG   = 'FF2B6CB0'
_C_RESOLVED_BG   = 'FF38A169'
_C_TRADE_MECH    = 'FFDBE5EE'
_C_TRADE_ELEC    = 'FFEDE2C6'
_C_TRADE_PLUMB   = 'FFD5E5E0'
_C_TRADE_FP      = 'FFEAD2D2'
_C_TRADE_TECH    = 'FFE0D5E8'
_C_TRADE_ARCH    = 'FFE5E0D5'
_C_TRADE_STRUCT  = 'FFD8DCE0'
_C_TRADE_FG      = 'FF2D3748'   # dark text inside trade pills
# Importance band fills (light, matching coord.html band chips).
_C_BAND_CRIT     = 'FFFCEBEB'
_C_BAND_MAJOR    = 'FFFAEEDA'
_C_BAND_MINOR    = 'FFF1EFE8'


def _build_styles_xml():
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    parts.append(
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')

    # ---- Fonts ----
    # 0: default body (dark text)
    # 1: header (white bold, slightly larger)
    # 2: status pill (white bold, smaller)
    # 3: trade pill (dark bold, smaller)
    parts.append('<fonts count="4">')
    parts.append('<font><sz val="11"/><color rgb="{0}"/><name val="Calibri"/></font>'
                 .format(_C_BLACK))
    parts.append('<font><b/><sz val="11"/><color rgb="{0}"/><name val="Calibri"/></font>'
                 .format(_C_WHITE))
    parts.append('<font><b/><sz val="10"/><color rgb="{0}"/><name val="Calibri"/></font>'
                 .format(_C_WHITE))
    parts.append('<font><b/><sz val="10"/><color rgb="{0}"/><name val="Calibri"/></font>'
                 .format(_C_TRADE_FG))
    parts.append('</fonts>')

    # ---- Fills ----
    # Excel REQUIRES indices 0 = none and 1 = gray125 (legacy).
    # 2: header bg
    # 3-6: status (Open, Reviewed, Approved, Resolved)
    # 7-13: trade (mechanical, electrical, plumbing, fp, tech, arch, struct)
    # 14-16: importance band (critical, major, minor)
    fill_colors = [
        _C_HEADER_BG,
        _C_OPEN_BG, _C_REVIEWED_BG, _C_APPROVED_BG, _C_RESOLVED_BG,
        _C_TRADE_MECH, _C_TRADE_ELEC, _C_TRADE_PLUMB, _C_TRADE_FP,
        _C_TRADE_TECH, _C_TRADE_ARCH, _C_TRADE_STRUCT,
        _C_BAND_CRIT, _C_BAND_MAJOR, _C_BAND_MINOR,
    ]
    parts.append('<fills count="{0}">'.format(2 + len(fill_colors)))
    parts.append('<fill><patternFill patternType="none"/></fill>')
    parts.append('<fill><patternFill patternType="gray125"/></fill>')
    for color in fill_colors:
        parts.append('<fill><patternFill patternType="solid">'
                     '<fgColor rgb="{0}"/></patternFill></fill>'.format(color))
    parts.append('</fills>')

    # ---- Borders ----
    # 0: no border
    # 1: thin gray border on all 4 sides (the body cell border)
    parts.append('<borders count="2">')
    parts.append('<border><left/><right/><top/><bottom/><diagonal/></border>')
    parts.append('<border>'
                 '<left style="thin"><color rgb="{0}"/></left>'
                 '<right style="thin"><color rgb="{0}"/></right>'
                 '<top style="thin"><color rgb="{0}"/></top>'
                 '<bottom style="thin"><color rgb="{0}"/></bottom>'
                 '<diagonal/></border>'.format(_C_BORDER))
    parts.append('</borders>')

    # ---- cellStyleXfs ----
    # Required by the spec; one base entry that other xfs derive from.
    parts.append('<cellStyleXfs count="1">')
    parts.append('<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>')
    parts.append('</cellStyleXfs>')

    # ---- cellXfs ----
    # ORDER MATTERS — must match the STYLE_* constants at the top of the file.
    # Index 0 = STYLE_DEFAULT, 1 = STYLE_HEADER, etc.
    parts.append('<cellXfs count="18">')

    # 0 STYLE_DEFAULT: bare default
    parts.append('<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>')

    # 1 STYLE_HEADER: white bold on dark slate, bordered, left-aligned, vert center
    parts.append(
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" '
        'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center" wrapText="1"/>'
        '</xf>')

    # 2 STYLE_BODY: default font, bordered, top-aligned with text wrap
    parts.append(
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" '
        'applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/>'
        '</xf>')

    # 3 STYLE_NUMBER: right-aligned with border (for #, IDs, midpoints)
    parts.append(
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" '
        'applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="top"/>'
        '</xf>')

    # 4-7 STATUS pills (white bold on color, centered)
    # Fill indices 3, 4, 5, 6 = Open, Reviewed, Approved, Resolved
    for fill_idx in (3, 4, 5, 6):
        parts.append(
            '<xf numFmtId="0" fontId="2" fillId="{0}" borderId="1" xfId="0" '
            'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="center" vertical="center"/>'
            '</xf>'.format(fill_idx))

    # 8-14 TRADE pills (dark bold on light color, centered)
    # Fill indices 7-13 = mechanical, electrical, plumbing, fp, tech, arch, struct
    for fill_idx in (7, 8, 9, 10, 11, 12, 13):
        parts.append(
            '<xf numFmtId="0" fontId="3" fillId="{0}" borderId="1" xfId="0" '
            'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="center" vertical="center"/>'
            '</xf>'.format(fill_idx))

    # 15-17 BAND pills (dark bold on light band color, centered)
    # Fill indices 14-16 = Critical, Major, Minor
    for fill_idx in (14, 15, 16):
        parts.append(
            '<xf numFmtId="0" fontId="3" fillId="{0}" borderId="1" xfId="0" '
            'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="center" vertical="center"/>'
            '</xf>'.format(fill_idx))

    parts.append('</cellXfs>')

    # cellStyles (for built-in named styles like "Normal")
    parts.append('<cellStyles count="1">')
    parts.append('<cellStyle name="Normal" xfId="0" builtinId="0"/>')
    parts.append('</cellStyles>')

    parts.append('</styleSheet>')
    return ''.join(parts)


_STYLES_XML = _build_styles_xml()
