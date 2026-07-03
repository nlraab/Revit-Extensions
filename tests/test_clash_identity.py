# -*- coding: utf-8 -*-
"""Unit tests for lib/clash_identity: the shared federation-key formula that the
exporter (clash_export) and the detector (clash_detect) BOTH build from. If this
formatting ever drifts, linked-element clashes silently map to the wrong (or no)
geometry, so these tests pin the exact string shape."""
import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import clash_identity


class ClashIdentityTests(unittest.TestCase):
    def test_host_fed_key_is_bare_uid(self):
        self.assertEqual(clash_identity.fed_key("UID-123"), "UID-123")
        self.assertEqual(clash_identity.fed_key("UID-123", None), "UID-123")

    def test_missing_uid_returns_none(self):
        self.assertIsNone(clash_identity.fed_key(None))
        self.assertIsNone(clash_identity.fed_key(""))

    def test_link_ns_format_is_3dp_comma_no_space(self):
        self.assertEqual(
            clash_identity.link_ns_from_origin("Arch.rvt", 1, 2, 3),
            "Arch.rvt@1.000,2.000,3.000")
        self.assertEqual(
            clash_identity.link_ns_from_origin("L", -0.0, 12.5, -4.0),
            "L@-0.000,12.500,-4.000")

    def test_link_ns_empty_name_falls_back(self):
        self.assertEqual(
            clash_identity.link_ns_from_origin("", 0, 0, 0),
            "link@0.000,0.000,0.000")

    def test_linked_fed_key_composes_ns_pipe_uid(self):
        ns = clash_identity.link_ns_from_origin("Arch.rvt", 0, 192, 0)
        self.assertEqual(
            clash_identity.fed_key("b383-0001", ns),
            "Arch.rvt@0.000,192.000,0.000|b383-0001")


if __name__ == "__main__":
    unittest.main()
