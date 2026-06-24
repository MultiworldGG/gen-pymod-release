from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_apworld_wheel.py"
SPEC = importlib.util.spec_from_file_location("build_apworld_wheel", SCRIPT_PATH)
assert SPEC is not None
build_apworld_wheel = importlib.util.module_from_spec(SPEC)
sys.modules["build_apworld_wheel"] = build_apworld_wheel
assert SPEC.loader is not None
SPEC.loader.exec_module(build_apworld_wheel)


class BuildApworldWheelTests(unittest.TestCase):
    def test_templates_dir_resolves_next_to_scripts(self) -> None:
        # Guards the dual-layout assumption: scripts/ + templates/ in this repo,
        # tools/build/scripts/ + tools/build/templates/ in the release bundle.
        templates_dir = build_apworld_wheel._TEMPLATES_DIR
        self.assertTrue((templates_dir / "pyproject.toml.j2").is_file())
        self.assertTrue((build_apworld_wheel._SCRIPTS_DIR / "shape_tree.py").is_file())

    def test_load_shape_tree_exposes_shape(self) -> None:
        self.assertTrue(callable(build_apworld_wheel._load_shape_tree().shape))

    def test_caller_repo_from_manifest_parses_repo_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "archipelago.json"
            manifest.write_text(
                '{"repo_url": "https://github.com/example/demo-world.git"}',
                encoding="utf-8",
            )
            self.assertEqual(
                build_apworld_wheel._caller_repo_from_manifest(manifest),
                "example/demo-world",
            )

    def test_caller_repo_from_manifest_falls_back_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "missing.json"
            self.assertEqual(
                build_apworld_wheel._caller_repo_from_manifest(manifest),
                "local/local",
            )

    def test_require_build_module_errors_with_pip_hint(self) -> None:
        with mock.patch.object(build_apworld_wheel.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                build_apworld_wheel._require_build_module()
        self.assertIn("pip install build", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
