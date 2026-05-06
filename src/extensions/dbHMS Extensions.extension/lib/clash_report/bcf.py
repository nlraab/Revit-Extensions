# -*- coding: utf-8 -*-
"""BCF 2.1 file builder.

A BCF 2.1 file is a .zip with this layout:

    bcf.version            (xml; declares "2.1")
    project.bcfp           (xml; project name + project_id GUID)
    <topic-guid-1>/
        markup.bcf         (xml; the issue: title, status, priority, comments,
                            assigned_to, related ifc/element refs)
        viewpoint.bcfv     (xml; camera position + target + up, clipping
                            planes for the section box, components list)
        snapshot.png       (the rendered viewpoint thumbnail)
    <topic-guid-2>/
        ...

We write the XMLs by hand using xml.etree.ElementTree (no external deps)
since the schema is small and stable.

References:
    - buildingSMART BCF-XML 2.1 spec
    - https://github.com/buildingSMART/BCF-XML
"""

BCF_VERSION = "2.1"


def build_bcf_zip(project_meta, clashes, viewpoints_dir, out_path,
                  filter_predicate=None):
    """Write a BCF 2.1 zip to `out_path`.

    `project_meta` is the project.json dict (we use its display_name).
    `clashes` is the full list of Clash dicts.
    `viewpoints_dir` is the source folder for snapshot PNGs (we copy the
        referenced ones into the zip).
    `filter_predicate(clash) -> bool` lets the caller drop clashes (e.g.
        "only Mechanical, only Open").

    Returns the number of topics written.
    """
    raise NotImplementedError


def _write_markup_bcf(zip_handle, topic_guid, clash, project_meta):
    """Write the per-topic markup.bcf XML into the zip."""
    raise NotImplementedError


def _write_viewpoint_bcfv(zip_handle, topic_guid, viewpoint):
    """Write the per-topic viewpoint.bcfv XML into the zip."""
    raise NotImplementedError


def _convert_clash_status_to_bcf(status):
    """Map our ClashStatus values to BCF Topic.TopicStatus values.
    BCF default statuses: Open, In Progress, Closed, ReOpened."""
    raise NotImplementedError
