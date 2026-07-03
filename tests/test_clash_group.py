# -*- coding: utf-8 -*-
"""Unit tests for lib/clash_group: Layer C sticky-issue grouping.

The S1-S8 fixtures mirror CLASH_GROUPING_DESIGN.md section 4; if a change
moves one of them to a different outcome, that is a design change and the
doc should move with it. The invariants the design hangs on get named
tests: sticky rosters (nothing a human touched is ever re-derived),
formation blindness to grouped clashes, the border no-bridge rule (false
merges structurally impossible), and the frozen anchor key.
"""
import copy
import os
import sys
import unittest

_LIB = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..",
    "src", "extensions", "dbHMS Extensions.extension", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import clash_group


R1 = '2026-07-01T10:00:00Z'
R1_LATER = '2026-07-01T10:00:01Z'
# Midnight-anchored so a real _now_iso() stamped during a test run always
# compares >= R2 (history entries minted by the lib use real time).
R2 = '2026-07-02T00:00:01Z'
R2_LATER = '2026-07-02T00:00:02Z'


def ref(source='host', element_id=1, unique_id=None, category='Pipes',
        name=None, level='L1', link_doc_title=None):
    return {'source': source, 'element_id': element_id,
            'unique_id': unique_id, 'category': category,
            'name': name or category, 'level': level,
            'link_doc_title': link_doc_title}


_SEQ = [0]


def clash(cid, a, b, midpoint, status='Open', test_id='t1',
          first_seen=R2, last_seen=R2, band='Major', score=55,
          cluster_n=0, suppressed=False, flags=None, history=None):
    _SEQ[0] += 1
    return {
        'id': cid, 'seq': _SEQ[0], 'test_id': test_id, 'kind': 'hard',
        'status': status, 'assignee': 'Mechanical',
        'ref_a': a, 'ref_b': b, 'midpoint': midpoint,
        'first_seen_run': first_seen, 'last_seen_run': last_seen,
        'comments': [], 'history': history or [],
        'importance': {
            'band': band, 'score': score, 'reason': 'r', 'rule': 'M1',
            'suppressed': suppressed, 'flags': flags or [],
            'features': {'cluster_n': cluster_n},
        },
    }


def duct_star(n=12, uid='DUCT1', x0=0.0, dx=5.0, cluster_n=2, **kw):
    """n clashes: one duct element vs n distinct beams along a corridor."""
    duct = ref(element_id=1, unique_id=uid, category='Ducts', name='DD-03',
               level='L3')
    rows = []
    for i in range(n):
        beam = ref(source='link:Structural', element_id=100 + i,
                   unique_id='B{0}'.format(i), category='Structural Framing',
                   level='L3')
        rows.append(clash('s1-{0}'.format(i), duct, beam,
                          [x0 + dx * i, 0.0, 0.0], cluster_n=cluster_n, **kw))
    return rows


def rack(n=35, cx=0.0, cluster_n=25, prefix='rk', **kw):
    """n clashes packed in a tight ball (a congested rack)."""
    rows = []
    for i in range(n):
        a = ref(element_id=200 + (i % 9), unique_id='E{0}'.format(i % 9),
                category='Conduits', level='L2')
        b = ref(element_id=300 + i, unique_id='F{0}'.format(i),
                category='Ducts', level='L2')
        mid = [cx + 0.11 * (i % 7), 0.13 * (i % 5), 0.07 * (i % 3)]
        rows.append(clash('{0}-{1}'.format(prefix, i), a, b, mid,
                          cluster_n=cluster_n, **kw))
    return rows


def run(clashes, groups=None, run_iso=R2, config=None):
    return clash_group.regroup_all(clashes, groups or [],
                                   run_iso=run_iso, config=config)


def members_of(groups, gid):
    for g in groups:
        if g['id'] == gid:
            return g['member_ids']
    return None


# ---------------------------------------------------------------------------
# Formation (S1, S2, S3, S6 + bounds)
# ---------------------------------------------------------------------------

