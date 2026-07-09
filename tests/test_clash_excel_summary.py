"""Tests for lib/clash_report/excel_summary.py — XLSX summary export.

Pure-data module — no Revit, no WPF. Verifies:
  * Zip structure matches the OOXML / XLSX spec
  * All required parts are present and well-formed XML
  * Sheet rows match the input clashes (header + N data rows)
  * Status / Trade columns use the correct colored-pill style indices
  * Numeric columns (Clash #, IDs, midpoints) use the right-aligned style
  * Auto-filter range covers all data rows
  * Frozen pane on row 1
  * Per-column widths set
  * Atomic write (no .tmp leftovers)
  * Empty input + missing fields handled gracefully
  * Special characters round-trip through shared strings
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_report import excel_summary  # noqa: E402


# Namespaces used inside the XLSX XML files. We bind them so xpath
# queries below can write `s:row` instead of the full URL.
NS = {
    's':  'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r':  'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'ct': 'http://schemas.openxmlformats.org/package/2006/content-types',
    'rl': 'http://schemas.openxmlformats.org/package/2006/relationships',
}


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _sample_clash(clash_id="clash-1", seq=1, status="Open",
                  assignee="Mechanical", with_comments=False):
    clash = {
        "id": clash_id,
        "seq": seq,
        "kind": "hard",
        "status": status,
        "assignee": assignee,
        "test_id": "default-mep-vs-architecture",
        "first_seen_run": "2026-05-06T05:11:16Z",
        "last_seen_run": "2026-05-06T05:21:15Z",
        "ref_a": {
            "source": "host",
            "element_id": 1250520,
            "category": "Ducts",
            "name": "Round Duct - 12in",
        },
        "ref_b": {
            "source": "link:Architectural",
            "element_id": 1240656,
            "category": "Walls",
            "name": "Generic - 8in",
        },
        "midpoint": [10.0, 20.0, 5.0],
        "comments": [],
    }
    if with_comments:
        clash["comments"] = [
            {"author": "Alice", "body": "First note"},
            {"author": "Bob", "body": "Latest note — talk to architect"},
        ]
    return clash


def _build_to_temp(clashes, **kwargs):
    """Helper: build the XLSX to a temp file, return its path (caller cleans up)."""
    tmpdir = tempfile.mkdtemp()
    out = os.path.join(tmpdir, "out.xlsx")
    excel_summary.build_xlsx(clashes, out, **kwargs)
    return out


def _read_part(zip_path, part_name):
    """Open the XLSX and return the contents of a zip member as bytes."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        return zf.read(part_name)


def _read_part_xml(zip_path, part_name):
    """Open the XLSX and return the parsed ElementTree of a zip member."""
    return ET.fromstring(_read_part(zip_path, part_name))


# ---------------------------------------------------------------------------
# Zip structure
# ---------------------------------------------------------------------------

class ZipStructureTests(unittest.TestCase):

    def test_required_top_level_parts_present(self):
        out = _build_to_temp([_sample_clash()])
        try:
            with zipfile.ZipFile(out, 'r') as zf:
                names = set(zf.namelist())
            for required in (
                '[Content_Types].xml',
                '_rels/.rels',
                'xl/workbook.xml',
                'xl/_rels/workbook.xml.rels',
                'xl/styles.xml',
                'xl/sharedStrings.xml',
                'xl/worksheets/sheet1.xml',
            ):
                self.assertIn(required, names)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_all_parts_are_well_formed_xml(self):
        out = _build_to_temp([_sample_clash(seq=i) for i in range(1, 4)])
        try:
            with zipfile.ZipFile(out, 'r') as zf:
                for name in zf.namelist():
                    if not name.endswith('.xml') and not name.endswith('.rels'):
                        continue
                    body = zf.read(name)
                    try:
                        ET.fromstring(body)
                    except ET.ParseError as ex:
                        self.fail("part {!r} is not well-formed XML: {}"
                                  .format(name, ex))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# workbook.xml
# ---------------------------------------------------------------------------

class WorkbookXmlTests(unittest.TestCase):

    def test_one_sheet_named_clashes(self):
        out = _build_to_temp([_sample_clash()])
        try:
            wb = _read_part_xml(out, 'xl/workbook.xml')
            sheets = wb.findall('.//s:sheet', NS)
            self.assertEqual(len(sheets), 1)
            self.assertEqual(sheets[0].get('name'), 'Clashes')
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# sheet1.xml — rows / cells
# ---------------------------------------------------------------------------

