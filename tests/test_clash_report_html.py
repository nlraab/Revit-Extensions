"""Tests for lib/clash_report/html.py.

Pure-data — no Revit. Verifies HTML structure, count math, filter
predicate, escaping, optional thumbnails (mocked file presence), and
the "no clashes" empty state.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_report import html as html_mod  # noqa: E402


def _clash(seq, status='Open', trade='Mechanical', test_id='t1',
           ref_a_name='Duct', ref_b_name='Wall', clash_id=None,
           comments=None, history=None):
    cid = clash_id or 'clash-{}'.format(seq)
    return {
        'id':       cid,
        'seq':      seq,
        'status':   status,
        'assignee': trade,
        'test_id':  test_id,
        'kind':     'hard',
        'ref_a':    {'element_id': 100 + seq, 'name': ref_a_name, 'source': 'host'},
        'ref_b':    {'element_id': 200 + seq, 'name': ref_b_name, 'source': 'link:Architectural'},
        'comments': comments or [],
        'history':  history or [],
    }


class _TempOutMixin(object):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._out = os.path.join(self._tmp, "report.html")

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass

    def _read(self):
        with open(self._out, 'r', encoding='utf-8') as f:
            return f.read()


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class StructureTests(_TempOutMixin, unittest.TestCase):

    def test_returns_count_of_clashes_written(self):
        n = html_mod.build_html(
            [_clash(1), _clash(2), _clash(3)], self._out)
        self.assertEqual(n, 3)

    def test_html_well_formed_doctype(self):
        html_mod.build_html([_clash(1)], self._out)
        body = self._read()
        self.assertTrue(body.startswith("<!DOCTYPE html>"))
        self.assertIn("<html", body)
        self.assertIn("</html>", body)
        self.assertIn("<head>", body)
        self.assertIn("</head>", body)

    def test_includes_dbhms_branding(self):
        html_mod.build_html([_clash(1)], self._out,
                            project_name="Acme Tower")
        body = self._read()
        self.assertIn("Acme Tower", body)
        self.assertIn(">db<", body)
        self.assertIn(">HMS<", body)
        self.assertIn("Clash Report", body)

    def test_includes_metadata_lines(self):
        html_mod.build_html(
            [_clash(1)], self._out,
            project_name="Acme",
            generated_by="nathan",
            now_iso="2026-05-08T14:00:00Z",
            filter_description="Mechanical only",
        )
        body = self._read()
        self.assertIn("nathan", body)
        # Timestamps render in a friendly short form (YYYY-MM-DD HH:MM).
        self.assertIn("2026-05-08 14:00", body)
        self.assertIn("Mechanical only", body)


# ---------------------------------------------------------------------------
# Counts + summary
# ---------------------------------------------------------------------------

class CountsTests(_TempOutMixin, unittest.TestCase):

    def test_status_counts_render(self):
        clashes = [
            _clash(1, status='Open'),
            _clash(2, status='Open'),
            _clash(3, status='Resolved'),
        ]
        html_mod.build_html(clashes, self._out)
        body = self._read()
        # We don't pin to exact HTML but the counts should be visible.
        self.assertIn("Open", body)
        self.assertIn("Resolved", body)

    def test_trade_counts_render(self):
        clashes = [
            _clash(1, trade='Mechanical'),
            _clash(2, trade='Mechanical'),
            _clash(3, trade='Plumbing'),
        ]
        html_mod.build_html(clashes, self._out)
        body = self._read()
        self.assertIn("Mechanical", body)
        self.assertIn("Plumbing", body)

    def test_total_count_in_metadata(self):
        clashes = [_clash(1), _clash(2), _clash(3), _clash(4)]
        html_mod.build_html(clashes, self._out)
        body = self._read()
        # The "Total clashes: 4" line should be in metadata.
        self.assertIn("Total clashes:", body)
        self.assertIn(">4<", body)


# ---------------------------------------------------------------------------
# Filter predicate
# ---------------------------------------------------------------------------

class FilterTests(_TempOutMixin, unittest.TestCase):

    def test_predicate_filters_clashes(self):
        clashes = [
            _clash(1, status='Open'),
            _clash(2, status='Resolved'),
            _clash(3, status='Open'),
        ]
        n = html_mod.build_html(
            clashes, self._out,
            filter_predicate=lambda c: c.get('status') == 'Open')
        self.assertEqual(n, 2)
        body = self._read()
        # Clash #2 should not appear in the document.
        self.assertNotIn("Clash #2", body)

    def test_predicate_exception_skips_silently(self):
        # A predicate that raises on one clash shouldn't kill the whole
        # build — that clash is just dropped.
        def bad(c):
            if c.get('seq') == 2:
                raise ValueError("boom")
            return True
        clashes = [_clash(1), _clash(2), _clash(3)]
        n = html_mod.build_html(clashes, self._out, filter_predicate=bad)
        self.assertEqual(n, 2)

    def test_no_predicate_includes_all(self):
        n = html_mod.build_html(
            [_clash(1), _clash(2), _clash(3)], self._out)
        self.assertEqual(n, 3)


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

class EmptyStateTests(_TempOutMixin, unittest.TestCase):

    def test_zero_clashes_renders_empty_message(self):
        n = html_mod.build_html([], self._out)
        self.assertEqual(n, 0)
        body = self._read()
        self.assertIn("No clashes", body)

    def test_filter_excluding_all_renders_empty(self):
        clashes = [_clash(1), _clash(2)]
        n = html_mod.build_html(
            clashes, self._out,
            filter_predicate=lambda c: False)
        self.assertEqual(n, 0)


# ---------------------------------------------------------------------------
# Escaping — defensive against malicious data
# ---------------------------------------------------------------------------

class EscapingTests(_TempOutMixin, unittest.TestCase):

    def test_clash_with_html_in_name_is_escaped(self):
        clash = _clash(1, ref_a_name="<script>alert('xss')</script>")
        html_mod.build_html([clash], self._out)
        body = self._read()
        # Should NOT contain an unescaped <script>
        self.assertNotIn("<script>alert", body)
        # Should contain the escaped form.
        self.assertIn("&lt;script&gt;", body)

    def test_comment_body_is_escaped(self):
        clash = _clash(1, comments=[
            {'author': 'a', 'at': '2026-05-08T14:00:00Z',
             'body': 'A <b>bold</b> claim & such'}
        ])
        html_mod.build_html([clash], self._out)
        body = self._read()
        self.assertNotIn("A <b>bold</b> claim", body)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", body)
        self.assertIn("&amp;", body)


# ---------------------------------------------------------------------------
# Comments + history rendering
# ---------------------------------------------------------------------------

class CommentsHistoryTests(_TempOutMixin, unittest.TestCase):

    def test_comments_section_renders_when_present(self):
        clash = _clash(1, comments=[
            {'author': 'nathan', 'at': '2026-05-08T14:00:00Z',
             'body': 'Looks fine to me'}
        ])
        html_mod.build_html([clash], self._out)
        body = self._read()
        self.assertIn("Looks fine to me", body)
        self.assertIn("nathan", body)

    def test_comments_section_skipped_when_empty(self):
        html_mod.build_html([_clash(1)], self._out)
        body = self._read()
        # The section header shouldn't appear if there are no comments.
        self.assertNotIn(">Comments (", body)

    def test_history_renders_with_action_arrow(self):
        clash = _clash(1, history=[
            {'author': 'nathan', 'at': '2026-05-08T14:00:00Z',
             'action': 'status_changed',
             'before': 'Open', 'after': 'Resolved'}
        ])
        html_mod.build_html([clash], self._out)
        body = self._read()
        self.assertIn("Open", body)
        self.assertIn("Resolved", body)
        self.assertIn("→", body)


# ---------------------------------------------------------------------------
# Test name lookup
# ---------------------------------------------------------------------------

class TestNameLookupTests(_TempOutMixin, unittest.TestCase):

    def test_test_name_resolved_from_lookup(self):
        clash = _clash(1, test_id='t-mep-arch')
        html_mod.build_html(
            [clash], self._out,
            test_name_lookup={'t-mep-arch': 'MEP vs Arch'})
        body = self._read()
        self.assertIn("MEP vs Arch", body)

    def test_unknown_test_falls_back(self):
        # No name and no lookup hit: show the raw test id (still identifies
        # which test found it) rather than a generic placeholder.
        clash = _clash(1, test_id='t-unknown')
        html_mod.build_html([clash], self._out, test_name_lookup={})
        body = self._read()
        self.assertIn("t-unknown", body)

    def test_missing_test_id_falls_back_to_placeholder(self):
        clash = _clash(1)
        clash.pop('test_id', None)
        html_mod.build_html([clash], self._out, test_name_lookup={})
        body = self._read()
        self.assertIn("(unknown test)", body)


# ---------------------------------------------------------------------------
# Thumbnails — base64 inline
# ---------------------------------------------------------------------------

class ThumbnailTests(_TempOutMixin, unittest.TestCase):

    def _make_fake_png(self, viewpoints_dir, clash_id):
        """Write a 1×1 PNG to viewpoints_dir/clash_id.png. Real PNG bytes
        so base64-encoding doesn't fail."""
        os.makedirs(viewpoints_dir, exist_ok=True)
        path = os.path.join(viewpoints_dir, '{}.png'.format(clash_id))
        # 1×1 transparent PNG as a known-good byte sequence.
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
            b'\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
            b'\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?'
            b'\x03\x00\x06\x00\x02\xfe\x88\x82\x82\x82'
            b'\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with open(path, 'wb') as f:
            f.write(png_bytes)
        return path

    def test_thumbnail_inlined_when_present(self):
        vpdir = os.path.join(self._tmp, 'viewpoints')
        self._make_fake_png(vpdir, 'clash-1')
        html_mod.build_html(
            [_clash(1, clash_id='clash-1')], self._out,
            viewpoints_dir=vpdir)
        body = self._read()
        # Should contain a data URI for the PNG.
        self.assertIn("data:image/png;base64,", body)

    def test_no_thumbnail_when_directory_missing(self):
        # No viewpoints dir → no img tag.
        html_mod.build_html([_clash(1, clash_id='clash-1')], self._out)
        body = self._read()
        self.assertNotIn("data:image/png", body)

    def test_thumbnail_skipped_when_include_thumbnails_false(self):
        vpdir = os.path.join(self._tmp, 'viewpoints')
        self._make_fake_png(vpdir, 'clash-1')
        html_mod.build_html(
            [_clash(1, clash_id='clash-1')], self._out,
            viewpoints_dir=vpdir,
            include_thumbnails=False)
        body = self._read()
        self.assertNotIn("data:image/png", body)

    def test_clash_without_image_renders_without_img(self):
        # Other clashes have images, this one doesn't.
        vpdir = os.path.join(self._tmp, 'viewpoints')
        self._make_fake_png(vpdir, 'clash-1')
        # clash-2 doesn't have a thumbnail
        clashes = [
            _clash(1, clash_id='clash-1'),
            _clash(2, clash_id='clash-2'),
        ]
        n = html_mod.build_html(clashes, self._out, viewpoints_dir=vpdir)
        self.assertEqual(n, 2)
        body = self._read()
        # Only one data: URI total (for clash-1).
        self.assertEqual(body.count("data:image/png;base64,"), 1)


# ---------------------------------------------------------------------------
# Skips garbage entries
# ---------------------------------------------------------------------------

class GarbageInputTests(_TempOutMixin, unittest.TestCase):

    def test_skips_non_dict_entries(self):
        # Pathological inputs from a corrupt clashes.json shouldn't crash.
        clashes = [_clash(1), "not a dict", None, _clash(2)]
        n = html_mod.build_html(clashes, self._out)
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