class FormationTests(unittest.TestCase):
    def test_s1_element_star_one_group_for_one_bad_duct(self):
        rows = duct_star(12)
        groups, summary = run(rows)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g['axis'], 'element')
        self.assertEqual(g['anchor']['unique_id'], 'DUCT1')
        self.assertEqual(len(g['member_ids']), 12)
        self.assertIn('DD-03', g['title'])
        self.assertTrue(all(c['group_id'] == g['id'] for c in rows))

    def test_s3_wall_anchors_penetration_set(self):
        wall = ref(source='link:Architectural', element_id=50,
                   unique_id='W1', category='Walls', name='Rated wall',
                   level='L2')
        rows = []
        for i in range(14):
            pipe = ref(element_id=400 + i, unique_id='P{0}'.format(i),
                       category='Pipes', level='L2')
            rows.append(clash('s3-{0}'.format(i), pipe, wall,
                              [3.0 * i, 0.0, 0.0], band='Minor', score=26,
                              flags=['penetration_candidate'], cluster_n=1))
        groups, _ = run(rows)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        # participation: wall 14 vs each pipe 1 -> the wall anchors
        self.assertEqual(g['anchor']['unique_id'], 'W1')
        self.assertTrue(g['title'].startswith('Penetrations'),
                        g['title'])

    def test_s2_rack_forms_one_cluster_group(self):
        rows = rack(35)
        groups, _ = run(rows)
        clusters = [g for g in groups if g['axis'] == 'cluster']
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]['member_ids']), 35)
        self.assertIn('Congested', clusters[0]['title'])
        r = clusters[0]['rollup']
        self.assertTrue(r['congested'])          # cluster_n 25 >= 20
        self.assertTrue(r['diameter_ft'] > 0)

    def test_member_cap_cuts_a_big_rack(self):
        rows = rack(30)
        groups, _ = run(rows, config={'max_members': 20})
        clusters = [g for g in groups if g['axis'] == 'cluster']
        self.assertEqual(len(clusters), 2)
        sizes = sorted(len(g['member_ids']) for g in clusters)
        self.assertEqual(sum(sizes), 30)

    def test_s6_two_distinct_problems_4ft_apart_stay_apart(self):
        a1 = ref(element_id=1, unique_id='SAN1', category='Pipes')
        b1 = ref(source='link:Structural', element_id=2, unique_id='BM1',
                 category='Structural Framing')
        a2 = ref(element_id=3, unique_id='TRAY1', category='Cable Trays')
        b2 = ref(element_id=4, unique_id='DUCT9', category='Ducts')
        rows = [
            clash('x1', a1, b1, [0.0, 0.0, 0.0], cluster_n=2),
            clash('x2', a2, b2, [4.0, 0.0, 0.0], cluster_n=2),
        ]
        groups, summary = run(rows)
        self.assertEqual(len(groups), 0)
        self.assertEqual(summary['ungrouped'], 2)

    def test_element_star_needs_three_members(self):
        rows = duct_star(2)
        groups, _ = run(rows)
        self.assertEqual(len(groups), 0)

    def test_long_main_splits_into_span_segments(self):
        rows = duct_star(12, dx=18.0)          # spans ~200 ft
        groups, _ = run(rows)
        self.assertTrue(len(groups) >= 2)
        for g in groups:
            self.assertEqual(g['anchor']['unique_id'], 'DUCT1')

    def test_suppressed_clashes_are_never_assigned(self):
        rows = rack(30)
        for c in rows[:5]:
            c['importance']['suppressed'] = True
        groups, _ = run(rows)
        grouped = set()
        for g in groups:
            grouped.update(g['member_ids'])
        for c in rows[:5]:
            self.assertNotIn(c['id'], grouped)

    def test_border_never_bridges_two_components(self):
        # The bridge sits within eps (6 ft) of BOTH components, so the
        # no-bridge rule must exclude it at formation (each cluster's
        # created history says 12 members); P6 adjacency may then join it
        # to the single nearest group, never both.
        core_a = rack(12, cx=0.0, prefix='a')
        core_b = rack(12, cx=10.0, prefix='b')
        bridge = clash('bridge',
                       ref(element_id=900, unique_id='BR', category='Pipes'),
                       ref(element_id=901, unique_id='BR2', category='Ducts'),
                       [5.0, 0.0, 0.0], cluster_n=2)
        rows = core_a + core_b + [bridge]
        groups, _ = run(rows)
        clusters = [g for g in groups if g['axis'] == 'cluster']
        self.assertEqual(len(clusters), 2)
        for g in clusters:
            created = [h for h in g['history'] if h['action'] == 'created']
            self.assertEqual(created[0]['after'], '12 members')
            a_ids = [m for m in g['member_ids'] if m.startswith('a-')]
            b_ids = [m for m in g['member_ids'] if m.startswith('b-')]
            self.assertTrue(not a_ids or not b_ids,
                            'a component absorbed cores from both racks')
        in_both = [g for g in clusters if 'bridge' in g['member_ids']]
        self.assertTrue(len(in_both) <= 1)

    def test_border_chain_cannot_extend_a_components_reach(self):
        # b1 attaches to the 8 cores (5.5 ft away); b2 is > eps from every
        # CORE and must not chain through b1. Component = 9 < 10, so no
        # group forms - in either border order.
        def build(border_order):
            rows = []
            for i in range(8):
                a = ref(element_id=200 + i, unique_id='C{0}'.format(i),
                        category='Conduits')
                b = ref(element_id=500 + i, unique_id='D{0}'.format(i),
                        category='Ducts')
                rows.append(clash('core-{0}'.format(i), a, b,
                                  [0.5 * i, 0.0, 0.0], cluster_n=10))
            b1 = clash('b1', ref(element_id=901, unique_id='BB1'),
                       ref(element_id=902, unique_id='BB2',
                           category='Ducts'),
                       [9.0, 0.0, 0.0], cluster_n=2)
            b2 = clash('b2', ref(element_id=903, unique_id='BB3'),
                       ref(element_id=904, unique_id='BB4',
                           category='Ducts'),
                       [14.5, 0.0, 0.0], cluster_n=2)
            return rows + (border_order == 'b1-first' and [b1, b2] or [b2, b1])
        for order in ('b1-first', 'b2-first'):
            groups, _ = run(build(order))
            self.assertEqual(
                [g for g in groups if g['axis'] == 'cluster'], [],
                'border chaining formed a group ({0})'.format(order))

    def test_oversize_component_is_cut_deterministically(self):
        rows = []
        for i in range(30):                     # 40-ft chain, all cores
            a = ref(element_id=200 + i, unique_id='C{0}'.format(i),
                    category='Conduits')
            b = ref(element_id=500 + i, unique_id='D{0}'.format(i),
                    category='Ducts')
            rows.append(clash('ch-{0}'.format(i), a, b,
                              [i * 40.0 / 29, 0.0, 0.0], cluster_n=25))
        groups, _ = run(rows, config={'max_span_ft': 20.0})
        clusters = [g for g in groups if g['axis'] == 'cluster']
        self.assertEqual(len(clusters), 2)


# ---------------------------------------------------------------------------
# Stickiness + identity (S4, S5, S7, successor cascade, frozen anchor)
# ---------------------------------------------------------------------------