class SheetXmlTests(unittest.TestCase):

    def test_header_row_uses_header_style(self):
        out = _build_to_temp([_sample_clash()])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            row1 = sheet.find('.//s:row[@r="1"]', NS)
            self.assertIsNotNone(row1)
            for c in row1.findall('s:c', NS):
                self.assertEqual(c.get('s'), str(excel_summary.STYLE_HEADER),
                                 "every header cell should use STYLE_HEADER")
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_row_count_matches_clashes(self):
        clashes = [_sample_clash("a", 1), _sample_clash("b", 2),
                   _sample_clash("c", 3)]
        out = _build_to_temp(clashes)
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            rows = sheet.findall('.//s:row', NS)
            # 1 header + 3 data
            self.assertEqual(len(rows), 4)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_status_cell_uses_status_pill_style(self):
        # Status is column 4 (Excel 'D'): Clash #, Importance, Score, Status.
        # For an "Open" clash, that cell's style should be STYLE_STATUS_OPEN.
        out = _build_to_temp([_sample_clash(status="Open")])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            data_row = sheet.find('.//s:row[@r="2"]', NS)
            status_cell = data_row.find('s:c[@r="D2"]', NS)
            self.assertEqual(status_cell.get('s'),
                             str(excel_summary.STYLE_STATUS_OPEN))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_resolved_status_uses_resolved_style(self):
        out = _build_to_temp([_sample_clash(status="Resolved")])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            cell = sheet.find('.//s:row[@r="2"]/s:c[@r="D2"]', NS)
            self.assertEqual(cell.get('s'),
                             str(excel_summary.STYLE_STATUS_RESOLVED))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_importance_cell_uses_band_style(self):
        # Importance is column 2 (Excel 'B'). A Critical clash uses the
        # Critical band pill style.
        clash = _sample_clash()
        clash['importance'] = {'score': 88, 'band': 'Critical'}
        out = _build_to_temp([clash])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            cell = sheet.find('.//s:row[@r="2"]/s:c[@r="B2"]', NS)
            self.assertEqual(cell.get('s'),
                             str(excel_summary.STYLE_BAND_CRITICAL))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_trade_cell_uses_trade_pill_style(self):
        # Trade is column 5 (Excel 'E'). For Mechanical:
        out = _build_to_temp([_sample_clash(assignee="Mechanical")])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            cell = sheet.find('.//s:row[@r="2"]/s:c[@r="E2"]', NS)
            self.assertEqual(cell.get('s'),
                             str(excel_summary.STYLE_TRADE_MECHANICAL))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_clash_number_cell_uses_number_style(self):
        # Clash # is column A. Should use the right-aligned NUMBER style.
        out = _build_to_temp([_sample_clash(seq=42)])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            cell = sheet.find('.//s:row[@r="2"]/s:c[@r="A2"]', NS)
            self.assertEqual(cell.get('s'), str(excel_summary.STYLE_NUMBER))
            # And the value should be the integer, written as a numeric.
            self.assertEqual(cell.find('s:v', NS).text, '42')
            # Numeric cells don't have t="s" (string-shared)
            self.assertIsNone(cell.get('t'))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_frozen_pane_on_header_row(self):
        out = _build_to_temp([_sample_clash()])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            pane = sheet.find('.//s:pane', NS)
            self.assertIsNotNone(pane)
            self.assertEqual(pane.get('state'), 'frozen')
            self.assertEqual(pane.get('ySplit'), '1')
            self.assertEqual(pane.get('topLeftCell'), 'A2')
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_autofilter_range_covers_all_data(self):
        out = _build_to_temp([_sample_clash(seq=i) for i in range(1, 6)])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            af = sheet.find('.//s:autoFilter', NS)
            self.assertIsNotNone(af)
            # 5 data + 1 header = 6 rows; 28 columns ends at AB
            self.assertEqual(af.get('ref'), 'A1:AB6')
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_column_widths_present_for_every_column(self):
        out = _build_to_temp([_sample_clash()])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            cols = sheet.findall('.//s:col', NS)
            self.assertEqual(len(cols), len(excel_summary.COLUMNS))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# styles.xml — verify all 15 expected cell styles are emitted with the
# right ordering (so the STYLE_* constants point at the right xfs)
# ---------------------------------------------------------------------------

