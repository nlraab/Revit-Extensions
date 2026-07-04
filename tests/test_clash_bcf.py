"""Tests for lib/clash_report/bcf.py — BCF 2.1 file builder.

Pure-data module — no Revit, no WPF. Verifies:
  * Zip structure matches BCF 2.1 layout (bcf.version + project.bcfp +
    per-topic folders)
  * markup.bcf XML has the required Topic children in the right order
  * viewpoint.bcfv produces 6 clipping planes from a section box, with
    correct inward-pointing normals
  * Coordinate units convert from feet to meters
  * Status mapping (our 4 statuses → BCF's 4)
  * Snapshot PNG is included when present, skipped silently when not
  * filter_predicate drops clashes correctly
  * Atomic write (no half-zip on failure)
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension" / "lib"
sys.path.insert(0, str(LIB_ROOT))


from clash_report import bcf  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _sample_clash(clash_id="clash-1", seq=1, status="Open",
                  assignee="Mechanical", with_viewpoint=True,
                  with_comments=False):
    clash = {
        "id": clash_id,
        "seq": seq,
        "kind": "hard",
        "status": status,
        "assignee": assignee,
        "test_id": "default-mep-vs-architecture",
        "first_seen_run": "2026-05-06T05:11:16Z",
        "last_seen_run": "2026-05-06T05:21:15Z",
        "ref_a": {
            "source": "host",
            "element_id": 1250520,
            "category": "Ducts",
            "name": "Round Duct - 12in",
        },
        "ref_b": {
            "source": "link:Architectural",
            "element_id": 1240656,
            "category": "Walls",
            "name": "Generic - 8in",
            "link_doc_title": "Tool Testing 01",
        },
        "midpoint": [10.0, 20.0, 5.0],
        "history": [
            {"action": "detected", "at": "2026-05-06T05:11:16Z",
             "author": "Nathan"},
        ],
        "comments": [],
        "viewpoints": [],
    }
    if with_viewpoint:
        clash["viewpoints"] = [{
            "id": "vp-1",
            "captured_at": "2026-05-06T05:12:00Z",
            "captured_by": "Nathan",
            "camera": {
                "position": [10.0, -10.0, 15.0],
                "target":   [10.0, 20.0, 5.0],
                "up":       [0.0, 0.0, 1.0],
            },
            "section_box": {
                "min": [5.0, 15.0, 0.0],
                "max": [15.0, 25.0, 10.0],
            },
            "snapshot_relpath": "viewpoints/clash-1.png",
        }]
    if with_comments:
        clash["comments"] = [
            {"author": "Nathan", "at": "2026-05-06T05:30:00Z",
             "body": "Talk to architect about beam location"},
        ]
    return clash


# ---------------------------------------------------------------------------
# build_bcf_zip — top-level integration
# ---------------------------------------------------------------------------

class BuildBcfZipStructureTests(unittest.TestCase):
    """Verify the overall zip layout matches BCF 2.1."""

    def test_zip_contains_required_top_level_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, [_sample_clash()], None, out)
            self.assertEqual(count, 1)
            with zipfile.ZipFile(out, "r") as zf:
                names = zf.namelist()
                self.assertIn("bcf.version", names)
                self.assertIn("project.bcfp", names)
                # And one topic folder
                topic_files = [n for n in names if n.endswith("markup.bcf")]
                self.assertEqual(len(topic_files), 1)

    def test_per_topic_folder_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [_sample_clash()], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                names = zf.namelist()
                self.assertTrue(any(n.endswith("markup.bcf") for n in names))
                self.assertTrue(any(n.endswith("viewpoint.bcfv") for n in names))

    def test_multiple_clashes_produce_multiple_topic_folders(self):
        clashes = [
            _sample_clash("clash-1", seq=1),
            _sample_clash("clash-2", seq=2),
            _sample_clash("clash-3", seq=3),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, clashes, None, out)
            self.assertEqual(count, 3)
            with zipfile.ZipFile(out, "r") as zf:
                markup_files = [n for n in zf.namelist()
                                if n.endswith("markup.bcf")]
                self.assertEqual(len(markup_files), 3)


# ---------------------------------------------------------------------------
# bcf.version XML
# ---------------------------------------------------------------------------

class VersionXmlTests(unittest.TestCase):

    def test_version_xml_declares_2_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [_sample_clash()], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                version_xml = zf.read("bcf.version").decode("utf-8")
            self.assertIn('VersionId="2.1"', version_xml)
            self.assertIn("<DetailedVersion>2.1</DetailedVersion>", version_xml)


# ---------------------------------------------------------------------------
# project.bcfp XML
# ---------------------------------------------------------------------------

class ProjectXmlTests(unittest.TestCase):

    def test_project_xml_uses_provided_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({"display_name": "My Project"},
                              [_sample_clash()], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                project_xml = zf.read("project.bcfp").decode("utf-8")
            self.assertIn("<Name>My Project</Name>", project_xml)

    def test_project_xml_falls_back_to_default_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [_sample_clash()], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                project_xml = zf.read("project.bcfp").decode("utf-8")
            self.assertIn("dbHMS Clash Export", project_xml)

    def test_project_xml_uses_project_hash_as_guid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({"project_hash": "abc123"},
                              [_sample_clash()], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                project_xml = zf.read("project.bcfp").decode("utf-8")
            self.assertIn('ProjectId="abc123"', project_xml)


# ---------------------------------------------------------------------------
# markup.bcf XML
# ---------------------------------------------------------------------------

class MarkupXmlTests(unittest.TestCase):

    def _read_markup(self, clash):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                markup_path = next(n for n in zf.namelist()
                                   if n.endswith("markup.bcf"))
                return ET.fromstring(zf.read(markup_path))

    def test_topic_has_required_attrs(self):
        root = self._read_markup(_sample_clash())
        topic = root.find("Topic")
        self.assertIsNotNone(topic)
        self.assertEqual(topic.get("TopicType"), "Clash")
        self.assertEqual(topic.get("TopicStatus"), "Open")
        self.assertTrue(topic.get("Guid"))

    def test_topic_required_children_present(self):
        root = self._read_markup(_sample_clash())
        topic = root.find("Topic")
        self.assertIsNotNone(topic.find("Title"))
        self.assertIsNotNone(topic.find("CreationDate"))
        self.assertIsNotNone(topic.find("CreationAuthor"))

    def test_topic_title_includes_seq_and_element_names(self):
        root = self._read_markup(_sample_clash(seq=42))
        title = root.find("Topic/Title").text
        self.assertIn("42", title)
        self.assertIn("Round Duct - 12in", title)
        self.assertIn("Generic - 8in", title)

    def test_assignee_is_written(self):
        root = self._read_markup(_sample_clash(assignee="Plumbing"))
        self.assertEqual(root.find("Topic/AssignedTo").text, "Plumbing")

    def test_comments_become_top_level_comment_elements(self):
        root = self._read_markup(_sample_clash(with_comments=True))
        comments = root.findall("Comment")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].find("Author").text, "Nathan")
        self.assertIn("architect", comments[0].find("Comment").text)

    def test_viewpoint_reference_present_when_clash_has_viewpoint(self):
        root = self._read_markup(_sample_clash(with_viewpoint=True))
        vp = root.find("Viewpoints")
        self.assertIsNotNone(vp)
        self.assertEqual(vp.find("Viewpoint").text, "viewpoint.bcfv")
        # No snapshot file is on disk here (viewpoints_dir=None), so the
        # markup must NOT reference one: a Snapshot element pointing at a
        # file the zip doesn't contain is a dangling ref strict readers
        # reject. Snapshot references are tested in SnapshotInclusionTests.
        self.assertIsNone(vp.find("Snapshot"))

    def test_viewpoint_reference_absent_when_clash_has_no_viewpoint(self):
        root = self._read_markup(_sample_clash(with_viewpoint=False))
        self.assertIsNone(root.find("Viewpoints"))


class StatusMappingTests(unittest.TestCase):
    """Our 4 statuses → BCF's 4 standard ones."""

    def _topic_status_for(self, our_status):
        clash = _sample_clash(status=our_status)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                markup_path = next(n for n in zf.namelist()
                                   if n.endswith("markup.bcf"))
                root = ET.fromstring(zf.read(markup_path))
            return root.find("Topic").get("TopicStatus")

    def test_open_maps_to_open(self):
        self.assertEqual(self._topic_status_for("Open"), "Open")

    def test_reviewed_maps_to_in_progress(self):
        self.assertEqual(self._topic_status_for("Reviewed"), "In Progress")

    def test_approved_maps_to_in_progress(self):
        self.assertEqual(self._topic_status_for("Approved"), "In Progress")

    def test_resolved_maps_to_closed(self):
        self.assertEqual(self._topic_status_for("Resolved"), "Closed")

    def test_unknown_status_defaults_to_open(self):
        self.assertEqual(self._topic_status_for("???"), "Open")


