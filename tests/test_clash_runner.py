# -*- coding: utf-8 -*-
"""Pure-logic tests for the runner's zero-row-alarm diagnostics (v2 plan
5.7). run_test itself needs Revit; only the requested-role parsing is pure
and testable in CPython."""
import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_detect import runner


class RequestedRolesTests(unittest.TestCase):
    def test_host_only_has_no_roles(self):
        t = {'set_a': {'source': 'host'}, 'set_b': {'source': 'host'}}
        self.assertEqual(runner._requested_roles(t), [])

    def test_link_roles_are_parsed_from_both_sides(self):
        t = {'set_a': {'source': 'host'},
             'set_b': {'source': ['host', 'link:Architectural', 'link:Structural']}}
        self.assertEqual(runner._requested_roles(t),
                         ['Architectural', 'Structural'])

    def test_roles_are_deduped_and_ordered(self):
        t = {'set_a': {'source': 'link:Architectural'},
             'set_b': {'source': ['link:Architectural', 'link:Structural']}}
        self.assertEqual(runner._requested_roles(t),
                         ['Architectural', 'Structural'])

    def test_missing_sources_are_safe(self):
        self.assertEqual(runner._requested_roles({}), [])
        self.assertEqual(runner._requested_roles({'set_a': {}}), [])


if __name__ == '__main__':
    unittest.main()
