# -*- coding: utf-8 -*-
"""High-level "show me this clash" entry point.

Wraps the work of: resolving the clash's two element refs back to live
Revit elements (across host + linked docs), computing a combined bounding
box in host coordinates, finding/creating the persistent Clash Navigator
3D view, setting its section box, switching to it, and selecting the host
element(s) so the Properties panel shows them.

Every Revit-API call lives here and in the geometry / threed_view
submodules; Browser / Walkthrough / Reports scripts only call show_clash
(plus the future capture-viewpoint helpers) and never touch the API
directly. That keeps version-compat handling (ElementId, link transforms)
in one place.
"""

from clash_view import geometry, threed_view, highlights


def show_clash(uidoc, clash_dict, role_map, section_box_half_feet=5.0):
    """Open / refresh the Clash Navigator view, framed on this clash.

    Arguments:
        uidoc                 - the active UIDocument.
        clash_dict            - a single clash record (the in-memory shape
                                clashes.json stores: ref_a / ref_b / midpoint).
        role_map              - the project's link_role_map (from project.json).
                                Used to resolve "link:Architectural" /
                                "link:Structural" sources back to actual
                                RevitLinkInstances.
        section_box_half_feet - half-edge of the cube section box centered on
                                the clash midpoint. 5 ft default → 10 ft cube,
                                enough context to see what's around the clash
                                without losing focus on the clash itself.

    Returns (success: bool, message: str, view: View3D or None). The
    message is short, suitable for a status bar; success=False means the
    view didn't change (caller should surface the message to the user).

    Framing strategy:
        The section box is built around the clash MIDPOINT, not the union
        of the elements' bounding boxes. Element bbox union produces a
        section box the size of whichever element is largest — for a 40 ft
        duct that clashes a wall at one end, that means a 40-ft section box
        instead of a 10-ft window on the actual clash. The midpoint is the
        center of the bbox-overlap region computed at detection time
        (see clash_detect.hard / clash_detect.soft), so framing around it
        puts the actual clash at the center of the view.

        If the midpoint is missing or malformed (older clash data, or a
        detection edge case), we fall back to padded element bboxes so the
        view at least lands somewhere sensible.

    Element selection:
        Host-side elements are selected (so Properties shows them and the
        next click in Revit modifies them naturally). Linked elements are
        visible inside the section box but not selected — Revit's Selection
        API doesn't take ElementIds across documents, and Reference-based
        link selection is finicky enough that the v1 keeps it simple.
    """
    from Autodesk.Revit.DB import Transaction, ElementId
    from System.Collections.Generic import List

    doc = uidoc.Document

    # 1. Resolve element refs to (Element, RevitLinkInstance|None) tuples.
    #    Surface ref-resolution exceptions instead of swallowing them — the
    #    silent-None failure mode wasted a debugging cycle when the Revit
    #    2026 ElementId(int) overload bug first showed up.
    try:
        a_info = _resolve_ref(doc, clash_dict.get('ref_a'), role_map)
        b_info = _resolve_ref(doc, clash_dict.get('ref_b'), role_map)
    except Exception as ex:
        return (False,
                "Couldn't resolve clash element refs: {}".format(ex),
                None)
    resolved = [info for info in (a_info, b_info) if info is not None]
    if not resolved:
        ref_a = clash_dict.get('ref_a') or {}
        ref_b = clash_dict.get('ref_b') or {}
        return (False,
                "Couldn't find either clashing element (A id={}, B id={}). "
                "They may have been deleted, or the linked model is unloaded."
                .format(ref_a.get('element_id'), ref_b.get('element_id')),
                None)

    # 2. Build the section box centered on the clash midpoint.
    #    Midpoint-centered framing keeps the view tight on the actual clash
    #    region rather than the (often huge) union of element bboxes — see
    #    the docstring's "Framing strategy" note for why.
    midpoint = clash_dict.get('midpoint')
    framed = geometry.box_around_point(midpoint,
                                       half_size=section_box_half_feet)
    if framed is None:
        # Midpoint missing/malformed (older clash data, or a detection edge
        # case). Fall back to a small padded union of element bboxes so the
        # view at least lands somewhere sensible instead of refusing to open.
        boxes = []
        for elem, link_inst in resolved:
            b = geometry.element_world_box(elem, link_inst)
            if b is not None:
                boxes.append(b)
        union = geometry.union_boxes(boxes)
        if union is None:
            return (False,
                    "No clash midpoint stored and the elements have no "
                    "bounding boxes — can't position the view.",
                    None)
        framed = geometry.pad_box(union, pad_feet=2.0)

    # 3. Build per-side host ElementId lists. The "per side" split is what
    #    lets the highlights submodule color element A red and element B
    #    blue — pooling them here would lose that distinction. Linked
    #    elements aren't included for either selection or highlighting
    #    (Revit's host-element APIs don't accept link refs cleanly).
    a_host_ids = _host_id_list(a_info)
    b_host_ids = _host_id_list(b_info)
    pooled_host_ids = a_host_ids + b_host_ids

    # 4. Find or create the navigator view + apply section box + highlights,
    #    all in a single transaction so it's one undo step for the user.
    txn = Transaction(doc, "dbHMS Show clash in 3D")
    try:
        txn.Start()
        view = threed_view.get_or_create_navigator_view(doc)
        if view is None:
            txn.RollBack()
            return (False,
                    "Couldn't create a 3D view (no 3D ViewFamilyType "
                    "available in this project).",
                    None)
        threed_view.set_section_box(view, framed)
        # Force a color-rendering DisplayStyle if the view is currently
        # in Hidden Line / Wireframe — those styles don't render the
        # surface color overrides we're about to apply, which is why
        # "colors don't work on the first click" used to surprise users.
        # Idempotent and respects the user's choice if they've picked
        # any other shaded/realistic style.
        threed_view.ensure_color_friendly_display_style(view)
        # Color element A red, element B blue, in the navigator view only.
        # Clears the previous clash's highlights as part of the same call.
        highlights.apply(view, a_host_ids, b_host_ids)
        txn.Commit()
    except Exception as ex:
        try:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
        except Exception:
            pass
        return (False,
                "Failed to update the clash view: {}".format(ex),
                None)

    # 5. Switch the active view to the navigator (UI op, no transaction).
    try:
        uidoc.ActiveView = view
    except Exception as ex:
        # The view exists and is set up; user can switch to it manually.
        return (True,
                "Clash view updated, but couldn't switch to it ({}). "
                "Open '{}' from the Project Browser."
                .format(ex, threed_view.NAVIGATOR_VIEW_NAME),
                view)

    # 6. Select the pooled host elements so Properties shows them.
    if pooled_host_ids:
        try:
            uidoc.Selection.SetElementIds(List[ElementId](pooled_host_ids))
        except Exception:
            pass  # selection failure shouldn't block the navigation

    # 7. Zoom the navigator UI view tight onto the section box.
    #    Two layers:
    #
    #    (a) Synchronous attempt. On SUBSEQUENT clicks (navigator view
    #        already active and rendered) this works immediately for a
    #        snappy UX.
    #
    #    (b) ViewActivated event handler. On the FIRST click from a
    #        different starting view, Revit's view-switch happens AFTER
    #        our synchronous zoom — which means we either don't find the
    #        UIView yet (it doesn't exist) or our zoom gets overridden
    #        by Revit's auto-fit during activation. ViewActivated fires
    #        precisely when the navigator view becomes active, at which
    #        point the UIView exists and the auto-fit has already run.
    #        Our zoom then lands cleanly. The handler is one-shot — fires
    #        once, unsubscribes itself.
    #
    #    Why ViewActivated and not Idling: Idling fires constantly during
    #    normal Revit use, requires polling to detect the activation
    #    moment, and (during heavy first-load) can run at unpredictable
    #    rates. Worst case, we'd leave a polling handler subscribed for
    #    seconds. ViewActivated is event-driven: precise signal, single
    #    fire, no polling.
    #
    #    ZoomAndCenterRectangle vs ZoomToFit: ZoomToFit zooms to the
    #    view's CROP region, which for a 3D view is project extents —
    #    not the section box. ZoomAndCenterRectangle takes explicit XYZ
    #    corners and frames exactly that rectangle.
    _zoom_to_rect(uidoc, view, framed)
    _schedule_view_activated_zoom(uidoc.Application, view.Id,
                                  framed.Min, framed.Max)

    # Build a precise status message that names which of the two refs we
    # resolved, so the user understands the result if one side was missing.
    parts = []
    if a_info is None:
        parts.append("element A not found")
    if b_info is None:
        parts.append("element B not found")
    msg = ("Showing clash in 3D."
           if not parts
           else "Showing clash in 3D ({}).".format(", ".join(parts)))
    return True, msg, view


