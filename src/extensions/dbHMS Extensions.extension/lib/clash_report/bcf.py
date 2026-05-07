# -*- coding: utf-8 -*-
"""BCF 2.1 file builder.

A BCF 2.1 file is a .zip with this layout:

    bcf.version            (xml; declares "2.1")
    project.bcfp           (xml; project name + project_id GUID)
    <topic-guid-1>/
        markup.bcf         (xml; topic title, status, comments,
                            assigned trade, viewpoint refs)
        viewpoint.bcfv     (xml; orthogonal camera + 6 clipping planes
                            for the section box)
        snapshot.png       (the rendered viewpoint thumbnail)
    <topic-guid-2>/
        ...

We write the XMLs by hand using xml.etree.ElementTree (no external deps)
since the schema is small and stable.

Coordinate units: BCF stores positions in METERS. Revit's internal
coordinate system is FEET. All XYZ values written to the BCF get
multiplied by FEET_TO_METERS first.

Pure Python — no Revit / WPF imports — so this module runs in the
CPython test suite. Callers handle the Revit-side viewpoint capture
upstream (the PNGs are already on disk and the camera/section-box
state is already serialized into the clash dicts by clash_view.viewpoint).

References:
    - buildingSMART BCF-XML 2.1 spec
    - https://github.com/buildingSMART/BCF-XML
"""

import codecs
import os
import shutil
import tempfile
import uuid as _uuid
import zipfile
from xml.etree import ElementTree as ET


BCF_VERSION = "2.1"
FEET_TO_METERS = 0.3048


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_bcf_zip(project_meta, clashes, viewpoints_dir, out_path,
                  filter_predicate=None, project_name=None):
    """Write a BCF 2.1 zip to `out_path`.

    Args:
        project_meta      - project.json dict (we use display_name + project_hash).
                            Pass {} if not available; defaults are used.
        clashes           - list of clash dicts (the in-memory shape clashes.json
                            stores).
        viewpoints_dir    - source folder where snapshot PNGs live (one per
                            clash, named <clash-id>.png — see
                            persistence.viewpoint_image_path). May be None or
                            non-existent; clashes without an existing PNG just
                            skip the snapshot, the topic still exports.
        out_path          - destination .bcf or .bcfzip file path.
        filter_predicate  - optional callable `predicate(clash) -> bool`. Only
                            clashes that return True are exported. Pass None
                            to export everything.
        project_name      - human-readable project name for project.bcfp. If
                            None, falls back to project_meta.display_name,
                            then to "dbHMS Clash Export".

    Returns the count of topics written.

    Robustness: writes to a temp file first, then atomic-renames to
    out_path on success. Partial failures don't leave a half-written
    zip the user might mistake for valid.
    """
    if not out_path:
        raise ValueError("out_path is required")
    project_meta = project_meta or {}
    clashes = clashes or []

    # Filter pass.
    if filter_predicate is not None:
        clashes = [c for c in clashes if c and filter_predicate(c)]
    else:
        clashes = [c for c in clashes if c]

    name = (project_name
            or project_meta.get('display_name')
            or "dbHMS Clash Export")
    project_guid = (project_meta.get('project_hash')
                    or _uuid.uuid4().hex)

    # Ensure the destination directory exists.
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # Atomic write via temp file in the same directory (so the rename
    # is atomic on the same filesystem).
    fd, tmp_path = tempfile.mkstemp(
        suffix='.bcfzip.tmp', dir=out_dir or None)
    os.close(fd)

    written = 0
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('bcf.version', _build_version_xml())
            zf.writestr('project.bcfp', _build_project_xml(name, project_guid))
            for clash in clashes:
                if _write_topic_to_zip(zf, clash, viewpoints_dir):
                    written += 1
        # Atomic rename onto out_path.
        if os.path.exists(out_path):
            os.remove(out_path)
        os.rename(tmp_path, out_path)
    except Exception:
        # Clean up the temp file if anything failed.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

    return written


# ---------------------------------------------------------------------------
# Per-topic write
# ---------------------------------------------------------------------------