def make_group(gid, member_ids, axis='element', anchor=None, title='G',
               title_locked=False, status='Open', assignee=None,
               comments=None, created_at=R1):
    return {
        'id': gid, 'created_at': created_at, 'created_by': 'system',
        'axis': axis, 'anchor': anchor, 'title': title,
        'title_locked': title_locked, 'status': status,
        'assignee': assignee, 'priority': None,
        'member_ids': list(member_ids), 'suggested_ids': [],
        'needs_review': False,
        'lineage': {'split_from': None, 'merged_from': [],
                    'merged_into': None},
        'history': [], 'comments': comments or [],
        'rep_clash_id': member_ids[0] if member_ids else None,
        'rollup': {},
    }


class StickinessTests(unittest.TestCase):
    def _s4_world(self):
        """The named, assigned, commented duct group after the duct moved:
        3 members persist, 9 auto-resolved this run, 2 genuinely new
        clashes on the same duct appear far away."""
        rows = duct_star(12, first_seen=R1, last_seen=R2)
        for c in rows[3:]:                       # 9 auto-resolved this run
            c['status'] = 'Resolved'
            c['last_seen_run'] = R1
            c['history'] = [{'author': 'system', 'action': 'auto_resolved',
                             'at': R2_LATER}]
        duct = rows[0]['ref_a']
        new = []
        for i in range(2):
            beam = ref(source='link:Structural', element_id=800 + i,
                       unique_id='NB{0}'.format(i),
                       category='Structural Framing', level='L3')
            new.append(clash('new-{0}'.format(i), duct, beam,
                             [100.0 + 10 * i, 0.0, 0.0], cluster_n=1))
        anchor = {'source': 'host', 'unique_id': 'DUCT1', 'element_id': 1,
                  'name': 'DD-03', 'category': 'Ducts',
                  'link_doc_title': None}
        g = make_group('g-s4', [c['id'] for c in rows], anchor=anchor,
                       title='DD-03 vs L3 beams', title_locked=True,
                       status='Reviewed', assignee='Mechanical',
                       comments=[{'author': 'nathan', 'body': 'x'}] * 4)
        return rows + new, [g]

    def test_s4_named_group_survives_churn_and_absorbs_new(self):
        clashes, groups = self._s4_world()
        out, summary = run(clashes, groups)
        self.assertEqual(len(out), 1)
        g = out[0]
        self.assertEqual(g['id'], 'g-s4')
        self.assertEqual(g['title'], 'DD-03 vs L3 beams')     # locked
        self.assertEqual(g['assignee'], 'Mechanical')
        self.assertEqual(len(g['comments']), 4)
        self.assertEqual(len(g['member_ids']), 14)            # 12 kept + 2
        self.assertIn('new-0', g['member_ids'])
        self.assertIn('new-1', g['member_ids'])
        self.assertTrue(g['needs_review'])                    # beyond Open
        self.assertEqual(g['rollup']['n_open'], 5)
        self.assertEqual(g['rollup']['n_resolved_run'], 9)
        self.assertEqual(g['rollup']['n_new_run'], 2)

    def test_s4_original_groups_list_is_never_mutated(self):
        clashes, groups = self._s4_world()
        snapshot = copy.deepcopy(groups)
        run(clashes, groups)
        self.assertEqual(groups, snapshot)

    def test_s7_manual_group_is_invisible_to_formation(self):
        tray = ref(element_id=1, unique_id='CBL1', category='Cable Trays')
        manual_rows = [clash('m-{0}'.format(i), tray,
                             ref(element_id=10 + i,
                                 unique_id='X{0}'.format(i),
                                 category='Ducts'),
                             [50.0 + i, 50.0, 0.0], cluster_n=1)
                       for i in range(3)]
        other_rows = [clash('o-{0}'.format(i), tray,
                            ref(element_id=20 + i,
                                unique_id='Y{0}'.format(i),
                                category='Pipes'),
                            [60.0 + i, 50.0, 0.0], cluster_n=1)
                      for i in range(3)]
        g = make_group('g-man', [c['id'] for c in manual_rows],
                       axis='manual', anchor=None,
                       title='coordinate with sprinkler sub',
                       title_locked=True)
        out, _ = run(manual_rows + other_rows, [g])
        man = [x for x in out if x['id'] == 'g-man'][0]
        self.assertEqual(sorted(man['member_ids']),
                         ['m-0', 'm-1', 'm-2'])          # verbatim
        self.assertEqual(man['title'], 'coordinate with sprinkler sub')
        # the other 3 clashes are free to form their own star
        stars = [x for x in out if x['axis'] == 'element']
        self.assertEqual(len(stars), 1)
        self.assertEqual(sorted(stars[0]['member_ids']),
                         ['o-0', 'o-1', 'o-2'])

    def test_s7_member_resolving_keeps_roster_and_counts(self):
        tray = ref(element_id=1, unique_id='CBL1', category='Cable Trays')
        rows = [clash('m-{0}'.format(i), tray,
                      ref(element_id=10 + i, unique_id='X{0}'.format(i),
                          category='Ducts'),
                      [50.0 + i, 50.0, 0.0])
                for i in range(3)]
        rows[0]['status'] = 'Resolved'
        g = make_group('g-man', [c['id'] for c in rows], axis='manual',
                       title_locked=True)
        out, _ = run(rows, [g])
        man = out[0]
        self.assertEqual(len(man['member_ids']), 3)
        self.assertEqual(man['rollup']['n_open'], 2)
        self.assertEqual(man['status'], 'Open')

    def test_s5_half_resolved_rack_drifts_but_never_splits(self):
        rows = rack(16)
        for i, c in enumerate(rows):
            if i < 4:
                c['midpoint'] = [0.0 + 0.2 * i, 0.0, 0.0]
            elif i < 8:
                c['midpoint'] = [30.0 + 0.2 * i, 0.0, 0.0]
            else:
                c['status'] = 'Resolved'
        g = make_group('g-rack', [c['id'] for c in rows], axis='cluster',
                       title='Rack', title_locked=True)
        out, _ = run(rows, [g])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 'g-rack')
        self.assertEqual(len(out[0]['member_ids']), 16)
        self.assertTrue(out[0]['rollup']['drifted'])

    def test_group_auto_resolves_and_reopens(self):
        rows = duct_star(3)
        g = make_group('g-r', [c['id'] for c in rows],
                       anchor={'source': 'host', 'unique_id': 'DUCT1',
                               'element_id': 1, 'name': 'DD-03',
                               'category': 'Ducts', 'link_doc_title': None})
        for c in rows:
            c['status'] = 'Resolved'
        out, _ = run(rows, [g])
        self.assertEqual(out[0]['status'], 'Resolved')
        self.assertEqual(out[0]['history'][-1]['action'], 'auto_resolved')
        rows[0]['status'] = 'Open'
        out2, _ = run(rows, out)
        self.assertEqual(out2[0]['status'], 'Open')
        self.assertEqual(out2[0]['history'][-1]['action'], 'reopened')

    def test_successor_tier1_same_pair_silently_rekeys(self):
        duct = ref(element_id=1, unique_id='DUCT1', category='Ducts')
        beam = ref(source='link:Structural', element_id=20, unique_id='BM',
                   category='Structural Framing')
        old = clash('old', duct, beam, [0.0, 0.0, 0.0], status='Resolved',
                    first_seen=R1, last_seen=R1,
                    history=[{'author': 'system', 'action': 'auto_resolved',
                              'at': R2_LATER}])
        new = clash('new', duct, beam, [0.9, 0.0, 0.0])
        keep = clash('keep', duct,
                     ref(source='link:Structural', element_id=21,
                         unique_id='BM2', category='Structural Framing'),
                     [5.0, 0.0, 0.0], first_seen=R1)
        g = make_group('g-a', ['old', 'keep'],
                       anchor=None, axis='manual', title_locked=True)
        clashes_in = [old, new, keep]
        out, summary = run(clashes_in, [g])
        roster = members_of(out, 'g-a')
        self.assertIn('new', roster)
        self.assertNotIn('old', roster)
        self.assertEqual(summary['adopted'], 1)
        actions = [h['action'] for h in out[0]['history']]
        self.assertIn('member_rekeyed', actions)
        # the evicted predecessor must not keep a phantom group_id stamp
        self.assertIsNone(old['group_id'])
        self.assertEqual(new['group_id'], 'g-a')
        # ...and a pure rekey is identity continuity, never "+1 new"
        self.assertEqual(out[0]['rollup']['n_new_run'], 0)

    def test_prior_run_resolved_member_is_never_evicted(self):
        duct = ref(element_id=1, unique_id='DUCT1', category='Ducts')
        beam = ref(source='link:Structural', element_id=20, unique_id='BM',
                   category='Structural Framing')
        old = clash('old', duct, beam, [0.0, 0.0, 0.0], status='Resolved',
                    first_seen=R1, last_seen=R1,
                    history=[{'author': 'system', 'action': 'auto_resolved',
                              'at': R1_LATER}])       # resolved LAST run
        far = clash('far', duct, beam, [400.0, 0.0, 0.0])  # same pair, new
        g = make_group('g-p', ['old'], axis='manual', title_locked=True)
        out, summary = run([old, far], [g])
        self.assertEqual(members_of(out, 'g-p'), ['old'])
        self.assertEqual(summary['adopted'], 0)
        self.assertEqual(out[0]['suggested_ids'], [])

    def test_run_iso_none_skips_adoption_and_preserves_counters(self):
        duct = ref(element_id=1, unique_id='DUCT1', category='Ducts')
        beam = ref(source='link:Structural', element_id=20, unique_id='BM',
                   category='Structural Framing')
        old = clash('old', duct, beam, [0.0, 0.0, 0.0], status='Resolved',
                    first_seen=R1, last_seen=R1,
                    history=[{'author': 'system', 'action': 'auto_resolved',
                              'at': R2_LATER}])
        new = clash('new', duct, beam, [0.9, 0.0, 0.0])
        g = make_group('g-n', ['old'], axis='manual', title_locked=True)
        g['rollup'] = {'n_new_run': 7, 'n_resolved_run': 3,
                       'n_reopened_run': 1}
        out, summary = run([old, new], [g], run_iso=None)
        self.assertEqual(members_of(out, 'g-n'), ['old'])
        self.assertEqual(summary['adopted'], 0)
        r = out[0]['rollup']
        self.assertEqual((r['n_new_run'], r['n_resolved_run'],
                          r['n_reopened_run']), (7, 3, 1))

    def test_successor_tier2_different_element_is_suggestion_only(self):
        duct = ref(element_id=1, unique_id='DUCT1', category='Ducts')
        beam = ref(source='link:Structural', element_id=20, unique_id='BM',
                   category='Structural Framing')
        beam2 = ref(source='link:Structural', element_id=99, unique_id='BM9',
                    category='Structural Framing')
        old = clash('old', duct, beam, [0.0, 0.0, 0.0], status='Resolved',
                    first_seen=R1, last_seen=R1,
                    history=[{'author': 'system', 'action': 'auto_resolved',
                              'at': R2_LATER}])
        new = clash('new', ref(element_id=2, unique_id='DUCT2',
                               category='Ducts'), beam2, [3.0, 0.0, 0.0])
        g = make_group('g-b', ['old'], axis='manual', title_locked=True)
        out, summary = run([old, new], [g])
        self.assertEqual(members_of(out, 'g-b'), ['old'])
        self.assertIn('new', out[0]['suggested_ids'])
        self.assertEqual(summary['suggested'], 1)

    def test_anchor_key_is_frozen_participation_shifts_never_rekey(self):
        rows = duct_star(4)
        out, _ = run(rows)
        gid = out[0]['id']
        self.assertEqual(out[0]['anchor']['unique_id'], 'DUCT1')
        # next run: a new clash touches DUCT1 but ALSO touches an element
        # with far higher participation in the new pool
        wall = ref(source='link:Architectural', element_id=70,
                   unique_id='WALLX', category='Walls')
        joiner = clash('join-1', rows[0]['ref_a'], wall, [90.0, 0.0, 0.0])
        wall_rows = [clash('w-{0}'.format(i),
                           ref(element_id=600 + i,
                               unique_id='Q{0}'.format(i),
                               category='Pipes'),
                           wall, [90.0 + i, 0.0, 0.0])
                     for i in range(5)]
        out2, _ = run(rows + [joiner] + wall_rows, out)
        g = [x for x in out2 if x['id'] == gid][0]
        self.assertEqual(g['anchor']['unique_id'], 'DUCT1')   # frozen
        self.assertIn('join-1', g['member_ids'])              # claimed first
        # the wall star forms separately from the remaining 5
        walls = [x for x in out2 if x['id'] != gid and x['axis'] == 'element']
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0]['anchor']['unique_id'], 'WALLX')

    def test_needs_review_not_set_when_group_still_open(self):
        rows = duct_star(4)
        out, _ = run(rows)
        joiner = clash('late', rows[0]['ref_a'],
                       ref(source='link:Structural', element_id=888,
                           unique_id='LB', category='Structural Framing'),
                       [200.0, 0.0, 0.0])
        out2, _ = run(rows + [joiner], out)
        g = out2[0]
        self.assertIn('late', g['member_ids'])
        self.assertFalse(g['needs_review'])

    def test_stale_suggestions_cleared_once_clash_is_grouped(self):
        rows = duct_star(3)
        g_other = make_group('g-x', [], axis='manual', title_locked=True)
        g_other['suggested_ids'] = [rows[0]['id'], 'ghost-id']
        out, _ = run(rows, [g_other])
        gx = [x for x in out if x['id'] == 'g-x'][0]
        self.assertEqual(gx['suggested_ids'], [])

    def test_dead_suggestions_are_pruned(self):
        lone = clash('lone', ref(element_id=1, unique_id='A1'),
                     ref(element_id=2, unique_id='A2', category='Ducts'),
                     [0.0, 0.0, 0.0], status='Resolved')
        supp = clash('supp', ref(element_id=3, unique_id='A3'),
                     ref(element_id=4, unique_id='A4', category='Ducts'),
                     [50.0, 0.0, 0.0], suppressed=True)
        live = clash('live', ref(element_id=5, unique_id='A5'),
                     ref(element_id=6, unique_id='A6', category='Ducts'),
                     [90.0, 0.0, 0.0])
        g = make_group('g-d', [], axis='manual', title_locked=True)
        g['suggested_ids'] = ['lone', 'supp', 'live']
        out, _ = run([lone, supp, live], [g])
        self.assertEqual(out[0]['suggested_ids'], ['live'])

    def test_p6_stray_auto_joins_untouched_cluster_but_only_suggests_into_curated(self):
        for curated in (False, True):
            _SEQ[0] = 0
            rows = rack(12, cx=0.0, prefix='p6')
            g = make_group('g-c', [c['id'] for c in rows], axis='cluster',
                           title='G', title_locked=curated)
            stray = clash('stray', ref(element_id=901, unique_id='S1'),
                          ref(element_id=902, unique_id='S2',
                              category='Ducts'),
                          [3.0, 0.0, 0.0], cluster_n=2)
            out, _ = run(rows + [stray], [g])
            gg = [x for x in out if x['id'] == 'g-c'][0]
            if curated:
                self.assertNotIn('stray', gg['member_ids'])
                self.assertIn('stray', gg['suggested_ids'])
            else:
                self.assertIn('stray', gg['member_ids'])

    def test_p6_reviewed_status_counts_as_curated(self):
        rows = rack(12, cx=0.0, prefix='p6b')
        g = make_group('g-rv', [c['id'] for c in rows], axis='cluster',
                       title='G', status='Reviewed')
        stray = clash('stray2', ref(element_id=901, unique_id='S3'),
                      ref(element_id=902, unique_id='S4', category='Ducts'),
                      [3.0, 0.0, 0.0], cluster_n=2)
        out, _ = run(rows + [stray], [g])
        gg = [x for x in out if x['id'] == 'g-rv'][0]
        self.assertNotIn('stray2', gg['member_ids'])
        self.assertIn('stray2', gg['suggested_ids'])

    def test_p6_stray_joins_the_truly_nearest_group(self):
        rows_a = rack(12, cx=0.0, prefix='na')
        rows_b = rack(12, cx=8.0, prefix='nb')
        ga = make_group('g-a2', [c['id'] for c in rows_a], axis='cluster')
        gb = make_group('g-b2', [c['id'] for c in rows_b], axis='cluster')
        stray = clash('stray3', ref(element_id=901, unique_id='S5'),
                      ref(element_id=902, unique_id='S6', category='Ducts'),
                      [5.5, 0.0, 0.0], cluster_n=2)
        out, _ = run(rows_a + rows_b + [stray], [ga, gb])
        gb_out = [x for x in out if x['id'] == 'g-b2'][0]
        ga_out = [x for x in out if x['id'] == 'g-a2'][0]
        self.assertIn('stray3', gb_out['member_ids'])   # 2.5 ft vs 4.8 ft
        self.assertNotIn('stray3', ga_out['member_ids'])

    def test_merged_into_tombstone_passes_through_untouched(self):
        rows = duct_star(3)
        tomb = make_group('g-t', ['dangling-1'], axis='element',
                          title='Old issue', title_locked=True,
                          status='MergedInto')
        tomb['suggested_ids'] = ['dangling-2']
        snapshot = copy.deepcopy(tomb)
        out, summary = run(rows, [tomb])
        tomb_out = [x for x in out if x['id'] == 'g-t'][0]
        self.assertEqual(tomb_out, snapshot)
        self.assertEqual(summary['groups_total'], 1)    # the new star only

    def test_member_reopen_under_human_status_flags_needs_review(self):
        rows = duct_star(3)
        rows[0]['status'] = 'Open'
        rows[0]['history'] = [{'author': 'system', 'action': 'reopened',
                               'at': R2_LATER}]
        g = make_group('g-ap', [c['id'] for c in rows], axis='manual',
                       title_locked=True, status='Approved')
        out, _ = run(rows, [g])
        gg = out[0]
        self.assertEqual(gg['status'], 'Approved')      # human status kept
        self.assertTrue(gg['needs_review'])
        self.assertEqual(gg['history'][-1]['action'], 'needs_review')


