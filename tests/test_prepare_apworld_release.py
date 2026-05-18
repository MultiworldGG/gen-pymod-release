from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_apworld_release.py"
SPEC = importlib.util.spec_from_file_location("prepare_apworld_release", SCRIPT_PATH)
assert SPEC is not None
prepare_apworld_release = importlib.util.module_from_spec(SPEC)
sys.modules["prepare_apworld_release"] = prepare_apworld_release
assert SPEC.loader is not None
SPEC.loader.exec_module(prepare_apworld_release)


class PrepareApworldReleaseTests(unittest.TestCase):
    def resolve_target_version(self, **kwargs: object) -> str:
        with redirect_stdout(io.StringIO()):
            return prepare_apworld_release._resolve_target_version(**kwargs)

    def test_normalize_repo_slug_accepts_common_github_urls(self) -> None:
        cases = {
            "owner/repo": "owner/repo",
            "owner/repo.git": "owner/repo",
            "https://github.com/owner/repo": "owner/repo",
            "https://github.com/owner/repo.git": "owner/repo",
            "git@github.com:owner/repo.git": "owner/repo",
            "ssh://git@github.com/owner/repo.git": "owner/repo",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(prepare_apworld_release._normalize_repo_slug(value), expected)

    def test_versions_from_tags_filters_by_apworld_prefix(self) -> None:
        versions = prepare_apworld_release._versions_from_tags(
            "myclgm",
            ["myclgm-1.2.3", "other-9.9.9", "myclgm-", "myclgm-2.0.0"],
        )

        self.assertEqual(versions, {"1.2.3", "2.0.0"})

    def test_next_patch_version_uses_highest_semver_tag(self) -> None:
        version = prepare_apworld_release._next_patch_version({"1.2.3", "1.10.9", "not-semver"})

        self.assertEqual(version, "1.10.10")

    def test_existing_current_version_defaults_to_next_patch(self) -> None:
        version = self.resolve_target_version(
            current_version="1.2.3",
            requested_version=None,
            existing_versions={"1.2.3"},
            input_func=lambda _: "",
        )

        self.assertEqual(version, "1.2.4")

    def test_requested_version_mismatch_can_keep_current_choice(self) -> None:
        version = self.resolve_target_version(
            current_version="1.2.4",
            requested_version="1.2.3",
            existing_versions=set(),
            input_func=lambda _: "2",
        )

        self.assertEqual(version, "1.2.4")

    def test_missing_version_defaults_to_next_patch(self) -> None:
        version = self.resolve_target_version(
            current_version=None,
            requested_version=None,
            existing_versions={"1.2.3"},
            input_func=lambda _: "",
        )

        self.assertEqual(version, "1.2.4")

    def test_validate_release_assets_accepts_one_wheel_and_optional_apworld(self) -> None:
        summary = prepare_apworld_release._validate_release_assets(("world-1.0.0.whl", "world.apworld"))

        self.assertEqual(summary.wheel, "world-1.0.0.whl")
        self.assertEqual(summary.apworld, "world.apworld")

    def test_validate_release_assets_rejects_missing_wheel(self) -> None:
        with self.assertRaises(SystemExit):
            prepare_apworld_release._validate_release_assets(("world.apworld",))

    def test_validate_release_assets_rejects_multiple_apworlds(self) -> None:
        with self.assertRaises(SystemExit):
            prepare_apworld_release._validate_release_assets(
                ("world-1.0.0.whl", "world.apworld", "world-copy.apworld")
            )


if __name__ == "__main__":
    unittest.main()