def _write_topic_to_zip(zf, clash, viewpoints_dir):
    """Write one topic's folder (markup + viewpoint + snapshot) into the zip.

    Returns True on success, False if the clash is missing required
    fields (no id) and was skipped.
    """
    clash_id = clash.get('id')
    if not clash_id:
        return False
    topic_guid = _ensure_guid(clash_id)
    folder = topic_guid + '/'

    # Pick the first viewpoint dict (single-viewpoint-per-clash design).
    viewpoints = clash.get('viewpoints') or []
    viewpoint = viewpoints[0] if viewpoints else None
    viewpoint_guid = _ensure_guid(
        viewpoint.get('id') if viewpoint else _uuid.uuid4().hex)

    # markup.bcf — always present.
    markup_xml = _build_markup_xml(clash, topic_guid, viewpoint_guid,
                                   has_viewpoint=viewpoint is not None)
    zf.writestr(folder + 'markup.bcf', markup_xml)

    if viewpoint is not None:
        # viewpoint.bcfv
        viewpoint_xml = _build_viewpoint_xml(viewpoint, viewpoint_guid)
        zf.writestr(folder + 'viewpoint.bcfv', viewpoint_xml)

        # snapshot.png (skipped silently if the file isn't on disk)
        if viewpoints_dir:
            png_path = os.path.join(viewpoints_dir, '{}.png'.format(clash_id))
            if os.path.isfile(png_path):
                zf.write(png_path, folder + 'snapshot.png')

    return True


# ---------------------------------------------------------------------------
# XML builders
# ---------------------------------------------------------------------------

def _build_version_xml():
    """bcf.version at the zip root."""
    root = ET.Element('Version', {'VersionId': BCF_VERSION})
    detailed = ET.SubElement(root, 'DetailedVersion')
    detailed.text = BCF_VERSION
    return _serialize(root)


def _build_project_xml(name, project_guid):
    """project.bcfp at the zip root — wraps the project name + GUID."""
    root = ET.Element('ProjectExtension')
    project = ET.SubElement(root, 'Project',
                            {'ProjectId': _ensure_guid(project_guid)})
    name_el = ET.SubElement(project, 'Name')
    name_el.text = name or 'dbHMS Clash Export'
    # ExtensionSchema is required by the BCF 2.1 spec, even if unused.
    ET.SubElement(root, 'ExtensionSchema').text = 'extensions.xsd'
    return _serialize(root)


def _build_markup_xml(clash, topic_guid, viewpoint_guid, has_viewpoint):
    """markup.bcf for one topic.

    Schema reference: BCF 2.1 markup.xsd. Required Topic children:
    Title, CreationDate, CreationAuthor (in that order). Optional:
    everything else.
    """
    root = ET.Element('Markup')

    # Topic
    topic_attrs = {
        'Guid': topic_guid,
        'TopicType': 'Clash',
        'TopicStatus': _bcf_status(clash.get('status')),
    }
    topic = ET.SubElement(root, 'Topic', topic_attrs)
    _add_text_element(topic, 'Title', _topic_title(clash))
    seq = clash.get('seq')
    if seq is not None:
        _add_text_element(topic, 'Index', str(seq))
    # Labels — comma-separated list. Use the test name + kind as labels.
    labels = []
    test_name = clash.get('test_name')  # not present in dict; let caller add
    if not test_name:
        test_name = clash.get('test_id') or ''
    if test_name:
        labels.append(test_name)
    kind = (clash.get('kind') or '').lower()
    if kind:
        labels.append(kind)
    for label in labels:
        _add_text_element(topic, 'Labels', label)

    creation_date, creation_author = _earliest_history(clash)
    _add_text_element(topic, 'CreationDate', creation_date)
    _add_text_element(topic, 'CreationAuthor', creation_author)

    modified_date, modified_author = _latest_history(clash)
    if modified_date and modified_date != creation_date:
        _add_text_element(topic, 'ModifiedDate', modified_date)
        _add_text_element(topic, 'ModifiedAuthor', modified_author)

    assignee = clash.get('assignee')
    if assignee:
        _add_text_element(topic, 'AssignedTo', assignee)

    description = _build_description(clash)
    if description:
        _add_text_element(topic, 'Description', description)

    # Comments (sibling of Topic, not nested).
    for comment_dict in (clash.get('comments') or []):
        _add_comment_element(root, comment_dict, viewpoint_guid if has_viewpoint else None)

    # Viewpoint reference (sibling of Topic, not nested).
    if has_viewpoint:
        viewpoints_el = ET.SubElement(root, 'Viewpoints',
                                      {'Guid': viewpoint_guid})
        _add_text_element(viewpoints_el, 'Viewpoint', 'viewpoint.bcfv')
        _add_text_element(viewpoints_el, 'Snapshot', 'snapshot.png')

    return _serialize(root)


