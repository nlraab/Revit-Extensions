# -*- coding: utf-8 -*-
"""Structural test for lib/clash_share/bundle: the share-to-browser packer.

The packer string-surgeries the 3D Viewer's viewer3.html -- it depends on
exactly one <script type="importmap"> and exactly one <script type="module">
in the page, and it inlines the whole three.js module graph off disk. The UI
rebuild keeps editing viewer3.html, so this test locks the invariants the
packer needs, catching a break in CI instead of as a silently half-working
share file in a browser:

  - the real page has exactly one importmap block and one inline module script;
  - build_share_html against the real web/ dir + a tiny glb produces a
    self-contained file with the flattened (data:-URL) import map and the
    __DBHMS_MODEL_B64 / __DBHMS_BUNDLE globals the standalone path reads.
"""
import os
import sys
import shutil
import tempfile
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_share import bundle

_WEB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "dbHMS Tools.tab",
    "Clash Detection.panel", "3D Viewer.pushbutton", "web"))
_PAGE = os.path.join(_WEB, "viewer3.html")


class ShareBundleInvariantsTests(unittest.TestCase):
    def _page(self):
        with open(_PAGE, "rb") as f:
            return f.read().decode("utf-8")

    def test_exactly_one_importmap_and_one_module_script(self):
        html = self._page()
        self.assertEqual(html.count('type="importmap"'), 1,
                         "the packer rewrites exactly one importmap block")
        self.assertEqual(html.count('<script type="module">'), 1,
                         "the packer flattens exactly one inline module; a second "
                         "would be left un-inlined and the share file would break")

    def test_build_produces_self_contained_file(self):
        d = tempfile.mkdtemp(prefix="dbhms_share_test_")
        try:
            glb = os.path.join(d, "m.glb")
            with open(glb, "wb") as f:
                f.write(b"glTF\x02\x00\x00\x00")   # a few bytes stand in for the model
            out = os.path.join(d, "share.html")
            rows = [{"label": "#1 A x B", "point": [1.0, 2.0, 3.0],
                     "trade": "Plumbing", "status": "Open", "kind": "hard",
                     "haystack": "a x b"}]
            vps = [{"name": "Lobby", "pos": [0, 1, 2], "yaw": 0.1, "pitch": -0.2}]
            n = bundle.build_share_html(_WEB, glb, rows, vps, "Test Model",
                                        "2026-07-11 12:00", out)
            self.assertGreater(n, 0)
            self.assertTrue(os.path.isfile(out))
            with open(out, "rb") as f:
                txt = f.read().decode("utf-8")
            # standalone-mode globals present
            self.assertIn("window.__DBHMS_MODEL_B64", txt)
            self.assertIn("window.__DBHMS_BUNDLE", txt)
            # engine module graph was flattened to inline data: URLs (no fetches)
            self.assertIn("data:text/javascript;base64,", txt)
            # the caller-provided data rode along
            self.assertIn("Test Model", txt)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
