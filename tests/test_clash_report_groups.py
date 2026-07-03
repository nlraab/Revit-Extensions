# -*- coding: utf-8 -*-
"""Tests for the Layer C reporting periphery (Phase 4): BCF group topics
and the pre-meeting agenda digest.

Both builders are pure-Python readers over the clashes.json shape (clashes
+ groups). These pin the topic-per-group mapping and the agenda assembly
so a schema drift or a wrong rollup read fails here, not in a consultant's
BCF viewer or a Monday agenda email.
"""
import io
import os
import sys
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from clash_report import bcf, digest


def clash(cid, seq, a, b, band='Major', score=55, status='Open',
          reason='r', suppressed=False, assignee='Mechanical'):
    return {
        'id': cid, 'seq': seq, 'test_id': 't1', 'kind': 'hard',
        'status': status, 'assignee': assignee,
        'ref_a': {'name': a, 'category': 'Ducts', 'element_id': 1, 'source': 'host'},
        'ref_b': {'name': b, 'category': 'Walls', 'element_id': 2,
                  'source': 'link:Architectural'},
        'midpoint': [0, 0, 0], 'comments': [], 'history': [],
        'first_seen_run': '2026-07-01T10:00:00Z',
        'importance': {'band': band, 'score': score, 'reason': reason,
                       'rule': 'M1', 'suppressed': suppressed, 'flags': []},
    }


def group(gid, seq, title, member_ids, band='Major', score=55,
          status='Open', assignee='Mechanical', axis='cluster',
          rep=None, n_open=None):
    return {
        'id': gid, 'seq': seq, 'axis': axis, 'title': title,
        'status': status, 'assignee': assignee,
        'member_ids': list(member_ids), 'rep_clash_id': rep or member_ids[0],
        'comments': [], 'history': [
            {'author': 'system', 'action': 'created', 'at': '2026-07-01T10:00:00Z'}],
        'rollup': {'band': band, 'score': score, 'reason': 'controlling reason',
                   'n_open': n_open if n_open is not None else len(member_ids),
                   'n_total': len(member_ids), 'n_new_run': 0},
    }