def clear_isolation(uidoc):
    """Restore the active view from temporary hide/isolate.

    show_clash currently uses section-boxing rather than isolation so
    callers don't *need* this for normal use, but it's exposed as a clean
    way to recover if a future caller opts into isolation and needs to
    bail out.
    """
    from Autodesk.Revit.DB import Transaction, TemporaryViewMode
    if uidoc is None:
        return
    view = uidoc.ActiveView
    if view is None:
        return
    doc = uidoc.Document
    txn = Transaction(doc, "dbHMS Clear clash isolation")
    try:
        txn.Start()
        view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        txn.Commit()
    except Exception:
        try:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
        except Exception:
            pass


def clear_highlights(uidoc):
    """Clear any element color overrides from the Clash Navigator view.

    Opens a transaction, clears, commits. NOT called from the Browser's
    close handler — the highlights persist across Browser sessions and
    are cleared automatically by the next show_clash call (see
    highlights._clear_last). Keeping the close handler free of any
    work is what makes Browser close as fast as the other tools.

    Available as an explicit cleanup entry point for any future caller
    (e.g. an "Unhighlight" button) that wants to wipe the navigator
    view's overrides on demand. No-op if the navigator view doesn't
    exist or the transaction fails.
    """
    from Autodesk.Revit.DB import Transaction
    if uidoc is None:
        return
    doc = uidoc.Document
    view = threed_view.find_navigator_view(doc)
    if view is None:
        return
    txn = Transaction(doc, "dbHMS Clear clash highlights")
    try:
        txn.Start()
        highlights.clear(view)
        txn.Commit()
    except Exception:
        try:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
        except Exception:
            pass