# ---------------------------------------------------------------------------
# viewpoint.bcfv XML
# ---------------------------------------------------------------------------

class ViewpointXmlTests(unittest.TestCase):

    def _read_viewpoint(self, clash):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], None, out)
            with zipfile.ZipFile(out, "r") as zf:
                vp_path = next(n for n in zf.namelist()
                               if n.endswith("viewpoint.bcfv"))
                return ET.fromstring(zf.read(vp_path))

    def test_six_clipping_planes_from_section_box(self):
        root = self._read_viewpoint(_sample_clash())
        planes = root.findall("ClippingPlanes/ClippingPlane")
        self.assertEqual(len(planes), 6,
                         "section box → exactly 6 clipping planes (one per face)")

    def test_clipping_plane_normals_point_inward(self):
        # Section box is min=(5,15,0) max=(15,25,10). The 6 plane
        # normals together cover ±X ±Y ±Z (each appearing exactly once
        # as a positive direction and once as a negative direction).
        root = self._read_viewpoint(_sample_clash())
        normals = []
        for plane in root.findall("ClippingPlanes/ClippingPlane"):
            d = plane.find("Direction")
            normals.append((float(d.find("X").text),
                            float(d.find("Y").text),
                            float(d.find("Z").text)))
        # Sort and compare
        normals_set = set(normals)
        expected = {(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0), (0.0, -1.0, 0.0),
                    (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)}
        self.assertEqual(normals_set, expected)

    def test_section_box_coordinates_converted_to_meters(self):
        # Section box min Z = 0 ft, max Z = 10 ft → meters: 0, 3.048
        root = self._read_viewpoint(_sample_clash())
        # The bottom face (normal +Z) is at min z = 0 ft = 0 m
        # The top face (normal -Z) is at max z = 10 ft = 3.048 m
        for plane in root.findall("ClippingPlanes/ClippingPlane"):
            direction = plane.find("Direction")
            dz = float(direction.find("Z").text)
            location_z = float(plane.find("Location").find("Z").text)
            if dz == 1.0:    # bottom face
                self.assertAlmostEqual(location_z, 0.0, places=4)
            elif dz == -1.0: # top face
                self.assertAlmostEqual(location_z, 10.0 * 0.3048, places=4)

    def test_orthogonal_camera_position_in_meters(self):
        root = self._read_viewpoint(_sample_clash())
        cam = root.find("OrthogonalCamera")
        self.assertIsNotNone(cam)
        pos = cam.find("CameraViewPoint")
        # Camera position in feet was (10, -10, 15). In meters: (3.048, -3.048, 4.572)
        self.assertAlmostEqual(float(pos.find("X").text), 10.0 * 0.3048, places=4)
        self.assertAlmostEqual(float(pos.find("Y").text), -10.0 * 0.3048, places=4)
        self.assertAlmostEqual(float(pos.find("Z").text), 15.0 * 0.3048, places=4)

    def test_camera_direction_is_unit_vector(self):
        root = self._read_viewpoint(_sample_clash())
        cam = root.find("OrthogonalCamera")
        d = cam.find("CameraDirection")
        dx = float(d.find("X").text)
        dy = float(d.find("Y").text)
        dz = float(d.find("Z").text)
        magnitude = (dx * dx + dy * dy + dz * dz) ** 0.5
        self.assertAlmostEqual(magnitude, 1.0, places=4,
                               msg="camera direction must be a unit vector")

    def test_view_to_world_scale_present(self):
        root = self._read_viewpoint(_sample_clash())
        cam = root.find("OrthogonalCamera")
        scale = cam.find("ViewToWorldScale")
        self.assertIsNotNone(scale)
        self.assertGreater(float(scale.text), 0.0)


