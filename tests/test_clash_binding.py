# -*- coding: utf-8 -*-
"""Unit tests for the per-machine binding registry in lib/clash_core/binding.

The Extensible Storage half is Revit-only (parse-checked elsewhere); these
tests pin the resilience layer: the registry that keeps a project's folder
across close-without-save and pyRevit reloads. Under CPython the ES read
path fails cleanly (no Revit API), which conveniently exercises the exact
fallback we care about."""
import os
import sys
import tempfile
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_core import binding


class FakeDoc(object):
    def __init__(self, path):
        self.PathName = path


class BindingRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = binding._registry_path
        binding._registry_path = lambda: os.path.join(self._tmp, "bindings.json")

    def tearDown(self):
        binding._registry_path = self._orig

    def test_remember_and_recall(self):
        doc = FakeDoc(r"T:\Projects\NIU\MEP_21112.rvt")
        binding.remember_local(doc, r"P:\NIU\Clash Data")
        self.assertEqual(binding._local_folder(doc), r"P:\NIU\Clash Data")

    def test_recall_is_path_normalized(self):
        # Same central model reached via a differently-cased path must hit
        # the same registry entry (hash_path normalizes).
        binding.remember_local(FakeDoc(r"T:\Projects\NIU\MODEL.rvt"), r"P:\X")
        self.assertEqual(
            binding._local_folder(FakeDoc(r"t:/projects/niu/model.rvt")),
            r"P:\X")

    def test_recall_survives_a_changed_primary_key(self):
        # A mapping written when PathName was the identity must still resolve
        # after code starts preferring a different identity path - the exact
        # orphaning that broke it before. Simulate by hand-writing the
        # registry under only the PathName key, then reading via a doc that
        # also exposes an (unwritten) cloud-style key.
        import io
        import json
        from clash_core import project
        doc = FakeDoc(r"C:\cache\niu.rvt")
        legacy_key = project.hash_path(r"C:\cache\niu.rvt")
        with io.open(binding._registry_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({legacy_key: {"folder": r"P:\NIU"}}))
        self.assertEqual(binding._local_folder(doc), r"P:\NIU")

    def test_folder_for_falls_back_to_registry_when_model_unreadable(self):
        # Under CPython the ES path always fails -> exactly the
        # close-without-save / reload scenario.
        doc = FakeDoc(r"T:\Projects\NIU\MEP.rvt")
        self.assertIsNone(binding.folder_for(doc))
        binding.remember_local(doc, r"P:\NIU\Clash Data")
        self.assertEqual(binding.folder_for(doc), r"P:\NIU\Clash Data")

    def test_needs_heal_reports_registry_only_bindings(self):
        doc = FakeDoc(r"T:\Projects\NIU\MEP.rvt")
        self.assertIsNone(binding.needs_heal(doc))
        binding.remember_local(doc, r"P:\NIU\Clash Data")
        self.assertEqual(binding.needs_heal(doc), r"P:\NIU\Clash Data")

    def test_unsaved_doc_never_registers(self):
        doc = FakeDoc("")            # untitled model: no stable key
        binding.remember_local(doc, r"P:\X")
        self.assertIsNone(binding._local_folder(doc))
        self.assertIsNone(binding.folder_for(doc))

    def test_registry_survives_reread_and_none_doc_is_safe(self):
        doc = FakeDoc(r"T:\A.rvt")
        binding.remember_local(doc, r"P:\A")
        binding.remember_local(FakeDoc(r"T:\B.rvt"), r"P:\B")
        self.assertEqual(binding._local_folder(doc), r"P:\A")
        self.assertIsNone(binding.folder_for(None))
        self.assertIsNone(binding.needs_heal(None))

    def test_corrupt_registry_degrades_to_empty(self):
        with open(binding._registry_path(), "w") as f:
            f.write("{not json")
        doc = FakeDoc(r"T:\A.rvt")
        self.assertIsNone(binding._local_folder(doc))
        binding.remember_local(doc, r"P:\A")   # write-through repairs it
        self.assertEqual(binding._local_folder(doc), r"P:\A")


if __name__ == '__main__':
    unittest.main()
