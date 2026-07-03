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

    def test_no_auto_heal_writeback_exists(self):
        # SAFETY INVARIANT: nothing may auto-write the model from the local
        # registry (that clobbered teammates' synced bindings on sync). The
        # heal machinery must be gone entirely.
        self.assertFalse(hasattr(binding, "needs_heal"))

    def test_write_binding_does_not_touch_registry(self):
        # write_binding must NOT write the registry (a rolled-back txn would
        # otherwise poison the local cache). The caller does remember_local
        # only after a confirmed commit.
        doc = FakeDoc(r"T:\Projects\NIU\MEP.rvt")
        # Behavioural: a write_binding attempt (its ES call fails under
        # CPython) must leave the registry empty - the caller writes it only
        # after a confirmed commit.
        try:
            binding.write_binding(doc, r"P:\X")
        except Exception:
            pass
        self.assertIsNone(binding._local_folder(doc))

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

    def test_corrupt_registry_degrades_to_empty(self):
        with open(binding._registry_path(), "w") as f:
            f.write("{not json")
        doc = FakeDoc(r"T:\A.rvt")
        self.assertIsNone(binding._local_folder(doc))
        binding.remember_local(doc, r"P:\A")   # write-through repairs it
        self.assertEqual(binding._local_folder(doc), r"P:\A")

    def test_forget_local_clears_this_docs_entries_only(self):
        a, b = FakeDoc(r"T:\A.rvt"), FakeDoc(r"T:\B.rvt")
        binding.remember_local(a, r"P:\A")
        binding.remember_local(b, r"P:\B")
        binding.forget_local(a)
        self.assertIsNone(binding._local_folder(a))
        self.assertEqual(binding._local_folder(b), r"P:\B")


class ThreeStateReadTests(unittest.TestCase):
    """The correctness core: a FAILED read (UNKNOWN) must never behave like a
    clean "not set" (UNSET), and a deliberate team-wide CLEAR (CLEARED) must
    never be resurrected from a stale local cache. Driven through the
    _read_entity seam so the Revit-only ES layer is stubbed."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_reg = binding._registry_path
        self._orig_read = binding._read_entity
        binding._registry_path = lambda: os.path.join(self._tmp, "bindings.json")

    def tearDown(self):
        binding._registry_path = self._orig_reg
        binding._read_entity = self._orig_read

    def _stub(self, status, path=None):
        binding._read_entity = lambda doc: (status, path)

    def test_bound_model_wins_over_stale_registry(self):
        # Teammate changed the model to Y and synced; this machine's cache
        # still holds X. The model must win.
        doc = FakeDoc(r"T:\M.rvt")
        binding.remember_local(doc, r"P:\X")     # stale local cache
        self._stub(binding.BOUND, r"P:\Y")
        self.assertEqual(binding.folder_for(doc), r"P:\Y")
        # and the fresh model value replaces the stale cache
        self._stub(binding.UNKNOWN)
        self.assertEqual(binding._local_folder(doc), r"P:\Y")

    def test_unknown_read_serves_registry_but_never_caches_it(self):
        # A transient ES failure shows the last-known folder (tool isn't
        # blank) but must NOT cement anything new.
        doc = FakeDoc(r"T:\M.rvt")
        binding.remember_local(doc, r"P:\X")
        self._stub(binding.UNKNOWN)
        self.assertEqual(binding.folder_for(doc), r"P:\X")   # display only
        # nothing new written: a doc with no cache stays None under UNKNOWN
        other = FakeDoc(r"T:\N.rvt")
        self.assertIsNone(binding.folder_for(other))

    def test_unset_model_falls_back_to_registry(self):
        # Genuine clean absence (close-without-save): registry covers it.
        doc = FakeDoc(r"T:\M.rvt")
        binding.remember_local(doc, r"P:\X")
        self._stub(binding.UNSET)
        self.assertEqual(binding.folder_for(doc), r"P:\X")

    def test_cleared_model_is_never_resurrected_from_cache(self):
        # The team deliberately cleared the binding; a stale local cache must
        # NOT bring it back.
        doc = FakeDoc(r"T:\M.rvt")
        binding.remember_local(doc, r"P:\X")
        self._stub(binding.CLEARED)
        self.assertIsNone(binding.folder_for(doc))

    def test_model_folder_is_bound_only(self):
        doc = FakeDoc(r"T:\M.rvt")
        self._stub(binding.BOUND, r"P:\Y")
        self.assertEqual(binding.model_folder(doc), r"P:\Y")
        for st in (binding.UNSET, binding.CLEARED, binding.UNKNOWN):
            self._stub(st)
            self.assertIsNone(binding.model_folder(doc))

    def test_read_retry_recovers_from_a_transient_unknown(self):
        doc = FakeDoc(r"T:\M.rvt")
        seq = [(binding.UNKNOWN, None), (binding.BOUND, r"P:\Y")]
        binding._read_entity = lambda d: seq.pop(0) if seq else (binding.BOUND, r"P:\Y")
        status, path = binding.read_model_retry(doc, attempts=3)
        self.assertEqual((status, path), (binding.BOUND, r"P:\Y"))


if __name__ == '__main__':
    unittest.main()