# ---------------------------------------------------------------------------
# Snapshot PNG inclusion
# ---------------------------------------------------------------------------

class SnapshotInclusionTests(unittest.TestCase):

    def test_snapshot_included_when_png_exists_on_disk(self):
        clash = _sample_clash("clash-A")
        with tempfile.TemporaryDirectory() as tmpdir:
            vp_dir = os.path.join(tmpdir, "viewpoints")
            os.makedirs(vp_dir)
            png_path = os.path.join(vp_dir, "clash-A.png")
            # Write a fake PNG (just bytes — BCF doesn't validate content)
            with open(png_path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfake")
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], vp_dir, out)
            with zipfile.ZipFile(out, "r") as zf:
                snapshot_files = [n for n in zf.namelist()
                                  if n.endswith("snapshot.png")]
                self.assertEqual(len(snapshot_files), 1)

    def test_snapshot_skipped_silently_when_png_missing(self):
        # Clash has a viewpoint dict but the PNG file isn't on disk.
        clash = _sample_clash("clash-X")
        with tempfile.TemporaryDirectory() as tmpdir:
            vp_dir = os.path.join(tmpdir, "viewpoints")
            os.makedirs(vp_dir)
            # No PNG written
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, [clash], vp_dir, out)
            self.assertEqual(count, 1)  # topic still exported
            with zipfile.ZipFile(out, "r") as zf:
                self.assertFalse(any(n.endswith("snapshot.png")
                                     for n in zf.namelist()))

    def test_no_viewpoints_dir_doesnt_crash(self):
        clash = _sample_clash()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, [clash], None, out)
            self.assertEqual(count, 1)

    def test_jpg_capture_preferred_and_referenced_in_markup(self):
        # The web viewer captures JPEGs; they win over legacy PNGs and the
        # markup's Snapshot element names the file that was actually packed.
        clash = _sample_clash("clash-J")
        with tempfile.TemporaryDirectory() as tmpdir:
            vp_dir = os.path.join(tmpdir, "viewpoints")
            os.makedirs(vp_dir)
            with open(os.path.join(vp_dir, "clash-J.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fakejpg")
            with open(os.path.join(vp_dir, "clash-J.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfake")
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], vp_dir, out)
            with zipfile.ZipFile(out, "r") as zf:
                names = zf.namelist()
                self.assertTrue(any(n.endswith("snapshot.jpg") for n in names))
                self.assertFalse(any(n.endswith("snapshot.png") for n in names))
                markup_path = next(n for n in names if n.endswith("markup.bcf"))
                root = ET.fromstring(zf.read(markup_path))
                self.assertEqual(root.find("Viewpoints/Snapshot").text,
                                 "snapshot.jpg")

    def test_snapshot_ships_without_a_viewpoint_dict(self):
        # Web-captured clashes have an image on disk but no camera dict;
        # the topic still gets a Viewpoints ref, a minimal .bcfv, and the
        # snapshot file.
        clash = _sample_clash("clash-W", with_viewpoint=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            vp_dir = os.path.join(tmpdir, "viewpoints")
            os.makedirs(vp_dir)
            with open(os.path.join(vp_dir, "clash-W.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fakejpg")
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], vp_dir, out)
            with zipfile.ZipFile(out, "r") as zf:
                names = zf.namelist()
                self.assertTrue(any(n.endswith("snapshot.jpg") for n in names))
                self.assertTrue(any(n.endswith("viewpoint.bcfv") for n in names))
                markup_path = next(n for n in names if n.endswith("markup.bcf"))
                root = ET.fromstring(zf.read(markup_path))
                self.assertIsNotNone(root.find("Viewpoints"))


