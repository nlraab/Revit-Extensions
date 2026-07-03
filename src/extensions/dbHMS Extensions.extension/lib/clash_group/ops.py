# -*- coding: utf-8 -*-
"""Human group operations (the `groupop:` channel's engine).

Every mutation a coordinator can make to a group lives here, pure and
CPython-testable, so the host stays a thin JSON-in/JSON-out shim. Every op
marks the group curated (from then on the machine only ever suggests) and
appends a history entry. Ops enforce the design invariants:

  - membership is exclusive: pulling clashes into a group removes them
    from any other group's roster (history on both sides);
  - groups are never deleted: ungroup empties the roster and closes the
    record; merge leaves a MergedInto tombstone so ids never dangle;
  - splits/merges are exactly the human acts the design allows
    ("new_from_selection" IS the split gesture: select members, pull them
    out into a new group).

`apply_op(clashes, groups, op, user)` mutates the given lists in place
(the host does read -> apply -> atomic write) and returns
(changed_group_ids, error). `error` is a user-facing string or None; on
error nothing was mutated.
"""

import uuid

import clash_group


VALID_STATUSES = ('Open', 'Reviewed', 'Approved', 'Resolved')


def apply_op(clashes, groups, op, user='user'):
    """Apply one group operation. Returns (changed_group_ids, error)."""
    if not isinstance(op, dict):
        return [], 'Malformed operation.'
    kind = op.get('op')
    handler = _HANDLERS.get(kind)
    if handler is None:
        return [], 'Unknown group operation: {0!r}'.format(kind)
    by_id = {}
    for c in clashes:
        cid = c.get('id')
        if cid:
            by_id[cid] = c
    try:
        changed = handler(clashes, groups, by_id, op, user)
    except _OpError as ex:
        return [], str(ex)
    # Refresh rollups + per-clash stamps for everything the op touched.
    changed_set = set(changed)
    for g in groups:
        if g['id'] in changed_set:
            g['rollup'] = clash_group.rollup(g, by_id)
    _restamp(clashes, groups, changed_set)
    return list(changed), None


class _OpError(Exception):
    pass


def _find(groups, gid):
    for g in groups:
        if g.get('id') == gid:
            return g
    raise _OpError('Group not found (was it merged away?). Refresh and retry.')


def _curate(g, user, action, before=None, after=None):
    g['curated'] = True
    _hist(g, user, action, before=before, after=after)


def _hist(g, user, action, before=None, after=None):
    try:
        from clash_core import models
        entry = models.make_history_entry(user, action, before=before,
                                          after=after)
    except Exception:
        entry = {'author': user, 'action': action, 'before': before,
                 'after': after, 'at': clash_group._now_iso()}
    g.setdefault('history', []).append(entry)


def _restamp(clashes, groups, changed_set):
    """Re-derive per-clash group_id for clashes touched by the op (cheap:
    full pass, membership map is authoritative)."""
    member_of = {}
    for g in groups:
        for mid in g.get('member_ids') or []:
            member_of[mid] = g['id']
    for c in clashes:
        c['group_id'] = member_of.get(c.get('id'))


def _pull_members(groups, clash_ids, user, into_gid):
    """Remove clash_ids from every other roster (exclusive membership)."""
    idset = set(clash_ids)
    for g in groups:
        if g.get('id') == into_gid:
            continue
        roster = g.get('member_ids') or []
        taken = [m for m in roster if m in idset]
        if not taken:
            continue
        g['member_ids'] = [m for m in roster if m not in idset]
        g['suggested_ids'] = [s for s in (g.get('suggested_ids') or [])
                              if s not in idset]
        _hist(g, user, 'members_moved_out',
              after='{0} member(s) -> another group'.format(len(taken)))


# ---------------------------------------------------------------------------
# Handlers (each returns the list of changed group ids)
# ---------------------------------------------------------------------------