class BcfGroupTopicTests(unittest.TestCase):
    def _world(self):
        clashes = [clash('c1', 1, 'Duct A', 'Wall 1', band='Critical', score=90),
                   clash('c2', 2, 'Duct A', 'Wall 2'),
                   clash('c3', 3, 'Pipe X', 'Wall 3', band='Minor', score=20)]
        groups = [
            group('g1', 1, 'Congested rack - L2', ['c1', 'c2'],
                  band='Critical', score=90, rep='c1'),
            group('g2', 2, 'Sleeve set', ['c3'], band='Minor', score=20),
            {'id': 'gtomb', 'status': 'MergedInto', 'member_ids': [],
             'title': 'dead', 'rollup': {}},
        ]
        return groups, clashes

    def test_one_topic_per_group_tombstones_skipped(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'g.bcfzip')
        n = bcf.build_group_bcf_zip({}, groups, clashes, None, out)
        self.assertEqual(n, 2)                       # tombstone excluded
        with zipfile.ZipFile(out) as zf:
            markups = [x for x in zf.namelist() if x.endswith('markup.bcf')]
            self.assertEqual(len(markups), 2)

    def test_topic_carries_title_priority_assignee_roster(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'g.bcfzip')
        bcf.build_group_bcf_zip({}, groups, clashes, None, out)
        with zipfile.ZipFile(out) as zf:
            markup = None
            for name in zf.namelist():
                if name.endswith('markup.bcf'):
                    x = ET.fromstring(zf.read(name))
                    t = x.find('Topic')
                    if t.find('Title').text == 'Congested rack - L2':
                        markup = t
            self.assertIsNotNone(markup)
            self.assertEqual(markup.get('TopicStatus'), 'Open')
            self.assertEqual(markup.find('Priority').text, 'High')  # Critical
            self.assertEqual(markup.find('AssignedTo').text, 'Mechanical')
            self.assertEqual(markup.find('Index').text, '1')
            desc = markup.find('Description').text
            self.assertIn('controlling reason', desc)
            self.assertIn('#1', desc)                # member seq listed
            self.assertIn('#2', desc)
            labels = [e.text for e in markup.findall('Labels')]
            self.assertIn('Critical', labels)
            self.assertIn('Congestion', labels)      # cluster axis
            # BCF 2.1 markup.xsd fixes the child order: Title, Priority,
            # Index, Labels, CreationDate... A strict validator (Solibri)
            # rejects out-of-order children.
            order = [c.tag for c in list(markup)]
            self.assertLess(order.index('Priority'), order.index('Index'))
            self.assertLess(order.index('Title'), order.index('Priority'))
            self.assertLess(order.index('Index'), order.index('Labels'))
            self.assertLess(order.index('Labels'), order.index('CreationDate'))

    def test_dissolved_empty_group_exports_no_ghost_topic(self):
        groups, clashes = self._world()
        groups.append({'id': 'gdead', 'status': 'Resolved', 'member_ids': [],
                       'title': 'ungrouped', 'rollup': {}})
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'g.bcfzip')
        n = bcf.build_group_bcf_zip({}, groups, clashes, None, out)
        self.assertEqual(n, 2)                        # g1 + g2 only, no ghost

    def test_group_predicate_filters(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'g.bcfzip')
        n = bcf.build_group_bcf_zip(
            {}, groups, clashes, None, out,
            group_predicate=lambda g: (g.get('rollup') or {}).get('band') == 'Critical')
        self.assertEqual(n, 1)

    def test_snapshot_embedded_when_png_on_disk(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        vp = os.path.join(tmp, 'vp')
        os.makedirs(vp)
        # a 1x1 PNG for the rep clash of g1
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00'
               b'\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx'
               b'\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')
        with open(os.path.join(vp, 'c1.png'), 'wb') as f:
            f.write(png)
        out = os.path.join(tmp, 'g.bcfzip')
        bcf.build_group_bcf_zip({}, groups, clashes, vp, out)
        with zipfile.ZipFile(out) as zf:
            snaps = [x for x in zf.namelist() if x.endswith('snapshot.png')]
            self.assertEqual(len(snaps), 1)          # only g1 has a rep png
            bcfvs = [x for x in zf.namelist() if x.endswith('viewpoint.bcfv')]
            self.assertEqual(len(bcfvs), 1)          # minimal viewpoint written

    def test_long_roster_is_capped(self):
        clashes = [clash('c{}'.format(i), i, 'D{}'.format(i), 'W', score=i)
                   for i in range(60)]
        groups = [group('gbig', 1, 'Big rack', [c['id'] for c in clashes],
                        n_open=60)]
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'g.bcfzip')
        bcf.build_group_bcf_zip({}, groups, clashes, None, out)
        with zipfile.ZipFile(out) as zf:
            for name in zf.namelist():
                if name.endswith('markup.bcf'):
                    desc = ET.fromstring(zf.read(name)).find('Topic/Description').text
                    self.assertIn('and 20 more', desc)   # 40 shown + tail
                    self.assertIn('60 of 60', desc)


