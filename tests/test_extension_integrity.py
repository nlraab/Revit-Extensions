import ast
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "src" / "extensions" / "dbHMS Extensions.extension"

DBHMS_PANEL = EXTENSION_ROOT / "dbHMS Tools.tab" / "dbHMS Tools.panel"
# Clash Detection used to be its own tab; it now lives as a panel
# inside dbHMS Tools.tab (Iter 16 restructure — single-tab layout).
CLASH_PANEL = EXTENSION_ROOT / "dbHMS Tools.tab" / "Clash Detection.panel"
LIB_ROOT = EXTENSION_ROOT / "lib"


def _read_bundle_layout(bundle_yaml_path):
    """Parse the simple `layout:` list from a tiny pyRevit bundle.yaml without PyYAML.

    Only handles the format we actually write:
        layout:
          - Item One
          - Item Two
          - -----
          - Item Three

    Skips visual separators (lines that are just dashes).
    """
    items = []
    in_layout = False
    for raw_line in bundle_yaml_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        if raw_line.lstrip() == raw_line and raw_line.rstrip(":") == "layout":
            in_layout = True
            continue
        if in_layout:
            stripped = raw_line.lstrip()
            # Top-level key after layout: stops parsing.
            if raw_line == stripped and ":" in raw_line:
                break
            if stripped.startswith("- "):
                value = stripped[2:].strip()
                if set(value) == {"-"}:
                    continue  # visual separator
                items.append(value)
    return items


