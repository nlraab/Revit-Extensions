# -*- coding: utf-8 -*-
"""Test orchestrator: turn a clash test definition into a list of raw clashes.

Sits on top of `hard` / `soft` / `linked`. Resolves the test's set_a /
set_b descriptors (host vs linked role, OST_ category list) into actual
Element collections, runs the right detection algorithm against every
combination of source buckets, and returns a flat list of raw clash dicts
ready for `clash_core.merge`.

A "bucket" is one (source, document, link_instance, elements) tuple - a
host source produces one bucket; a linked role produces one bucket per
link instance currently mapped to that role.
"""

from clash_core import models
from clash_detect import hard as hard_mod
from clash_detect import soft as soft_mod
from clash_detect import linked


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_test(doc, test_dict, role_map, log=None, trade_filter=None,
             tess_cache=None, ins_cache=None, status=None):
    """Run a single clash test and return a list of raw clash dicts.

    Each raw clash has:
        test_id, kind, ref_a, ref_b, midpoint, default_assignee
        (plus 'gap_inches' for soft clashes)

    `log` is an optional callable that receives one-line diagnostic strings
    (e.g. pyrevit's `output.print_md`). Used to surface per-test counts so
    the user can see what's actually happening - a test that returns 0
    might mean "no elements collected" or "elements collected but no
    intersection," and those are very different problems.

    `trade_filter` is an optional set/iterable of trade strings (e.g.
    {"Mechanical", "Plumbing"}). When provided, set_a is filtered down to
    only elements whose category resolves to one of those trades. Filtering
    only set_a (not set_b) means "show clashes where the *primary* element
    is in a selected trade" - the most useful interpretation for "give me
    just the Mechanical clashes."

    Caller is expected to merge these with the existing clashes.json via
    `clash_core.merge.merge_runs`.
    """
    test_id = test_dict.get('id') or ''
    test_name = test_dict.get('name') or test_id or '<unnamed>'
    kind = (test_dict.get('kind') or 'hard').lower()
    # Tessellation cache for the soft narrow phase. Callers running several
    # tests should pass ONE dict for the whole session so shared elements
    # (the same wall in three tests) tessellate once.
    if tess_cache is None:
        tess_cache = {}
    set_a = test_dict.get('set_a') or {}
    set_b = test_dict.get('set_b') or {}
    tolerance_in = float(test_dict.get('tolerance_inches') or 0.0)
    default_assignee = test_dict.get('default_assignee')

    if log:
        log("**Test:** {}  ({}, tol={} in)".format(test_name, kind, tolerance_in))
        if trade_filter:
            log("  - trade filter: {}".format(", ".join(sorted(trade_filter))))

    if kind == 'clearance':
        if log:
            log("  - skipped (clearance detection isn't implemented yet)")
        return []

    if log:
        log("  - resolving set_a (source `{}`):".format(set_a.get('source', 'host')))
    a_buckets = list(_resolve_buckets(doc, set_a, role_map, log=log))
    if log:
        log("  - resolving set_b (source `{}`):".format(set_b.get('source', 'host')))
    b_buckets = list(_resolve_buckets(doc, set_b, role_map, log=log))

    # Session-scope insulation cache (like tess_cache): buckets are rebuilt
    # per test, so enrichment's per-document insulation collector pass would
    # otherwise re-run for every test and side.
    if ins_cache is None:
        ins_cache = {}
    for bucket in a_buckets + b_buckets:
        bucket['_ins_cache'] = ins_cache

    # Apply the trade filter to set_a if provided
    if trade_filter:
        a_buckets = _filter_buckets_by_trade(a_buckets, trade_filter, log)

    if log:
        a_total = sum(len(ab['elements']) for ab in a_buckets)
        b_total = sum(len(bb['elements']) for bb in b_buckets)
        log("  - set_a total: **{}** element(s) across {} bucket(s)".format(
            a_total, len(a_buckets)))
        log("  - set_b total: **{}** element(s) across {} bucket(s)".format(
            b_total, len(b_buckets)))

    if not a_buckets:
        if log:
            log("  - **no set_a elements** (every category came back empty, "
                "or every element was filtered out by the trade filter)")
        return []
    if not b_buckets:
        if log:
            log("  - **no set_b elements** (every category came back empty; "
                "if this is a host-vs-link test, double-check the role "
                "mapping in Settings)")
        return []

    out = []
    for a_bucket in a_buckets:
        for b_bucket in b_buckets:
            try:
                pairs = _run_detection(
                    doc, kind, tolerance_in,
                    a_bucket['elements'], b_bucket['elements'],
                    a_bucket['link_instance'], b_bucket['link_instance'],
                    tess_cache=tess_cache, log=log, status=status,
                )
            except Exception as ex:
                if log:
                    log("  - **detection error**: {}".format(ex))
                continue
            if log:
                log("  - {} x {} -> {} clash(es)".format(
                    a_bucket['source'], b_bucket['source'], len(pairs)))
            for pair in pairs:
                clash = {
                    'test_id':          test_id,
                    'kind':             kind,
                    'ref_a':            _make_ref(pair['elem_a'], a_bucket),
                    'ref_b':            _make_ref(pair['elem_b'], b_bucket),
                    'midpoint':         _xyz_to_list(pair.get('midpoint')),
                    'default_assignee': default_assignee,
                    # The test's tolerance, stamped per clash so the pure
                    # scoring layer can compute gap/tolerance without a
                    # test-library lookup (refreshed via _PER_RUN_FIELDS).
                    'tolerance_inches': tolerance_in,
                }
                # Soft-clash measurement fields: the REAL surface gap, the
                # closest-point pair (host feet), whether the pair touches/
                # intersects, and how the gap was measured ('mesh' = true
                # geometry, 'bbox' = fallback estimate). Absent on hard rows.
                for k in ('gap_inches', 'is_contact', 'gap_method'):
                    if k in pair:
                        clash[k] = pair[k]
                for k in ('closest_point_a', 'closest_point_b'):
                    if pair.get(k) is not None:
                        clash[k] = _xyz_to_list(pair[k])
                out.append(clash)

    if log:
        log("  - **total: {} clash(es)** for this test".format(len(out)))
    return out