# ---------------------------------------------------------------------------
# Rollup + titles + determinism
# ---------------------------------------------------------------------------

class RollupTests(unittest.TestCase):
    def test_band_is_max_open_member_resolved_never_counts(self):
        rows = duct_star(4)
        rows[0]['importance']['band'] = 'Critical'
        rows[0]['importance']['score'] = 95
        rows[0]['status'] = 'Resolved'
        out, _ = run(rows)
        r = out[0]['rollup']
        self.assertEqual(r['band'], 'Major')
        self.assertEqual(r['n_open'], 3)

    def test_suppressed_member_keeps_seat_but_not_counted_open(self):
        rows = duct_star(4)
        g = make_group('g-s', [c['id'] for c in rows],
                       axis='manual', title_locked=True)
        rows[0]['importance']['suppressed'] = True
        out, _ = run(rows, [g])
        r = out[0]['rollup']
        self.assertEqual(len(out[0]['member_ids']), 4)
        self.assertEqual(r['n_suppressed'], 1)
        self.assertEqual(r['n_open'], 3)

    def test_cluster_title_carries_level_count_trades(self):
        rows = rack(12)
        out, _ = run(rows)
        t = out[0]['title']
        self.assertIn('L2', t)
        self.assertIn('12', t)

    def test_untouched_title_regenerates_locked_title_does_not(self):
        rows = duct_star(4)
        out, _ = run(rows)
        auto = out[0]['title']
        self.assertTrue(auto)
        out[0]['title'] = 'My name'
        out[0]['title_locked'] = True
        out2, _ = run(rows, out)
        self.assertEqual(out2[0]['title'], 'My name')

    def test_old_data_without_unique_ids_still_groups_stably(self):
        duct = ref(element_id=1, unique_id=None, category='Ducts')
        rows = [clash('od-{0}'.format(i), duct,
                      ref(source='link:Structural', element_id=100 + i,
                          unique_id=None, category='Structural Framing'),
                      [5.0 * i, 0.0, 0.0], cluster_n=1)
                for i in range(4)]
        out, _ = run(rows)
        self.assertEqual(len(out), 1)
        # and the anchor still claims on the next run
        late = clash('od-late', duct,
                     ref(source='link:Structural', element_id=999,
                         unique_id=None, category='Structural Framing'),
                     [90.0, 0.0, 0.0])
        out2, _ = run(rows + [late], out)
        self.assertIn('od-late', out2[0]['member_ids'])

    def test_deterministic_structure_across_repeat_runs(self):
        def build():
            _SEQ[0] = 0
            return duct_star(6) + rack(14, cx=200.0)
        out1, _ = run(build())
        out2, _ = run(build())
        shape1 = sorted((g['axis'], tuple(sorted(g['member_ids'])),
                         g['title']) for g in out1)
        shape2 = sorted((g['axis'], tuple(sorted(g['member_ids'])),
                         g['title']) for g in out2)
        self.assertEqual(shape1, shape2)

    def test_duplicate_titles_get_a_centroid_locator(self):
        _SEQ[0] = 0
        rows = rack(12, cx=0.0, prefix='da') + rack(12, cx=100.0, prefix='db')
        groups, _ = run(rows)
        clusters = [g for g in groups if g['axis'] == 'cluster']
        self.assertEqual(len(clusters), 2)
        titles = [g['title'] for g in clusters]
        self.assertNotEqual(titles[0], titles[1])
        self.assertTrue(all('@' in t for t in titles), titles)

    def test_unique_titles_stay_clean_and_locked_titles_untouched(self):
        rows = duct_star(4)
        groups, _ = run(rows)
        self.assertNotIn('@', groups[0]['title'])
        groups[0]['title'] = 'My issue'
        groups[0]['title_locked'] = True
        rows2 = rack(12, cx=0.0, prefix='u1') + rack(12, cx=80.0, prefix='u2')
        out, _ = run(rows + rows2, groups)
        mine = [g for g in out if g.get('title_locked')][0]
        self.assertEqual(mine['title'], 'My issue')

    def test_element_stars_never_flag_drifted(self):
        rows = duct_star(6, dx=11.0)      # members spread 55 ft apart
        groups, _ = run(rows)
        self.assertEqual(groups[0]['axis'], 'element')
        self.assertFalse(groups[0]['rollup']['drifted'])

    def test_fully_decided_group_keeps_its_historical_band(self):
        rows = duct_star(3)
        rows[0]['importance']['band'] = 'Critical'
        rows[0]['importance']['score'] = 95
        g = make_group('g-hist', [c['id'] for c in rows], axis='manual',
                       title_locked=True)
        for c in rows:
            c['status'] = 'Resolved'
        out, _ = run(rows, [g])
        r = out[0]['rollup']
        self.assertEqual(r['band'], 'Critical')     # not null / "0 Minor"
        self.assertEqual(r['score'], 95)
        self.assertEqual(r['n_open'], 0)

    def test_malformed_group_without_member_ids_never_kills_grouping(self):
        rows = duct_star(4)
        broken = {'id': 'g-broken', 'axis': 'element', 'status': 'Open',
                  'title': 'hand-edited', 'title_locked': False}
        out, summary = run(rows, [broken])
        self.assertEqual(summary['groups_total'], 2)   # broken + new star
        star = [g for g in out if g['id'] != 'g-broken'][0]
        self.assertEqual(len(star['member_ids']), 4)

    def test_group_seqs_are_unique_and_stable(self):
        _SEQ[0] = 0
        rows = duct_star(4) + rack(12, cx=200.0)
        groups, _ = run(rows)
        seqs = [g['seq'] for g in groups]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertTrue(all(isinstance(s, int) and s > 0 for s in seqs))
        by_id = dict((g['id'], g['seq']) for g in groups)
        out2, _ = run(rows, groups)
        for g in out2:
            self.assertEqual(g['seq'], by_id[g['id']])   # never re-minted

    def test_s8_mixed_first_run_summary_is_consistent(self):
        _SEQ[0] = 0
        rows = (duct_star(12) +
                rack(20, cx=300.0) +
                [clash('lone-{0}'.format(i),
                       ref(element_id=700 + i, unique_id='L{0}'.format(i)),
                       ref(source='link:Structural', element_id=800 + i,
                           unique_id='M{0}'.format(i),
                           category='Structural Framing'),
                       [500.0 + 20.0 * i, 0.0, 0.0], cluster_n=0)
                 for i in range(10)])
        groups, summary = run(rows)
        self.assertEqual(summary['groups_new'], 2)
        self.assertEqual(summary['ungrouped'], 10)
        grouped = set()
        for g in groups:
            for m in g['member_ids']:
                self.assertNotIn(m, grouped)     # exclusive membership
                grouped.add(m)
        self.assertEqual(len(grouped), 32)