def _host_id_list(resolved_info):
    """Pull the host ElementId out of a (Element, link_instance) tuple.

    Returns [] when the ref didn't resolve, or when the element lives in
    a linked document (linked elements aren't selected or color-overridden
    — Revit's host-element APIs don't accept link refs cleanly).
    """
    if not resolved_info:
        return []
    elem, link_inst = resolved_info
    if link_inst is not None or elem is None:
        return []
    try:
        return [elem.Id]
    except Exception:
        return []


def _zoom_to_rect(uidoc, view, bbox):
    """Find the UIView for `view` and zoom-and-center it on `bbox`.

    Returns True on success, False if no matching UIView exists or the
    zoom call raises. Caller doesn't need to retry — the ViewActivated-
    deferred zoom in show_clash handles the first-activation case
    separately, when this synchronous attempt is expected to fail
    because the navigator view's UIView doesn't exist yet.
    """
    try:
        for uv in uidoc.GetOpenUIViews():
            if uv.ViewId == view.Id:
                uv.ZoomAndCenterRectangle(bbox.Min, bbox.Max)
                return True
    except Exception:
        return False
    return False


# Module-level handle for the currently-pending ViewActivated zoom
# handler so we can cancel it if a new show_clash supersedes it (user
# clicks another clash before the first one's handler has fired). Cleaner
# than letting multiple handlers stack up.
_pending_view_activation = {'uiapp': None, 'handler': None}


def _cancel_pending_view_activation():
    pending = _pending_view_activation
    uiapp = pending['uiapp']
    handler = pending['handler']
    if uiapp is None or handler is None:
        return
    try:
        uiapp.ViewActivated -= handler
    except Exception:
        pass
    pending['uiapp'] = None
    pending['handler'] = None