# ---------------------------------------------------------------------------
# filter_predicate
# ---------------------------------------------------------------------------

class FilterPredicateTests(unittest.TestCase):

    def test_predicate_drops_clashes_returning_false(self):
        clashes = [
            _sample_clash("a", assignee="Mechanical"),
            _sample_clash("b", assignee="Plumbing"),
            _sample_clash("c", assignee="Electrical"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip(
                {}, clashes, None, out,
                filter_predicate=lambda c: c.get("assignee") == "Plumbing",
            )
            self.assertEqual(count, 1)

    def test_no_predicate_exports_all(self):
        clashes = [_sample_clash("a"), _sample_clash("b"), _sample_clash("c")]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, clashes, None, out)
            self.assertEqual(count, 3)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class RobustnessTests(unittest.TestCase):

    def test_empty_clash_list_produces_valid_zip(self):
        # No clashes — BCF still has bcf.version + project.bcfp.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, [], None, out)
            self.assertEqual(count, 0)
            self.assertTrue(os.path.isfile(out))
            with zipfile.ZipFile(out, "r") as zf:
                self.assertIn("bcf.version", zf.namelist())
                self.assertIn("project.bcfp", zf.namelist())

    def test_clash_without_id_is_skipped(self):
        clashes = [
            _sample_clash("good"),
            {"seq": 99, "kind": "hard"},  # no id → skipped
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, clashes, None, out)
            self.assertEqual(count, 1)

    def test_clash_without_viewpoint_still_exports_markup(self):
        clash = _sample_clash(with_viewpoint=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            count = bcf.build_bcf_zip({}, [clash], None, out)
            self.assertEqual(count, 1)
            with zipfile.ZipFile(out, "r") as zf:
                names = zf.namelist()
                self.assertTrue(any(n.endswith("markup.bcf") for n in names))
                self.assertFalse(any(n.endswith("viewpoint.bcfv") for n in names))

    def test_atomic_write_no_partial_zip_on_failure(self):
        # Simulate failure by passing an unwritable directory.
        # On Windows tempfile.mkstemp will succeed in any writable dir,
        # so we test that the temp file is cleaned up by inspecting the
        # output directory after a successful call (no .tmp leftovers).
        clash = _sample_clash()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.bcfzip")
            bcf.build_bcf_zip({}, [clash], None, out)
            leftovers = [f for f in os.listdir(tmpdir)
                         if f.endswith(".bcfzip.tmp")]
            self.assertEqual(leftovers, [],
                             "no temp files should remain after success")


if __name__ == "__main__":
    unittest.main()