class StylesXmlTests(unittest.TestCase):

    def test_cellxfs_count_matches_style_constants(self):
        out = _build_to_temp([_sample_clash()])
        try:
            styles = _read_part_xml(out, 'xl/styles.xml')
            cell_xfs = styles.find('s:cellXfs', NS)
            xfs = cell_xfs.findall('s:xf', NS)
            self.assertEqual(len(xfs), 18,
                             "should have 18 cellXfs entries (default + "
                             "header + body + number + 4 status + 7 trades "
                             "+ 3 bands)")
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_fills_include_all_status_and_trade_colors(self):
        out = _build_to_temp([_sample_clash()])
        try:
            styles = _read_part_xml(out, 'xl/styles.xml')
            fills = styles.find('s:fills', NS)
            fill_colors = []
            for fill in fills.findall('s:fill', NS):
                fg = fill.find('s:patternFill/s:fgColor', NS)
                if fg is not None and fg.get('rgb'):
                    fill_colors.append(fg.get('rgb'))
            # All four status colors present
            for color in ('FFE53E3E', 'FFD69E2E', 'FF2B6CB0', 'FF38A169'):
                self.assertIn(color, fill_colors,
                              "status color {} missing from fills".format(color))
            # And the trade colors (just spot-check Mechanical + Plumbing)
            for color in ('FFDBE5EE', 'FFD5E5E0'):
                self.assertIn(color, fill_colors,
                              "trade color {} missing from fills".format(color))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# sharedStrings.xml — content + special character handling
# ---------------------------------------------------------------------------

class SharedStringsTests(unittest.TestCase):

    def test_header_strings_stored_in_shared_strings(self):
        out = _build_to_temp([_sample_clash()])
        try:
            sst = _read_part_xml(out, 'xl/sharedStrings.xml')
            text_values = [t.text for t in sst.findall('.//s:t', NS)]
            for header in excel_summary.COLUMNS:
                self.assertIn(header, text_values,
                              "header {!r} missing from shared strings".format(header))
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_unicode_in_element_name_round_trips(self):
        clash = _sample_clash()
        clash['ref_a']['name'] = u'Round Duct — 12° bend'
        out = _build_to_temp([clash])
        try:
            sst = _read_part_xml(out, 'xl/sharedStrings.xml')
            text_values = [t.text for t in sst.findall('.//s:t', NS)]
            self.assertIn(u'Round Duct — 12° bend', text_values)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_xml_special_chars_are_escaped(self):
        clash = _sample_clash()
        clash['ref_a']['name'] = 'Duct <12in> & "supply"'
        out = _build_to_temp([clash])
        try:
            sst_bytes = _read_part(out, 'xl/sharedStrings.xml')
            # Raw XML must NOT contain unescaped <12 or & or quotes inside <t>
            self.assertIn(b'&lt;12in&gt;', sst_bytes)
            self.assertIn(b'&amp;', sst_bytes)
            # And it should still parse
            ET.fromstring(sst_bytes)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# Filter predicate
# ---------------------------------------------------------------------------

class FilterTests(unittest.TestCase):

    def test_predicate_drops_clashes_returning_false(self):
        clashes = [
            _sample_clash("a", assignee="Mechanical"),
            _sample_clash("b", assignee="Plumbing"),
            _sample_clash("c", assignee="Electrical"),
        ]
        out = _build_to_temp(
            clashes,
            filter_predicate=lambda c: c.get("assignee") == "Plumbing")
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            data_rows = [r for r in sheet.findall('.//s:row', NS)
                         if int(r.get('r')) >= 2]
            self.assertEqual(len(data_rows), 1)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class RobustnessTests(unittest.TestCase):

    def test_empty_clash_list_still_produces_valid_xlsx(self):
        out = _build_to_temp([])
        try:
            self.assertTrue(os.path.isfile(out))
            with zipfile.ZipFile(out, 'r') as zf:
                # All required parts present even with no data rows
                names = set(zf.namelist())
                self.assertIn('xl/worksheets/sheet1.xml', names)
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            data_rows = [r for r in sheet.findall('.//s:row', NS)
                         if int(r.get('r')) >= 2]
            self.assertEqual(len(data_rows), 0)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_bare_clash_with_only_id_and_seq_doesnt_crash(self):
        out = _build_to_temp([{'id': 'x', 'seq': 1}])
        try:
            sheet = _read_part_xml(out, 'xl/worksheets/sheet1.xml')
            data_row = sheet.find('.//s:row[@r="2"]', NS)
            self.assertIsNotNone(data_row)
        finally:
            os.remove(out); os.rmdir(os.path.dirname(out))

    def test_atomic_write_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.xlsx")
            excel_summary.build_xlsx([_sample_clash()], out)
            leftovers = [f for f in os.listdir(tmpdir) if f.endswith('.tmp')]
            self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# Column letter helper
# ---------------------------------------------------------------------------

class ColLetterTests(unittest.TestCase):

    def test_single_letter_columns(self):
        self.assertEqual(excel_summary._col_letter(1), 'A')
        self.assertEqual(excel_summary._col_letter(2), 'B')
        self.assertEqual(excel_summary._col_letter(26), 'Z')

    def test_double_letter_columns(self):
        self.assertEqual(excel_summary._col_letter(27), 'AA')
        self.assertEqual(excel_summary._col_letter(28), 'AB')  # last column
        self.assertEqual(excel_summary._col_letter(52), 'AZ')


if __name__ == "__main__":
    unittest.main()
