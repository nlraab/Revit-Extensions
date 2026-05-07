"""Tests for the pure-data piece of lib/clash_view/walkthrough_render.py.

Only `render_filename` is tested here — `render_stop` requires Revit's
ExportImage and is exercised live, not in CPython tests.
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_view import walkthrough_render as wr  # noqa: E402


class RenderFilenameTests(unittest.TestCase):

    def test_uses_seq_and_timestamp(self):
        when = datetime(2026, 5, 6, 14, 30, 15)
        name = wr.render_filename({"seq": 42}, when=when)
        self.assertEqual(name, "clash-42-20260506-143015.png")

    def test_missing_seq_uses_x(self):
        when = datetime(2026, 5, 6, 14, 30, 15)
        name = wr.render_filename({}, when=when)
        self.assertEqual(name, "clash-x-20260506-143015.png")

    def test_none_clash_uses_x(self):
        when = datetime(2026, 5, 6, 14, 30, 15)
        name = wr.render_filename(None, when=when)
        self.assertEqual(name, "clash-x-20260506-143015.png")

    def test_default_when_is_now(self):
        # No when= means use utcnow(); just verify the shape.
        name = wr.render_filename({"seq": 1})
        self.assertTrue(name.startswith("clash-1-"))
        self.assertTrue(name.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
