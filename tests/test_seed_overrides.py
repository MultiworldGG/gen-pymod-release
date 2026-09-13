from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed_wheels.py"
SPEC = importlib.util.spec_from_file_location("seed_wheels", SCRIPT_PATH)
assert SPEC is not None
seed_wheels = importlib.util.module_from_spec(SPEC)
sys.modules["seed_wheels"] = seed_wheels
assert SPEC.loader is not None
SPEC.loader.exec_module(seed_wheels)


def load_override(apworld: str):
    with mock.patch.object(seed_wheels, "SEED_OVERRIDES_DIR", REPO_ROOT / "seed_overrides"):
        override = seed_wheels._load_override(apworld)
    assert override is not None, f"no override for {apworld}"
    return override


# Mirrors the upstream game_watcher AUTO HINTING block at its real indentation,
# minus the network calls, so the override's anchors match verbatim.
PAPERMARIO_CLIENT = '''\
class PaperMarioClient:
    def __init__(self) -> None:
        self.autohint_stored = set()
        self.autohint_released = set()

    def autohint_candidates(self, current_location):
                # AUTO HINTING
                # Build list of items to scout
                hints = []
                for loc in location_groups["AutoHint"]:
                    if loc not in self.autohint_released.union(self.autohint_stored):
                        if current_location == (location_table[loc][2], location_table[loc][3]):
                            hints.append(loc)
                return hints
'''

PATCHED_FILTER = "if location_name_to_id[loc] not in self.autohint_released.union(self.autohint_stored):"


def instantiate_client(source: str):
    namespace = {
        "location_groups": {"AutoHint": ["Shop A", "Shop B"]},
        "location_table": {"Shop A": (0, 0, 1, 2), "Shop B": (0, 0, 1, 2)},
        "location_name_to_id": {"Shop A": 101, "Shop B": 102},
    }
    exec(compile(source, "client.py", "exec"), namespace)
    return namespace["PaperMarioClient"]()


class PaperMarioOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.override = load_override("papermario")

    def _apply_to(self, client_source: str) -> tuple[list[str], bytes]:
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = Path(tmp)
            (world_dir / "client.py").write_bytes(client_source.encode("utf-8"))
            changes = self.override.apply(world_dir)
            return changes, (world_dir / "client.py").read_bytes()

    def test_touches_only_client(self) -> None:
        self.assertEqual(self.override.TOUCHES, ["client.py"])

    def test_fixture_reproduces_name_vs_id_bug(self) -> None:
        client = instantiate_client(PAPERMARIO_CLIENT)
        client.autohint_released = {101, 102}
        self.assertEqual(client.autohint_candidates((1, 2)), ["Shop A", "Shop B"])

    def test_apply_dedupes_by_location_id(self) -> None:
        changes, patched = self._apply_to(PAPERMARIO_CLIENT)
        self.assertEqual(changes, ["patched client.py (1 edit(s))"])
        self.assertNotIn(b"\r", patched)
        self.assertIn(PATCHED_FILTER.encode("utf-8"), patched)
        self.assertNotIn(b"if loc not in self.autohint_released", patched)

        client = instantiate_client(patched.decode("utf-8"))
        client.autohint_released = {101}
        # autohint_stored becomes a list after the first scout upstream.
        client.autohint_stored = [102]
        self.assertEqual(client.autohint_candidates((1, 2)), [])
        client.autohint_stored = []
        self.assertEqual(client.autohint_candidates((1, 2)), ["Shop B"])
        self.assertEqual(client.autohint_candidates((3, 4)), [])

    def test_drifted_anchor_raises_and_leaves_file_untouched(self) -> None:
        drifted = PAPERMARIO_CLIENT.replace(
            "self.autohint_released.union(self.autohint_stored)",
            "self.autohint_released | set(self.autohint_stored)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = Path(tmp)
            (world_dir / "client.py").write_bytes(drifted.encode("utf-8"))
            with self.assertRaisesRegex(ValueError, "found 0.*drifted"):
                self.override.apply(world_dir)
            self.assertEqual((world_dir / "client.py").read_bytes(), drifted.encode("utf-8"))

    def test_ambiguous_anchor_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "found 2"):
            self._apply_to(PAPERMARIO_CLIENT + "\n" + PAPERMARIO_CLIENT)

    def test_missing_client_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                self.override.apply(Path(tmp))


ID1_WORLDS = ("doom_1993_beta", "doom_ii_beta", "heretic_beta")
ID1_FILES = {
    "__init__.py": b"from .options import Episode\n",
    "options.py": b"class Episode: ...\n",
    "LICENSE": b"zlib\n",
}


def make_id1_worlds_tree(tmp: str, apworld: str) -> Path:
    library = Path(tmp) / "worlds" / "_id1common"
    library.mkdir(parents=True)
    for name, data in ID1_FILES.items():
        (library / name).write_bytes(data)
    (library / "README.md").write_bytes(b"# id1Common\n")
    world_dir = library.parent / apworld
    world_dir.mkdir()
    return world_dir


class Id1CommonOverrideTests(unittest.TestCase):
    def test_every_id1_world_shares_the_vendor_contract(self) -> None:
        for apworld in ID1_WORLDS:
            with self.subTest(apworld=apworld):
                override = load_override(apworld)
                self.assertEqual(override.TOUCHES, [f"id1common/{name}" for name in ID1_FILES])

    def test_apply_vendors_library_as_importable_package(self) -> None:
        override = load_override("doom_1993_beta")
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = make_id1_worlds_tree(tmp, "doom_1993_beta")
            changes = override.apply(world_dir)
            self.assertEqual(len(changes), len(ID1_FILES))
            vendored = world_dir / "id1common"
            self.assertEqual(sorted(p.name for p in vendored.iterdir()), sorted(ID1_FILES))
            for name, data in ID1_FILES.items():
                self.assertEqual((vendored / name).read_bytes(), data)

            sys.path.insert(0, tmp)
            try:
                module = importlib.import_module("worlds.doom_1993_beta.id1common")
                self.assertTrue(hasattr(module, "Episode"))
            finally:
                sys.path.remove(tmp)
                for name in [n for n in sys.modules if n == "worlds" or n.startswith("worlds.")]:
                    del sys.modules[name]

    def test_missing_library_raises_before_writing(self) -> None:
        override = load_override("heretic_beta")
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = Path(tmp) / "worlds" / "heretic_beta"
            world_dir.mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                override.apply(world_dir)
            self.assertFalse((world_dir / "id1common").exists())


if __name__ == "__main__":
    unittest.main()