def _op_rename(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    title = (op.get('title') or '').strip()
    if not title:
        raise _OpError('A group name cannot be empty.')
    before = g.get('title')
    g['title'] = title
    g['title_locked'] = True
    _curate(g, user, 'renamed', before=before, after=title)
    return [g['id']]


def _op_assign(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    assignee = op.get('assignee') or None
    before = g.get('assignee')
    g['assignee'] = assignee
    _curate(g, user, 'assigned', before=before, after=assignee)
    return [g['id']]


def _op_status(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    status = op.get('status')
    if status not in VALID_STATUSES:
        raise _OpError('Invalid status: {0!r}'.format(status))
    before = g.get('status')
    g['status'] = status
    if status == 'Open':
        g['needs_review'] = False
    _curate(g, user, 'status_changed', before=before, after=status)
    return [g['id']]


def _op_comment(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    body = (op.get('body') or '').strip()
    if not body:
        raise _OpError('An empty comment.')
    try:
        from clash_core import models
        comment = models.make_comment(user, body)
    except Exception:
        comment = {'author': user, 'at': clash_group._now_iso(),
                   'body': body}
    g.setdefault('comments', []).append(comment)
    g['curated'] = True
    return [g['id']]


def _op_accept_suggestion(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    cid = op.get('clash_id')
    if cid not in (g.get('suggested_ids') or []):
        raise _OpError('That suggestion is no longer available.')
    g['suggested_ids'] = [s for s in g['suggested_ids'] if s != cid]
    _pull_members(groups, [cid], user, g['id'])
    g.setdefault('member_ids', []).append(cid)
    _curate(g, user, 'member_joined', after=cid)
    return [g['id']]


def _op_dismiss_suggestion(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    cid = op.get('clash_id')
    g['suggested_ids'] = [s for s in (g.get('suggested_ids') or [])
                          if s != cid]
    _curate(g, user, 'suggestion_dismissed', after=cid)
    return [g['id']]


def _op_new_from_selection(clashes, groups, by_id, op, user):
    ids = [i for i in (op.get('clash_ids') or []) if i in by_id]
    if not ids:
        raise _OpError('Select at least one clash first.')
    donors = [g['id'] for g in groups
              if set(ids) & set(g.get('member_ids') or [])]
    _pull_members(groups, ids, user, None)
    title = (op.get('title') or '').strip()
    g = {
        'id': str(uuid.uuid4()),
        'created_at': clash_group._now_iso(),
        'created_by': user,
        'axis': 'manual',
        'anchor': None,
        'title': title or 'Manual group ({0})'.format(len(ids)),
        'title_locked': bool(title),
        'status': 'Open',
        'assignee': None,
        'member_ids': list(ids),
        'suggested_ids': [],
        'needs_review': False,
        'curated': True,
        'lineage': {'split_from': donors[0] if len(donors) == 1 else None,
                    'merged_from': [], 'merged_into': None},
        'history': [],
        'comments': [],
        'rep_clash_id': ids[0],
        'rollup': {},
    }
    _hist(g, user, 'created', after='{0} members (manual)'.format(len(ids)))
    groups.append(g)
    return [g['id']] + donors


def _op_remove_members(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    ids = set(op.get('clash_ids') or [])
    if not ids:
        raise _OpError('Nothing selected to remove.')
    before_n = len(g.get('member_ids') or [])
    g['member_ids'] = [m for m in (g.get('member_ids') or [])
                       if m not in ids]
    removed = before_n - len(g['member_ids'])
    if not removed:
        raise _OpError('The selected clashes are not members of this group.')
    _curate(g, user, 'members_removed',
            after='{0} member(s)'.format(removed))
    return [g['id']]


def _op_ungroup(clashes, groups, by_id, op, user):
    g = _find(groups, op.get('group_id'))
    n = len(g.get('member_ids') or [])
    g['member_ids'] = []
    g['suggested_ids'] = []
    g['status'] = 'Resolved'
    _curate(g, user, 'ungrouped', after='{0} member(s) released'.format(n))
    return [g['id']]


def _op_merge(clashes, groups, by_id, op, user):
    gids = op.get('group_ids') or []
    gs = [_find(groups, gid) for gid in gids]
    gs = [g for g in gs if g.get('status') != 'MergedInto']
    if len(gs) < 2:
        raise _OpError('Select two or more groups to merge.')

    def curation_weight(g):
        return (len(g.get('comments') or [])
                + (1 if g.get('title_locked') else 0)
                + (1 if g.get('assignee') else 0))

    # Survivor = the more-curated group; tie goes to the older record.
    gs_sorted = sorted(gs, key=lambda g: (-curation_weight(g),
                                          g.get('created_at') or '',
                                          g.get('id') or ''))
    survivor = gs_sorted[0]
    absorbed = gs_sorted[1:]
    statuses = set(g.get('status') for g in gs)
    for g in absorbed:
        for m in g.get('member_ids') or []:
            if m not in survivor['member_ids']:
                survivor['member_ids'].append(m)
        for s in g.get('suggested_ids') or []:
            if (s not in survivor.setdefault('suggested_ids', [])
                    and s not in survivor['member_ids']):
                survivor['suggested_ids'].append(s)
        g['member_ids'] = []
        g['suggested_ids'] = []
        g['status'] = 'MergedInto'
        g.setdefault('lineage', {})['merged_into'] = survivor['id']
        _hist(g, user, 'merged_into', after=survivor['id'])
        survivor.setdefault('lineage', {}).setdefault(
            'merged_from', []).append(g['id'])
    if len(statuses) > 1:
        survivor['needs_review'] = True
    _curate(survivor, user, 'merged',
            after='{0} group(s) absorbed'.format(len(absorbed)))
    return [survivor['id']] + [g['id'] for g in absorbed]


_HANDLERS = {
    'rename': _op_rename,
    'assign': _op_assign,
    'status': _op_status,
    'comment': _op_comment,
    'accept_suggestion': _op_accept_suggestion,
    'dismiss_suggestion': _op_dismiss_suggestion,
    'new_from_selection': _op_new_from_selection,
    'remove_members': _op_remove_members,
    'ungroup': _op_ungroup,
    'merge': _op_merge,
}