def _build_viewpoint_xml(viewpoint, viewpoint_guid):
    """viewpoint.bcfv with orthogonal camera + 6 clipping planes for the
    section box.

    All XYZ values are converted from feet to meters before writing —
    BCF stores coordinates in meters by spec convention.
    """
    root = ET.Element('VisualizationInfo', {'Guid': viewpoint_guid})

    # Clipping planes from the section box (one per face).
    section_box = viewpoint.get('section_box')
    if section_box and 'min' in section_box and 'max' in section_box:
        planes = ET.SubElement(root, 'ClippingPlanes')
        for location, direction in _section_box_to_clipping_planes(
                section_box['min'], section_box['max']):
            plane = ET.SubElement(planes, 'ClippingPlane')
            _add_xyz_element(plane, 'Location', location)
            _add_xyz_element(plane, 'Direction', direction)

    # Orthogonal camera. Our navigator views are isometric/orthographic,
    # so OrthogonalCamera is the right BCF primitive.
    camera = viewpoint.get('camera') or {}
    position = camera.get('position')
    target = camera.get('target')
    up = camera.get('up')
    if position and target and up:
        cam = ET.SubElement(root, 'OrthogonalCamera')
        _add_xyz_element(cam, 'CameraViewPoint',
                         [c * FEET_TO_METERS for c in position])
        # BCF wants direction (camera looking direction), not target point.
        direction = _direction_from_position_target(position, target)
        _add_xyz_element(cam, 'CameraDirection', direction)  # unitless vector
        _add_xyz_element(cam, 'CameraUpVector', up)          # unitless vector
        # ViewToWorldScale in meters — height of the view's world region.
        # Use the section box's largest extent as a reasonable default.
        scale = _view_to_world_scale(section_box)
        ET.SubElement(cam, 'ViewToWorldScale').text = '{0:.6f}'.format(scale)

    return _serialize(root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize(root):
    """Render an ElementTree element as a UTF-8 XML string with a declaration.

    ElementTree.tostring with xml_declaration=True is Python 3.8+;
    pre-pending the declaration manually is the cross-version path.
    """
    body = ET.tostring(root, encoding='utf-8')
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def _add_text_element(parent, tag, text):
    el = ET.SubElement(parent, tag)
    el.text = u'' if text is None else _safe_str(text)
    return el


def _add_xyz_element(parent, tag, xyz):
    """Add a child with X / Y / Z grandchildren (BCF's standard 3D coord shape)."""
    el = ET.SubElement(parent, tag)
    _add_text_element(el, 'X', '{0:.6f}'.format(float(xyz[0])))
    _add_text_element(el, 'Y', '{0:.6f}'.format(float(xyz[1])))
    _add_text_element(el, 'Z', '{0:.6f}'.format(float(xyz[2])))
    return el


def _add_comment_element(root, comment_dict, viewpoint_guid):
    """Append a top-level Comment element to the markup root."""
    comment_guid = _ensure_guid(comment_dict.get('id') or _uuid.uuid4().hex)
    el = ET.SubElement(root, 'Comment', {'Guid': comment_guid})
    _add_text_element(el, 'Date', comment_dict.get('at') or '')
    _add_text_element(el, 'Author', comment_dict.get('author') or 'unknown')
    _add_text_element(el, 'Comment', comment_dict.get('body') or '')
    if viewpoint_guid:
        ET.SubElement(el, 'Viewpoint', {'Guid': viewpoint_guid})


def _topic_title(clash):
    seq = clash.get('seq')
    a_name = ((clash.get('ref_a') or {}).get('name')
              or (clash.get('ref_a') or {}).get('category')
              or 'Element A')
    b_name = ((clash.get('ref_b') or {}).get('name')
              or (clash.get('ref_b') or {}).get('category')
              or 'Element B')
    base = u"{} vs {}".format(a_name, b_name)
    if seq is not None:
        return u"Clash #{}: {}".format(seq, base)
    return base


def _build_description(clash):
    """Multi-line description with element details + comment count."""
    lines = []
    a = clash.get('ref_a') or {}
    b = clash.get('ref_b') or {}
    lines.append(u"Element A: {} (ID {}{})".format(
        a.get('name') or a.get('category') or '?',
        a.get('element_id') or '?',
        u', ' + a['source'] if a.get('source') and a.get('source') != 'host' else u''))
    lines.append(u"Element B: {} (ID {}{})".format(
        b.get('name') or b.get('category') or '?',
        b.get('element_id') or '?',
        u', ' + b['source'] if b.get('source') and b.get('source') != 'host' else u''))
    kind = clash.get('kind')
    if kind:
        lines.append(u"Kind: {}".format(kind))
    return u'\n'.join(lines)


def _bcf_status(our_status):
    """Map our ClashStatus values to BCF Topic.TopicStatus values.

    BCF default statuses: Open, In Progress, Closed, ReOpened. Receiving
    tools sometimes have extra schema-defined statuses, but these four
    are universally accepted.
    """
    if not our_status:
        return 'Open'
    mapping = {
        'Open':     'Open',
        'Reviewed': 'In Progress',
        'Approved': 'In Progress',
        'Resolved': 'Closed',
    }
    return mapping.get(our_status, 'Open')


def _earliest_history(clash):
    """Return (date, author) of the earliest history entry, or sensible
    fallbacks if there's none."""
    history = clash.get('history') or []
    if history:
        first = history[0]
        return first.get('at') or '', first.get('author') or 'unknown'
    return clash.get('first_seen_run') or '', 'unknown'


def _latest_history(clash):
    """Return (date, author) of the latest history entry."""
    history = clash.get('history') or []
    if history:
        last = history[-1]
        return last.get('at') or '', last.get('author') or 'unknown'
    return clash.get('last_seen_run') or '', 'unknown'


def _direction_from_position_target(position, target):
    """Compute a UNIT direction vector from camera position to target.

    Returns a 3-tuple of floats. Output is unitless (it's a direction).
    """
    dx = target[0] - position[0]
    dy = target[1] - position[1]
    dz = target[2] - position[2]
    mag = (dx * dx + dy * dy + dz * dz) ** 0.5
    if mag < 1e-9:
        # Zero vector — fall back to looking down -Z.
        return (0.0, 0.0, -1.0)
    return (dx / mag, dy / mag, dz / mag)


def _section_box_to_clipping_planes(min_xyz, max_xyz):
    """Convert a section box (min/max XYZ in feet) to 6 clipping planes
    in METERS.

    Each plane is (location, direction) where direction points INTO the
    region we want to keep visible — BCF's convention. So the +X face's
    plane has normal (-1, 0, 0) (points back into the box), the -X face
    has normal (+1, 0, 0), etc.

    Yields (location_xyz_meters, direction_xyz_unit) tuples.
    """
    min_x = min_xyz[0] * FEET_TO_METERS
    min_y = min_xyz[1] * FEET_TO_METERS
    min_z = min_xyz[2] * FEET_TO_METERS
    max_x = max_xyz[0] * FEET_TO_METERS
    max_y = max_xyz[1] * FEET_TO_METERS
    max_z = max_xyz[2] * FEET_TO_METERS
    yield ((min_x, min_y, min_z), (1.0, 0.0, 0.0))   # left face   (+X inward)
    yield ((max_x, min_y, min_z), (-1.0, 0.0, 0.0))  # right face
    yield ((min_x, min_y, min_z), (0.0, 1.0, 0.0))   # front face
    yield ((min_x, max_y, min_z), (0.0, -1.0, 0.0))  # back face
    yield ((min_x, min_y, min_z), (0.0, 0.0, 1.0))   # bottom face
    yield ((min_x, min_y, max_z), (0.0, 0.0, -1.0))  # top face


def _view_to_world_scale(section_box):
    """Compute a sensible OrthogonalCamera ViewToWorldScale (in meters)
    from the section box dimensions.

    Use the largest dimension (in feet) converted to meters, with a
    small margin so the BCF receiver's view shows a bit of context
    around the section-boxed region.
    """
    if not section_box:
        return 5.0  # safe fallback (≈16 ft visible)
    try:
        mn = section_box['min']
        mx = section_box['max']
        dx = abs(mx[0] - mn[0])
        dy = abs(mx[1] - mn[1])
        dz = abs(mx[2] - mn[2])
        largest_feet = max(dx, dy, dz)
        return max(1.0, largest_feet * FEET_TO_METERS * 1.2)
    except Exception:
        return 5.0


def _ensure_guid(value):
    """Coerce `value` to a string usable as a BCF Guid attribute.

    BCF Guids should be UUID-format strings, but BCF receivers in
    practice accept any non-empty string identifier (we tested with
    Solibri, BIMcollab, Newforma). We pass through whatever the caller
    provides — our clash IDs are uuid4 hex strings, our project hashes
    are SHA-1 prefixes, both reasonable enough — and only synthesize a
    fresh uuid4 when the input is empty or None.
    """
    if not value:
        return str(_uuid.uuid4())
    return str(value)


def _safe_str(value):
    """Coerce any value to a unicode-safe string for XML text."""
    try:
        if isinstance(value, bytes):
            return value.decode('utf-8', 'replace')
    except Exception:
        pass
    try:
        return u'{}'.format(value)
    except Exception:
        return u''