# ---------------------------------------------------------------------------
# Set resolution: descriptor -> list of (doc, link_instance, elements)
# ---------------------------------------------------------------------------

def _resolve_buckets(doc, set_def, role_map, log=None):
    """Yield element buckets for a set descriptor.

    `source` accepts either a single string or a list of strings:

        "host"                              -> one bucket from the active doc
        "link:Architectural"                -> one bucket per arch link instance
        ["host", "link:Architectural"]      -> union: host bucket + arch buckets

    Multi-source is useful when an element category may live in either the
    host or a linked model (e.g. a project where someone drew walls in the
    MEP model alongside the linked architectural walls).
    """
    source = set_def.get('source')
    categories = set_def.get('categories') or []
    if not categories:
        if log:
            log("    (no categories declared - nothing to collect)")
        return

    if isinstance(source, list):
        sources = [s for s in source if s]
    elif source:
        sources = [source]
    else:
        sources = ['host']

    for src in sources:
        for bucket in _resolve_one_source(doc, src, categories, role_map, log=log):
            yield bucket


def _resolve_one_source(doc, source, categories, role_map, log=None):
    source = (source or 'host').strip()

    if source == 'host':
        if log:
            log("    host doc:")
        elements = linked.collect_doc_elements(doc, categories, log=log)
        if elements:
            yield {
                'source':         'host',
                'doc':            doc,
                'link_instance':  None,
                'elements':       elements,
            }
        return

    if not source.startswith('link:'):
        if log:
            log("    unknown source format `{}` - skipping".format(source))
        return

    role = source[len('link:'):]
    instances = list(linked.links_for_role(doc, role_map, role))
    if not instances:
        if log:
            log("    no linked models mapped to role `{}` (check Settings -> "
                "Linked model role mapping)".format(role))
        return
    for inst in instances:
        link_doc = inst.GetLinkDocument()
        if link_doc is None:
            if log:
                log("    link `{}` is unloaded - skipping".format(linked.link_title(inst)))
            continue
        if log:
            log("    link `{}`:".format(link_doc.Title))
        elements = linked.collect_doc_elements(link_doc, categories, log=log)
        if not elements:
            continue
        yield {
            'source':         source,
            'doc':            link_doc,
            'link_instance':  inst,
            'elements':       elements,
        }


