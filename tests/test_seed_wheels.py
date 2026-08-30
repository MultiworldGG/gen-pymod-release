from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "seed_wheels.py"
SPEC = importlib.util.spec_from_file_location("seed_wheels", SCRIPT_PATH)
assert SPEC is not None
seed_wheels = importlib.util.module_from_spec(SPEC)
sys.modules["seed_wheels"] = seed_wheels
assert SPEC.loader is not None
SPEC.loader.exec_module(seed_wheels)


def write_world(tmp: str, apworld: str, init_source: str) -> Path:
    world_dir = Path(tmp) / apworld
    world_dir.mkdir(parents=True)
    (world_dir / "__init__.py").write_text(textwrap.dedent(init_source), encoding="utf-8")
    return world_dir


class ScanCoverageGapsTests(unittest.TestCase):
    # scan_coverage_gaps only ever takes text (never a path), so it has no way
    # to mutate an authored requirements.txt - these cover its print-only output.

    def test_uncovered_import_prints_info_only(self) -> None:
        # factorio shape: authored `factorio-rcon-py==2.1.3`, scan detects the
        # import name `factorio_rcon` (canonical `factorio-rcon`) - no match.
        buf = io.StringIO()
        with redirect_stdout(buf):
            seed_wheels.scan_coverage_gaps(["factorio_rcon"], "factorio-rcon-py==2.1.3\n")
        output = buf.getvalue()
        self.assertIn("[deps][info]", output)
        self.assertIn("factorio_rcon", output)

    def test_covered_import_is_quiet(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            seed_wheels.scan_coverage_gaps(["colorama"], "colorama==0.4.6\n# comment\n")
        self.assertEqual(buf.getvalue(), "")


class PlanScanAdditionsTests(unittest.TestCase):
    def test_host_baseline_filters_additions(self) -> None:
        # detected imports yaml+colorama+vdf, mapped through IMPORT_NAME_MAP
        # first (yaml -> PyYAML) as detect_third_party_deps would do.
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = write_world(tmp, "demo", """
                import yaml
                import colorama
                import vdf
            """)
            detected = seed_wheels.detect_third_party_deps("demo", world_dir)
        self.assertEqual(detected, ["PyYAML", "colorama", "vdf"])

        plan = seed_wheels.plan_scan_additions(
            "demo", detected,
            host_provided=frozenset({"pyyaml", "colorama"}),
            resolver=lambda spec: True,
        )
        self.assertEqual(plan.additions, ["vdf"])
        self.assertEqual(plan.dropped, [])

    def test_unresolvable_dep_dropped_with_warning(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            plan = seed_wheels.plan_scan_additions(
                "demo", ["totally_fake_pkg"],
                host_provided=frozenset(),
                resolver=lambda spec: False,
            )
        self.assertEqual(plan.additions, [])
        self.assertEqual(plan.dropped, ["totally_fake_pkg"])
        output = buf.getvalue()
        self.assertIn("[deps][warn]", output)
        self.assertIn("totally_fake_pkg", output)
        self.assertIn("demo", output)

    def test_unverifiable_dep_ships_with_warning(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            plan = seed_wheels.plan_scan_additions(
                "demo", ["maybe_pkg"],
                host_provided=frozenset(),
                resolver=lambda spec: None,
                require_resolve_check=False,
            )
        self.assertEqual(plan.additions, ["maybe_pkg"])
        self.assertEqual(plan.dropped, [])
        self.assertIn("[deps][warn]", buf.getvalue())

    def test_require_resolve_check_exits_when_unverifiable(self) -> None:
        with self.assertRaises(SystemExit):
            seed_wheels.plan_scan_additions(
                "demo", ["maybe_pkg"],
                host_provided=frozenset(),
                resolver=lambda spec: None,
                require_resolve_check=True,
            )


class DetectThirdPartyDepsTests(unittest.TestCase):
    def test_attr_maps_to_attrs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = write_world(tmp, "demo", "import attr\n")
            self.assertEqual(seed_wheels.detect_third_party_deps("demo", world_dir), ["attrs"])

    def test_dev_tools_and_dunder_imports_never_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = write_world(tmp, "demo", """
                import pytest
                import PyInstaller
                import __vendor_private
            """)
            self.assertEqual(seed_wheels.detect_third_party_deps("demo", world_dir), [])


class HostProvidedParsingTests(unittest.TestCase):
    # _host_provided_dists reuses shape_tree.parse_requirements_txt +
    # _canonical_dist_name directly - exercise that parsing without touching
    # the lru_cache'd, MAIN_REPO-backed wrapper.
    def test_markers_comments_and_direct_refs(self) -> None:
        text = textwrap.dedent("""
            colorama==0.4.6
            chardet>=5,<6; python_version >= "3.8"
            # a full-line comment
            kivymd @ git+https://github.com/kivymd/KivyMD@5ff9d0d
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requirements.txt"
            path.write_text(text, encoding="utf-8")
            specs = seed_wheels.shape_tree.parse_requirements_txt(path)
        names = {seed_wheels.shape_tree._canonical_dist_name(s) for s in specs}
        self.assertEqual(names, {"colorama", "chardet", "kivymd"})


class DistNameTests(unittest.TestCase):
    def test_pinned_spec(self) -> None:
        self.assertEqual(seed_wheels._dist_name("pkg==1"), "pkg")

    def test_extra(self) -> None:
        self.assertEqual(seed_wheels._dist_name("pkg[extra]"), "pkg")

    def test_direct_ref_with_fragment(self) -> None:
        self.assertEqual(
            seed_wheels._dist_name("name @ git+https://example.com/repo@sha#egg=name"),
            "name",
        )

    def test_hash_continuation_first_line(self) -> None:
        self.assertEqual(seed_wheels._dist_name("somepkg==2.3.4 \\"), "somepkg")


class BuildIsCachedTests(unittest.TestCase):
    def _prior(self, **overrides: object) -> dict:
        prior = {
            "dependencies": [],
            "components": None,
            "deps_rev": seed_wheels.DEPS_REV,
            "wheel_filename": "worlds_demo-1.0.0-py3-none-any.whl",
        }
        prior.update(overrides)
        return prior

    def test_matching_deps_rev_with_wheel_present_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheels_dir = Path(tmp)
            prior = self._prior()
            (wheels_dir / prior["wheel_filename"]).write_bytes(b"")
            with mock.patch.object(seed_wheels, "WHEELS_DIR", wheels_dir):
                self.assertTrue(seed_wheels._build_is_cached(prior, force=False))

    def test_stale_deps_rev_forces_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheels_dir = Path(tmp)
            prior = self._prior(deps_rev=seed_wheels.DEPS_REV - 1)
            (wheels_dir / prior["wheel_filename"]).write_bytes(b"")
            with mock.patch.object(seed_wheels, "WHEELS_DIR", wheels_dir):
                self.assertFalse(seed_wheels._build_is_cached(prior, force=False))

    def test_force_always_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheels_dir = Path(tmp)
            prior = self._prior()
            (wheels_dir / prior["wheel_filename"]).write_bytes(b"")
            with mock.patch.object(seed_wheels, "WHEELS_DIR", wheels_dir):
                self.assertFalse(seed_wheels._build_is_cached(prior, force=True))


if __name__ == "__main__":
    unittest.main()