class ExtensionIntegrityTests(unittest.TestCase):
    def test_expected_pushbuttons_exist(self):
        expected = {
            "AlignViews.pushbutton",
            "Chatbot.pushbutton",
            "Parameters Management.pushbutton",
            "Revisions Manager.pushbutton",
            "Sheet Manager.pushbutton",
            "SheetSetup.pushbutton",
            "View Range Helper.pushbutton",
            "View Templates Manager.pushbutton",
        }
        actual = {p.name for p in DBHMS_PANEL.glob("*.pushbutton")}
        self.assertTrue(expected.issubset(actual), "Missing one or more expected pushbuttons.")

    def test_clash_detection_pushbuttons_exist(self):
        expected = {
            "Run Clash Test.pushbutton",
            "Clash Browser.pushbutton",
            "Test Library.pushbutton",
            "Reports.pushbutton",
            "Settings.pushbutton",
            "3D Viewer.pushbutton",
        }
        actual = {p.name for p in CLASH_PANEL.glob("*.pushbutton")}
        self.assertTrue(
            expected.issubset(actual),
            "Missing one or more expected Clash Detection pushbuttons. Got: %s" % sorted(actual),
        )

    def test_clash_detection_panel_layout_matches_pushbuttons(self):
        # pyRevit controls panel button order via bundle.yaml's `layout:` key.
        # Each entry is the bundle name WITHOUT the .pushbutton suffix.
        bundle_path = CLASH_PANEL / "bundle.yaml"
        self.assertTrue(bundle_path.exists(), "bundle.yaml is missing in Clash Detection panel.")

        layout_names = _read_bundle_layout(bundle_path)
        self.assertTrue(layout_names, "bundle.yaml has no `layout:` entries.")

        pushbutton_names_on_disk = {p.name[:-len(".pushbutton")] for p in CLASH_PANEL.glob("*.pushbutton")}
        layout_set = set(layout_names)

        # Every entry in layout must correspond to a real pushbutton folder.
        missing_on_disk = layout_set - pushbutton_names_on_disk
        self.assertFalse(
            missing_on_disk,
            "bundle.yaml layout references pushbuttons that don't exist: %s" % sorted(missing_on_disk),
        )
        # Every pushbutton on disk must be listed in layout (so toolbar order is deterministic).
        missing_in_layout = pushbutton_names_on_disk - layout_set
        self.assertFalse(
            missing_in_layout,
            "Pushbuttons exist on disk but are missing from bundle.yaml layout: %s" % sorted(missing_in_layout),
        )

    def test_clash_detection_pushbuttons_have_icons(self):
        for pushbutton_dir in CLASH_PANEL.glob("*.pushbutton"):
            with self.subTest(pushbutton=pushbutton_dir.name):
                icon = pushbutton_dir / "icon.png"
                self.assertTrue(icon.exists(), "icon.png is missing for %s" % pushbutton_dir.name)
                self.assertGreater(icon.stat().st_size, 0, "icon.png is empty for %s" % pushbutton_dir.name)

    def test_python_scripts_are_valid_syntax(self):
        # Walk the entire extension - covers both tabs AND the shared lib/ folder.
        py_files = sorted(EXTENSION_ROOT.rglob("*.py"))
        self.assertGreater(len(py_files), 0, "No .py files found under extension.")

        for py_path in py_files:
            with self.subTest(py=str(py_path.relative_to(ROOT))):
                source = py_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_path))

    def test_source_files_have_no_merge_conflict_markers(self):
        source_files = (
            list(EXTENSION_ROOT.rglob("*.py"))
            + list(EXTENSION_ROOT.rglob("*.xaml"))
            + list(EXTENSION_ROOT.rglob("*.json"))
            + list(EXTENSION_ROOT.rglob("*.md"))
        )
        conflict_re = re.compile(
            r"(?m)^\s*(<<<<<<< .+|=======|>>>>>>> .+)\s*$"
        )

        for file_path in source_files:
            with self.subTest(file=str(file_path.relative_to(ROOT))):
                text = file_path.read_text(encoding="utf-8")
                self.assertIsNone(conflict_re.search(text), "Conflict markers found.")

    def test_xaml_files_are_well_formed_xml(self):
        xaml_files = sorted(EXTENSION_ROOT.rglob("*.xaml"))
        self.assertGreater(len(xaml_files), 0, "No XAML files found under extension.")

        for xaml_path in xaml_files:
            with self.subTest(xaml=str(xaml_path.relative_to(ROOT))):
                ET.parse(str(xaml_path))

    def test_all_json_files_parse(self):
        # Generic gate - any JSON anywhere in the extension must parse.
        json_files = sorted(EXTENSION_ROOT.rglob("*.json"))
        self.assertGreater(len(json_files), 0, "No JSON files found under extension.")

        for json_path in json_files:
            with self.subTest(json=str(json_path.relative_to(ROOT))):
                json.loads(json_path.read_text(encoding="utf-8"))

    def test_json_configs_are_valid_and_have_required_keys(self):
        align_cfg_path = DBHMS_PANEL / "AlignViews.pushbutton" / "config.json"
        sheetsetup_cfg_path = DBHMS_PANEL / "SheetSetup.pushbutton" / "config.json"

        for cfg_path in (align_cfg_path, sheetsetup_cfg_path):
            with self.subTest(config=str(cfg_path.relative_to(ROOT))):
                self.assertTrue(cfg_path.exists(), "Config file is missing.")
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertIsInstance(cfg, dict, "Config root must be an object.")

        align_cfg = json.loads(align_cfg_path.read_text(encoding="utf-8"))
        self.assertIn("filters", align_cfg)
        self.assertIn("match", align_cfg)
        self.assertIn("same_scale", align_cfg["filters"])
        self.assertIn("same_titleblock", align_cfg["filters"])
        self.assertIn("viewport_position", align_cfg["match"])
        self.assertIn("title_position", align_cfg["match"])

        sheet_cfg = json.loads(sheetsetup_cfg_path.read_text(encoding="utf-8"))
        self.assertIn("patterns", sheet_cfg)
        self.assertIn("options", sheet_cfg)
        self.assertIn("disciplines", sheet_cfg)
        self.assertIsInstance(sheet_cfg["disciplines"], list)
        self.assertGreater(len(sheet_cfg["disciplines"]), 0, "At least one discipline is required.")

        for discipline in sheet_cfg["disciplines"]:
            self.assertIn("code", discipline)
            self.assertIn("name", discipline)
            self.assertIn("plan_types", discipline)
            self.assertIsInstance(discipline["plan_types"], list)

    def test_clash_default_tests_library_shape(self):
        path = CLASH_PANEL / "Test Library.pushbutton" / "default_tests.json"
        self.assertTrue(path.exists(), "default_tests.json is missing.")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict, "default_tests.json root must be an object.")
        self.assertIn("$schema_version", data)
        self.assertIn("tests", data)
        self.assertIsInstance(data["tests"], list)
        self.assertGreater(len(data["tests"]), 0, "At least one default clash test must be shipped.")

        valid_kinds = {"hard", "soft", "clearance"}
        valid_sources = {"host", "link:Architectural", "link:Structural"}
        valid_disciplines = {
            "Mechanical", "Electrical", "Plumbing", "Fire Protection",
            "Technology", "Architectural", "Structural",
        }
        seen_ids = set()

        for test in data["tests"]:
            with self.subTest(test_id=test.get("id")):
                for required_key in ("id", "name", "kind", "tolerance_inches", "set_a", "set_b", "default_assignee"):
                    self.assertIn(required_key, test, "Missing key %r" % required_key)

                self.assertNotIn(test["id"], seen_ids, "Duplicate test id: %s" % test["id"])
                seen_ids.add(test["id"])

                self.assertIn(test["kind"], valid_kinds, "Bad kind: %s" % test["kind"])
                self.assertIsInstance(test["tolerance_inches"], (int, float))
                if test["kind"] == "soft":
                    self.assertGreater(test["tolerance_inches"], 0, "Soft clash tolerance must be > 0")

                self.assertIn(test["default_assignee"], valid_disciplines, "Bad assignee: %s" % test["default_assignee"])

                for set_name in ("set_a", "set_b"):
                    s = test[set_name]
                    self.assertIn("source", s)
                    src = s["source"]
                    # source may be a single string OR a list of strings (multi-source)
                    if isinstance(src, list):
                        self.assertGreater(len(src), 0, "source list cannot be empty")
                        for one in src:
                            self.assertIn(one, valid_sources, "Bad source in list: %s" % one)
                    else:
                        self.assertIn(src, valid_sources, "Bad source: %s" % src)
                    self.assertIn("categories", s)
                    self.assertIsInstance(s["categories"], list)
                    self.assertGreater(len(s["categories"]), 0, "Categories list cannot be empty")

    def test_clash_lib_packages_have_init(self):
        # Every clash_* package directory must have an __init__.py so it's importable.
        for pkg_dir in LIB_ROOT.glob("clash_*"):
            with self.subTest(package=pkg_dir.name):
                init_file = pkg_dir / "__init__.py"
                self.assertTrue(init_file.exists(), "Missing __init__.py in %s" % pkg_dir)
        # Subpackages (e.g. clearance/) too.
        for sub_init in LIB_ROOT.rglob("__init__.py"):
            with self.subTest(init=str(sub_init.relative_to(ROOT))):
                # Just confirms it's parseable - syntax check is covered elsewhere.
                ast.parse(sub_init.read_text(encoding="utf-8"))

    def test_dbhms_telemetry_lib_exists_and_exports_public_api(self):
        # The shared telemetry recorder used by every pushbutton.
        pkg = LIB_ROOT / "dbhms_telemetry"
        init_file = pkg / "__init__.py"
        impl_file = pkg / "telemetry.py"
        self.assertTrue(init_file.exists(), "Missing dbhms_telemetry/__init__.py")
        self.assertTrue(impl_file.exists(), "Missing dbhms_telemetry/telemetry.py")

        # The package must re-export the public surface that the scripts depend on.
        init_src = init_file.read_text(encoding="utf-8")
        for name in ("session", "start", "end"):
            self.assertIn(
                name,
                init_src,
                "dbhms_telemetry/__init__.py is missing public name %r" % name,
            )

        # And the implementation must define them (under their real names).
        impl_tree = ast.parse(impl_file.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(impl_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for name in ("session", "start", "end", "Session"):
            self.assertIn(
                name,
                defined,
                "dbhms_telemetry/telemetry.py does not define %r" % name,
            )

    def test_every_pushbutton_records_telemetry(self):
        # Every pushbutton script.py must wire dbhms_telemetry. The CLAUDE.md
        # rule is: import the module and call either session() (modal tools)
        # or start() (modeless tools). This test fails the build if a new
        # pushbutton ships without telemetry.
        panels = (DBHMS_PANEL, CLASH_PANEL)
        for panel in panels:
            for pushbutton_dir in panel.glob("*.pushbutton"):
                script = pushbutton_dir / "script.py"
                if not script.exists():
                    continue  # icon-only buttons (none today, but safe)
                with self.subTest(pushbutton=pushbutton_dir.name):
                    src = script.read_text(encoding="utf-8")
                    self.assertIn(
                        "import dbhms_telemetry",
                        src,
                        "%s does not import dbhms_telemetry" % pushbutton_dir.name,
                    )
                    has_session = "dbhms_telemetry.session(" in src
                    has_start = "dbhms_telemetry.start(" in src
                    self.assertTrue(
                        has_session or has_start,
                        "%s imports dbhms_telemetry but never calls session() or start()"
                        % pushbutton_dir.name,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