# ---------------------------------------------------------------------------
# Group operations (the groupop: channel's engine)
# ---------------------------------------------------------------------------

from clash_group import ops as group_ops


class OpsTests(unittest.TestCase):
    def _world(self):
        _SEQ[0] = 0
        rows = duct_star(4)
        groups, _ = run(rows)
        return rows, groups, groups[0]['id']

    def test_rename_locks_and_curates(self):
        rows, groups, gid = self._world()
        changed, err = group_ops.apply_op(
            rows, groups, {'op': 'rename', 'group_id': gid,
                           'title': 'DD-03 conflicts'}, user='nathan')
        self.assertIsNone(err)
        g = groups[0]
        self.assertEqual(g['title'], 'DD-03 conflicts')
        self.assertTrue(g['title_locked'])
        self.assertTrue(g['curated'])
        self.assertEqual(g['history'][-1]['author'], 'nathan')
        # a later regroup must not regenerate the locked title
        out, _ = run(rows, groups)
        self.assertEqual(out[0]['title'], 'DD-03 conflicts')

    def test_rename_empty_title_errors_without_mutation(self):
        rows, groups, gid = self._world()
        before = copy.deepcopy(groups)
        changed, err = group_ops.apply_op(
            rows, groups, {'op': 'rename', 'group_id': gid, 'title': '  '})
        self.assertTrue(err)
        self.assertEqual(groups, before)

    def test_status_validates(self):
        rows, groups, gid = self._world()
        _c, err = group_ops.apply_op(
            rows, groups, {'op': 'status', 'group_id': gid,
                           'status': 'Banana'})
        self.assertTrue(err)
        _c, err = group_ops.apply_op(
            rows, groups, {'op': 'status', 'group_id': gid,
                           'status': 'Reviewed'})
        self.assertIsNone(err)
        self.assertEqual(groups[0]['status'], 'Reviewed')

    def test_accept_suggestion_moves_into_roster(self):
        rows, groups, gid = self._world()
        extra = clash('sugg-1', ref(element_id=77, unique_id='EX'),
                      ref(element_id=78, unique_id='EX2', category='Ducts'),
                      [300.0, 0.0, 0.0])
        rows.append(extra)
        groups[0]['suggested_ids'] = ['sugg-1']
        _c, err = group_ops.apply_op(
            rows, groups, {'op': 'accept_suggestion', 'group_id': gid,
                           'clash_id': 'sugg-1'})
        self.assertIsNone(err)
        self.assertIn('sugg-1', groups[0]['member_ids'])
        self.assertEqual(groups[0]['suggested_ids'], [])
        self.assertEqual(extra['group_id'], gid)

    def test_new_from_selection_is_the_split_gesture(self):
        rows, groups, gid = self._world()
        pulled = [rows[0]['id'], rows[1]['id']]
        changed, err = group_ops.apply_op(
            rows, groups, {'op': 'new_from_selection', 'clash_ids': pulled,
                           'title': 'talk to structural'}, user='nathan')
        self.assertIsNone(err)
        manual = groups[-1]
        self.assertEqual(manual['axis'], 'manual')
        self.assertEqual(sorted(manual['member_ids']), sorted(pulled))
        self.assertTrue(manual['title_locked'])
        self.assertEqual(manual['lineage']['split_from'], gid)
        donor = groups[0]
        self.assertEqual(len(donor['member_ids']), 2)     # 4 - 2 pulled
        self.assertEqual(donor['history'][-1]['action'], 'members_moved_out')
        self.assertEqual(rows[0]['group_id'], manual['id'])
        self.assertEqual(rows[2]['group_id'], gid)

    def test_remove_members_and_ungroup(self):
        rows, groups, gid = self._world()
        _c, err = group_ops.apply_op(
            rows, groups, {'op': 'remove_members', 'group_id': gid,
                           'clash_ids': [rows[0]['id']]})
        self.assertIsNone(err)
        self.assertEqual(len(groups[0]['member_ids']), 3)
        self.assertIsNone(rows[0]['group_id'])
        _c, err = group_ops.apply_op(
            rows, groups, {'op': 'ungroup', 'group_id': gid})
        self.assertIsNone(err)
        self.assertEqual(groups[0]['member_ids'], [])
        self.assertEqual(groups[0]['status'], 'Resolved')
        self.assertTrue(all(c['group_id'] is None for c in rows))

    def test_merge_keeps_the_more_curated_group(self):
        rows, groups, gid = self._world()
        more = rack(12, cx=500.0, prefix='mg')
        rows.extend(more)
        groups2, _ = run(rows, groups)
        gids = [g['id'] for g in groups2 if g.get('status') != 'MergedInto']
        self.assertEqual(len(gids), 2)
        # curate the SECOND group so it must survive
        second = [g for g in groups2 if g['id'] != gid][0]
        group_ops.apply_op(rows, groups2,
                           {'op': 'rename', 'group_id': second['id'],
                            'title': 'THE issue'})
        _c, err = group_ops.apply_op(
            rows, groups2, {'op': 'merge', 'group_ids': gids})
        self.assertIsNone(err)
        survivor = [g for g in groups2 if g['id'] == second['id']][0]
        tomb = [g for g in groups2 if g['id'] == gid][0]
        self.assertEqual(survivor['title'], 'THE issue')
        self.assertEqual(len(survivor['member_ids']), 16)
        self.assertEqual(tomb['status'], 'MergedInto')
        self.assertEqual(tomb['member_ids'], [])
        self.assertEqual(tomb['lineage']['merged_into'], survivor['id'])
        self.assertIn(gid, survivor['lineage']['merged_from'])
        # every clash restamped onto the survivor
        self.assertTrue(all(c['group_id'] == survivor['id']
                            for c in rows if c['id'].startswith(('s1-', 'mg-'))))

    def test_unknown_op_is_a_soft_error(self):
        rows, groups, _gid = self._world()
        _c, err = group_ops.apply_op(rows, groups, {'op': 'explode'})
        self.assertTrue(err)


