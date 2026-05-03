import ast
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXT_ROOT = (
    ROOT
    / "src"
    / "extensions"
    / "dbHMS Extensions.extension"
    / "dbHMS Tools.tab"
    / "dbHMS Tools.panel"
)


class ExtensionIntegrityTests(unittest.TestCase):
    def test_expected_pushbuttons_exist(self):
        expected = {
            "AlignViews.pushbutton",
            "Revisions Manager.pushbutton",
            "Sheet Manager.pushbutton",
            "SheetSetup.pushbutton",
            "View Range Helper.pushbutton",
            "View Templates Manager.pushbutton",
        }
        actual = {p.name for p in EXT_ROOT.glob("*.pushbutton")}
        self.assertTrue(expected.issubset(actual), "Missing one or more expected pushbuttons.")

    def test_python_scripts_are_valid_syntax(self):
        script_files = sorted(EXT_ROOT.rglob("script.py"))
        self.assertGreater(len(script_files), 0, "No script.py files found under extension panel.")

        for script_path in script_files:
            with self.subTest(script=str(script_path.relative_to(ROOT))):
                source = script_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(script_path))

    def test_source_files_have_no_merge_conflict_markers(self):
        source_files = list(EXT_ROOT.rglob("*.py")) + list(EXT_ROOT.rglob("*.xaml")) + list(
            EXT_ROOT.rglob("*.json")
        )
        conflict_re = re.compile(
            r"(?m)^\s*(<<<<<<< .+|=======|>>>>>>> .+)\s*$"
        )

        for file_path in source_files:
            with self.subTest(file=str(file_path.relative_to(ROOT))):
                text = file_path.read_text(encoding="utf-8")
                self.assertIsNone(conflict_re.search(text), "Conflict markers found.")

    def test_xaml_files_are_well_formed_xml(self):
        xaml_files = sorted(EXT_ROOT.rglob("*.xaml"))
        self.assertGreater(len(xaml_files), 0, "No XAML files found under extension panel.")

        for xaml_path in xaml_files:
            with self.subTest(xaml=str(xaml_path.relative_to(ROOT))):
                ET.parse(str(xaml_path))

    def test_json_configs_are_valid_and_have_required_keys(self):
        align_cfg_path = EXT_ROOT / "AlignViews.pushbutton" / "config.json"
        sheetsetup_cfg_path = EXT_ROOT / "SheetSetup.pushbutton" / "config.json"

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