# ---------------------------------------------------------------------------
# Detection dispatch
# ---------------------------------------------------------------------------

def _run_detection(doc, kind, tolerance_in,
                   set_a_elems, set_b_elems,
                   a_link, b_link, tess_cache=None, log=None, status=None):
    if kind == 'hard':
        return hard_mod.find_hard_clashes(
            doc, set_a_elems, set_b_elems, a_link, b_link,
            log=log, progress=status,
        )
    if kind == 'soft':
        return soft_mod.find_soft_clashes(
            doc, set_a_elems, set_b_elems, tolerance_in, a_link, b_link,
            tess_cache=tess_cache, log=log, progress=status,
        )
    return []


# ---------------------------------------------------------------------------
# Element ref construction
# ---------------------------------------------------------------------------

def _make_ref(elem, bucket):
    """Build an ElementRef dict for a clash, including the category id so
    the merge layer can auto-derive the assignee trade."""
    from clash_detect._compat import eid_int
    try:
        cat = elem.Category
        if cat is not None:
            cat_name = cat.Name
            cat_id = eid_int(cat.Id)
        else:
            cat_name = None
            cat_id = None
    except Exception:
        cat_name = None
        cat_id = None
    try:
        elem_name = elem.Name
    except Exception:
        elem_name = None
    link_doc_title = None
    if bucket.get('link_instance') is not None and bucket.get('doc') is not None:
        try:
            link_doc_title = bucket['doc'].Title
        except Exception:
            pass
    # Stable per-element key joining this clash to the exported glTF node and
    # back to the Revit element. Built via the shared clash_identity helper so it
    # is byte-identical to what the exporter stamps on each node (host = bare
    # UniqueId; linked = link-instance namespace + UniqueId). The link instance
    # in scope here is the one this element actually came from (bucket pairing),
    # so the namespace is correct.
    uid = None
    fk = None
    try:
        import clash_identity
        uid = elem.UniqueId
        inst = bucket.get('link_instance')
        link_ns = None if inst is None else clash_identity.link_ns_for_instance(inst)
        fk = clash_identity.fed_key(uid, link_ns)
    except Exception:
        pass
    ref = models.make_element_ref(
        source=bucket['source'],
        element_id=eid_int(elem.Id),
        category=cat_name,
        category_id=cat_id,
        name=elem_name,
        link_doc_title=link_doc_title,
        unique_id=uid,
        fed_key=fk,
    )
    # MEP enrichment for the importance engine (system, sizes, insulation,
    # level, discipline). All nullable; fingerprint-safe by design (the
    # fingerprint hashes only source + element_id + midpoint bucket), and
    # refs are wholesale-replaced at merge so these refresh every run.
    try:
        from clash_detect import enrich
        ref.update(enrich.mep_facts(elem, bucket, cat_id))
    except Exception:
        pass
    return ref


def _xyz_to_list(xyz):
    if xyz is None:
        return None
    try:
        return [float(xyz.X), float(xyz.Y), float(xyz.Z)]
    except AttributeError:
        try:
            return [float(c) for c in xyz]
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Trade filter (per-run scope reduction)
# ---------------------------------------------------------------------------

def _filter_buckets_by_trade(buckets, trade_filter, log=None):
    """Filter every bucket's elements down to those whose category-discipline
    is in `trade_filter`. Drop empty buckets afterward."""
    from clash_detect._compat import eid_int
    from clash_core import categories

    out = []
    for bucket in buckets:
        kept = []
        for elem in bucket['elements']:
            try:
                cat = elem.Category
                cat_id = eid_int(cat.Id) if cat is not None else None
            except Exception:
                cat_id = None
            if cat_id is None:
                continue
            try:
                disc = categories.discipline_for_category_id(cat_id)
            except Exception:
                disc = None
            if disc and disc in trade_filter:
                kept.append(elem)
        if kept:
            new_bucket = dict(bucket)
            new_bucket['elements'] = kept
            out.append(new_bucket)
            if log:
                log("    trade filter on `{}`: {} -> {} element(s)".format(
                    bucket['source'], len(bucket['elements']), len(kept)))
        else:
            if log:
                log("    trade filter on `{}`: {} -> 0 element(s) (bucket dropped)".format(
                    bucket['source'], len(bucket['elements'])))
    return out