# ---------------------------------------------------------------------------
# Wiring integrity: the silent-drop guard
# ---------------------------------------------------------------------------

class WiringIntegrityTests(unittest.TestCase):
    """Both run pipelines rebuild clashes.json's top level from a literal.
    A writer that forgets the `groups` key silently wipes every named
    group on the next run — the likeliest real-world data-loss bug, so it
    must fail the suite, not a coordinator's Tuesday meeting."""

    _PANEL = os.path.abspath(os.path.join(
        _LIB, "..", "dbHMS Tools.tab", "Clash Detection.panel"))

    # 2026-07: the legacy Run Clash Test writer was deleted with the old
    # WPF suite; the Clash Detection web app is the only run pipeline.
    _WRITER_SCRIPTS = (
        os.path.join("Clash Detection.pushbutton", "script.py"),
    )

    def test_every_run_writer_literal_carries_the_groups_key(self):
        import io
        for rel in self._WRITER_SCRIPTS:
            path = os.path.join(self._PANEL, rel)
            with io.open(path, "r", encoding="utf-8") as f:
                src = f.read()
            starts = [i for i in range(len(src))
                      if src.startswith("new_data = {", i)]
            self.assertTrue(starts, "{0}: no new_data literal found".format(rel))
            for i in starts:
                block = src[i:i + 800]
                self.assertIn("'groups'", block,
                              "{0}: a new_data literal is missing the "
                              "'groups' key - this silently wipes every "
                              "named group on the next run".format(rel))
                self.assertIn("'clashes'", block, rel)

    def test_read_clashes_default_includes_groups(self):
        from clash_core import persistence
        orig = persistence.clashes_path
        persistence.clashes_path = lambda ph: os.path.join(
            os.path.dirname(__file__), "does-not-exist-{0}.json".format(ph))
        try:
            data = persistence.read_clashes("test-hash")
        finally:
            persistence.clashes_path = orig
        self.assertIn("groups", data)
        self.assertEqual(data["groups"], [])

    def test_read_clashes_backfills_groups_on_old_files(self):
        import io as _io
        import json as _json
        import tempfile
        from clash_core import persistence
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "clashes.json")
        with _io.open(path, "w", encoding="utf-8") as f:
            f.write(_json.dumps({"schema_version": 1, "clashes": []}))
        orig = persistence.clashes_path
        persistence.clashes_path = lambda ph: path
        try:
            data = persistence.read_clashes("x")
        finally:
            persistence.clashes_path = orig
        self.assertEqual(data["groups"], [])


if __name__ == '__main__':
    unittest.main()