class DigestTests(unittest.TestCase):
    def _world(self):
        clashes = [
            clash('c1', 1, 'Duct A', 'Wall 1', band='Critical', score=90),
            clash('c2', 2, 'Duct A', 'Wall 2', band='Major', score=55),
            clash('c3', 3, 'Pipe X', 'Wall 3', band='Minor', score=20),
            clash('s1', 4, 'Sanitary main', 'W36 beam', band='Critical',
                  score=95, assignee='Plumbing'),          # ungrouped single
            clash('m1', 5, 'Small pipe', 'Wall', band='Minor', score=12),  # ungrouped minor
        ]
        groups = [
            group('g1', 1, 'Congested rack - L2', ['c1', 'c2'],
                  band='Critical', score=90, rep='c1'),
            group('g2', 2, 'Sleeve set', ['c3'], band='Minor', score=20),
        ]
        return groups, clashes

    def test_agenda_is_critical_major_only_minor_group_is_batch(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        n = digest.build_digest_html(groups, clashes, out,
                                     project_name='NIU', now_iso='2026-07-03T00:00:00Z')
        # agenda = g1 (Critical group) + s1 (Critical single) = 2.
        # g2 (Minor group) and m1 (ungrouped Minor) are batch, off-agenda.
        self.assertEqual(n, 2)
        html = io.open(out, encoding='utf-8').read()
        self.assertIn('Sanitary main', html)         # critical singleton on agenda
        self.assertNotIn('Small pipe', html)         # minor singleton excluded
        self.assertIn('Congested rack - L2', html)
        self.assertNotIn('Sleeve set', html)         # minor group off the agenda
        self.assertIn('Batch items (Minor)', html)   # but counted as batch
        self.assertIn('controlling reason', html)     # group rollup reason

    def test_agenda_sorted_critical_first_then_score(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        digest.build_digest_html(groups, clashes, out,
                                 now_iso='2026-07-03T00:00:00Z')
        html = io.open(out, encoding='utf-8').read()
        # s1 (Critical 95) before g1 (Critical 90); Sleeve set (Minor) absent
        self.assertLess(html.index('Sanitary main'), html.index('Congested rack'))

    def test_owner_breakdown_present(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        digest.build_digest_html(groups, clashes, out,
                                 now_iso='2026-07-03T00:00:00Z')
        html = io.open(out, encoding='utf-8').read()
        self.assertIn('By owner', html)
        self.assertIn('Plumbing', html)              # from the sanitary single

    def test_resolved_group_is_off_the_agenda(self):
        groups, clashes = self._world()
        groups[0]['status'] = 'Resolved'
        for c in clashes:
            if c['id'] in ('c1', 'c2'):
                c['status'] = 'Resolved'
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        n = digest.build_digest_html(groups, clashes, out,
                                     now_iso='2026-07-03T00:00:00Z')
        html = io.open(out, encoding='utf-8').read()
        self.assertNotIn('Congested rack', html)     # resolved group gone
        self.assertEqual(n, 1)                        # only s1 (Critical single)

    def test_html_escapes_titles(self):
        groups, clashes = self._world()
        groups[0]['title'] = 'Duct <Supply> vs "Wall"'
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        digest.build_digest_html(groups, clashes, out,
                                 now_iso='2026-07-03T00:00:00Z')
        html = io.open(out, encoding='utf-8').read()
        self.assertIn('&lt;Supply&gt;', html)
        self.assertNotIn('<Supply>', html)

    def test_non_ascii_names_do_not_crash_esc(self):
        # IronPython 2.7: str(u'...') with non-ASCII raises. _esc must stay
        # unicode. Simulate a Revit type name with en-dash/degree/accent.
        groups, clashes = self._world()
        groups[0]['title'] = u'Duct – VAV \xd8300'   # en-dash + diameter sign
        clashes[3]['assignee'] = u'M\xe9canique'          # accented owner
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        digest.build_digest_html(groups, clashes, out,
                                 now_iso='2026-07-03T00:00:00Z')
        html = io.open(out, encoding='utf-8').read()
        self.assertIn(u'–', html)                    # preserved, not crashed
        self.assertIn(u'M\xe9canique', html)

    def test_filter_predicate_narrows_the_agenda(self):
        groups, clashes = self._world()
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'digest.html')
        # Only accept the Critical group; the Critical singleton fails.
        n = digest.build_digest_html(
            groups, clashes, out, now_iso='2026-07-03T00:00:00Z',
            group_predicate=lambda g: (g.get('rollup') or {}).get('band') == 'Critical',
            clash_predicate=lambda c: False)
        self.assertEqual(n, 1)                            # only g1


if __name__ == '__main__':
    unittest.main()