def _schedule_view_activated_zoom(uiapp, view_id, bbox_min, bbox_max):
    """Subscribe a one-shot ViewActivated handler that, when the navigator
    view becomes active, defers the zoom by one Idling tick so Revit's
    auto-fit runs first and our zoom lands LAST (and therefore wins).

    Two-stage chain:
      stage 1 (ViewActivated) — fires the moment the navigator view
        becomes active. We unsubscribe from ViewActivated immediately
        and subscribe to Idling for a single tick.
      stage 2 (Idling, one shot) — fires on Revit's next idle moment,
        which is AFTER the view-switch + auto-fit sequence has fully
        settled. We zoom, then unsubscribe immediately.

    Why we need stage 2: ViewActivated fires DURING the activation
    sequence — at that moment, Revit's auto-fit has not yet completed,
    so a synchronous zoom inside the ViewActivated handler gets
    overwritten by Revit's auto-fit a few ms later. Idling fires only
    when Revit has nothing else to do, which means the activation
    sequence is finished. A single, immediate Idling-shot zoom has
    none of the popup risks the earlier polling-burst pattern had —
    no transactions, no repeated firing, just one synchronous
    UI-thread call to ZoomAndCenterRectangle and an unsubscribe.

    Cancels any previously-scheduled handler so quick consecutive
    clicks don't leave multiple chains stacked up.
    """
    if uiapp is None or view_id is None or bbox_min is None or bbox_max is None:
        return

    from clash_detect._compat import eid_int

    _cancel_pending_view_activation()

    target_view_eid = eid_int(view_id)

    def _zoom_now():
        try:
            uidoc = uiapp.ActiveUIDocument
            if uidoc is None:
                return
            for uv in uidoc.GetOpenUIViews():
                try:
                    if eid_int(uv.ViewId) != target_view_eid:
                        continue
                except Exception:
                    continue
                try:
                    uv.ZoomAndCenterRectangle(bbox_min, bbox_max)
                except Exception:
                    pass
                return
        except Exception:
            pass

    def idling_handler(sender, args):
        # One-shot — unsubscribe FIRST so any zoom exception can't
        # leave us re-firing on every Idling tick forever. No
        # transactions, no UI dialogs in here — just the zoom call.
        try:
            uiapp.Idling -= idling_handler
        except Exception:
            pass
        _zoom_now()

    def view_activated_handler(sender, args):
        # Unsubscribe immediately. Any future activation events get
        # ignored regardless of what happens below.
        try:
            uiapp.ViewActivated -= view_activated_handler
        except Exception:
            pass
        if _pending_view_activation.get('handler') is view_activated_handler:
            _pending_view_activation['uiapp'] = None
            _pending_view_activation['handler'] = None

        try:
            current = args.CurrentActiveView
            if current is None:
                return
            try:
                if eid_int(current.Id) != target_view_eid:
                    # Some other view became active — user navigated
                    # away while we were waiting. Don't try to zoom.
                    return
            except Exception:
                return
            # Our view! Stage 2: defer the zoom by one Idling tick so
            # Revit's auto-fit (which happens AFTER this event) runs
            # before our zoom does, so we land last and win.
            try:
                uiapp.Idling += idling_handler
            except Exception:
                pass
        except Exception:
            pass

    # Belt-and-suspenders fallback: poll Idling up to N times, zooming
    # on each tick. The ViewActivated chain above is the "right" path
    # but on FIRST click of a fresh navigator view it can miss for
    # one of two reasons:
    #   (a) ViewActivated fires synchronously inside the
    #       `uidoc.ActiveView = view` setter — BEFORE our handler is
    #       subscribed — so we miss it entirely.
    #   (b) The single Idling tick after ViewActivated fires before
    #       Revit's auto-fit for the freshly-activated view has
    #       completed; our zoom lands, then auto-fit overwrites it.
    #
    # Multi-tick polling defends against both. Each tick we just call
    # ZoomAndCenterRectangle again. Once Revit settles (auto-fit done,
    # UIView present), the zoom takes; subsequent ticks are visual
    # no-ops (already at the right zoom). Capped so we don't poll
    # forever if something genuinely goes wrong.
    #
    # Earlier "polling-burst" patterns we'd avoided: those mixed
    # transactions and dialog popups into Idling handlers, which
    # caused real Revit-side issues. Pure UIView.ZoomAndCenterRectangle
    # is a no-state UI operation — repeating it is safe.
    fallback_attempts = [0]
    MAX_ATTEMPTS = 8

    def fallback_idling(sender, args):
        fallback_attempts[0] += 1
        _zoom_now()
        if fallback_attempts[0] >= MAX_ATTEMPTS:
            try:
                uiapp.Idling -= fallback_idling
            except Exception:
                pass

    try:
        uiapp.Idling += fallback_idling
    except Exception:
        pass

    try:
        uiapp.ViewActivated += view_activated_handler
        _pending_view_activation['uiapp'] = uiapp
        _pending_view_activation['handler'] = view_activated_handler
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal: clash-ref -> live-element resolution (across host and links)
# ---------------------------------------------------------------------------

def _resolve_ref(doc, ref_dict, role_map):
    """Resolve a clash element-ref dict to (Element, RevitLinkInstance | None).

    Returns None if the element can't be found (deleted, link unloaded,
    role_map missing the link the clash was originally detected against,
    etc.). Callers handle None by either skipping that side or falling back
    to the saved midpoint.

    For linked refs we honor `link_doc_title` when present — a project may
    have multiple link instances of the same role (two arch links, etc.)
    and the recorded title pins us to the specific one we detected against.

    ElementId construction goes through `_compat.make_eid` rather than
    `ElementId(int)` directly: on Revit 2026 the int constructor silently
    routes through the BuiltInCategory overload and produces a wrong
    ElementId, which `GetElement` then "fails" to resolve.
    """
    from clash_detect import linked
    from clash_detect._compat import make_eid

    if not ref_dict:
        return None
    eid_value = ref_dict.get('element_id')
    if not eid_value:
        return None
    elem_id = make_eid(eid_value)
    if elem_id is None:
        return None

    source = ref_dict.get('source') or 'host'
    if source == 'host':
        elem = doc.GetElement(elem_id)
        return (elem, None) if elem is not None else None

    if not source.startswith('link:'):
        return None
    role = source[len('link:'):]

    # Prefer a link instance whose linked .rvt title matches what was
    # recorded at detection time. Fall back to "any link in this role" if
    # the title isn't recorded or no longer matches (e.g. file renamed).
    target_title = ref_dict.get('link_doc_title')
    candidates = list(linked.links_for_role(doc, role_map, role))
    if target_title:
        titled = [inst for inst in candidates
                  if linked.link_title(inst) == target_title]
        if titled:
            candidates = titled

    for inst in candidates:
        link_doc = inst.GetLinkDocument()
        if link_doc is None:
            continue
        elem = link_doc.GetElement(elem_id)
        if elem is not None:
            return (elem, inst)
    return None
