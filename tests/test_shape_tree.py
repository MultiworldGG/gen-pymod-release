from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "shape_tree.py"
SPEC = importlib.util.spec_from_file_location("shape_tree", SCRIPT_PATH)
assert SPEC is not None
shape_tree = importlib.util.module_from_spec(SPEC)
sys.modules["shape_tree"] = shape_tree
assert SPEC.loader is not None
SPEC.loader.exec_module(shape_tree)

# The skip path logs a warning; keep test output clean.
logging.disable(logging.WARNING)


class ParseClientEntryPointsTests(unittest.TestCase):
    def parse(self, init_source: str, apworld: str = "demo") -> list[tuple[str, str]]:
        """Write a synthetic worlds/<apworld>/__init__.py and trace it."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "__init__.py").write_text(
                textwrap.dedent(init_source), encoding="utf-8"
            )
            return shape_tree.parse_client_entry_points(Path(tmp), apworld)

    # --- Process / multiprocessing dispatch (osu, sims4) ---------------------

    def test_process_keyword_target_absolute_import(self) -> None:
        # osu shape: `Process(target=main)` with an absolute inner import.
        source = """
            from multiprocessing import Process
            from ..LauncherComponents import Component, components, Type

            def run_client():
                from worlds.osu.Client import main
                p = Process(target=main)
                p.start()

            components.append(Component("osu! Client", func=run_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source, "osu"),
            [("worlds.osu.Client", "worlds.osu.Client:main")],
        )

    def test_process_keyword_target_relative_import(self) -> None:
        # sims4 shape: `Process(target=main)` with a relative inner import.
        source = """
            from multiprocessing import Process
            from worlds.LauncherComponents import Component, components, Type

            def run_client():
                from .Client import main
                p = Process(target=main)
                p.start()

            components.append(Component("Demo Client", func=run_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Client:main")],
        )

    def test_multiprocessing_dotted_process(self) -> None:
        source = """
            import multiprocessing
            from worlds.LauncherComponents import Component, components, Type

            def run_client():
                from .Client import main
                proc = multiprocessing.Process(target=main)
                proc.start()

            components.append(Component("Demo Client", func=run_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Client:main")],
        )

    # --- Nested wrapper defs (dk64, stardew_valley) --------------------------

    def test_nested_def_in_if_try(self) -> None:
        # dk64 shape: wrapper + Component nested inside `if baseclasses_loaded:`,
        # dispatch via `launch_component(launch, ...)`, client in an external pkg.
        source = """
            baseclasses_loaded = False
            try:
                import BaseClasses
                baseclasses_loaded = True
            except ImportError:
                pass
            if baseclasses_loaded:
                from worlds.LauncherComponents import Component, components, Type

                def launch_client():
                    from archipelago.DemoClient import launch
                    from worlds.LauncherComponents import launch as launch_component
                    launch_component(launch, name="Demo Client")

                components.append(Component("Demo Client", func=launch_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "archipelago.DemoClient:launch")],
        )

    def test_nested_aliased_launch(self) -> None:
        # stardew_valley shape: wrapper nested two `if`s deep, dispatch named
        # `launch` collides with an aliased client import `launch as client_main`.
        source = """
            TRACKER_ENABLED = True
            if TRACKER_ENABLED:
                import os
                if os.name:
                    from worlds.LauncherComponents import Component, components, Type

                    def launch_client(*args):
                        from worlds.LauncherComponents import launch
                        from .client import launch as client_main
                        launch(client_main, name="Demo Tracker", args=args)

                    components.append(Component("Demo Tracker", func=launch_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.client:launch")],
        )

    # --- Attribute dispatch target (jakanddaxter, poe, tboir) ----------------

    def test_attribute_dispatch_target(self) -> None:
        # `from . import client` then `launch_subprocess(client.launch, ...)`.
        source = """
            from worlds.LauncherComponents import components, Component, launch_subprocess, Type

            def launch_client():
                from . import client
                launch_subprocess(client.launch, name="Demo Client")

            components.append(Component("Demo Client", func=launch_client, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.client:launch")],
        )

    # --- Call-wrapped dispatch target (ufo50, xenobladex) --------------------

    def test_call_wrapped_target_direct(self) -> None:
        # ufo50 shape: `launch_subprocess(launch(*args), ...)` AND aliased Type.
        # Display name has no "Client", so this also exercises alias recognition.
        source = """
            from worlds.LauncherComponents import components, Component, launch_subprocess, Type as ComponentType

            CLIENT_NAME = "Demo"

            def launch_client(*args):
                from .Client import launch
                launch_subprocess(launch(*args), name=CLIENT_NAME)

            components.append(
                Component("Demo", game_name="Demo", func=launch_client, component_type=ComponentType.CLIENT)
            )
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Client:launch")],
        )

    def test_call_wrapped_target_partial(self) -> None:
        # xenobladex shape: `launch_subprocess(partial(launch, *args), ...)`.
        source = """
            from worlds.LauncherComponents import Component, components, launch_subprocess, Type
            from functools import partial

            def launch_client(*args):
                from .Client import launch
                launch_subprocess(partial(launch, *args), name="Demo Client")

            components.append(
                Component("Demo Client", func=launch_client, component_type=Type.CLIENT, game_name="Demo")
            )
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Client:launch")],
        )

    # --- Aliased Type (smo, saving_princess) ---------------------------------

    def test_type_alias_component_type(self) -> None:
        # smo shape: `Type as component_type`, Component assigned then appended,
        # dispatch via `launch_component(launch, ...)` with a nested-package import.
        source = """
            from worlds.LauncherComponents import (Component, components, Type as component_type, launch as launch_component)

            def launch_client(*args):
                from .Connector.Client import launch
                launch_component(launch, name="DemoClient", args=args)

            component = Component("Demo Client", component_type=component_type.CLIENT, game_name="Demo", func=launch_client)
            components.append(component)
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Connector.Client:launch")],
        )

    # --- Regression: canonical shape still works -----------------------------

    def test_canonical_shape_still_works(self) -> None:
        # saving_princess shape once Type is plain: `launch_subprocess(launch, ...)`.
        source = """
            from worlds.LauncherComponents import components, Component, launch_subprocess, Type

            CLIENT_NAME = "Demo"

            def launch_client(*args):
                from .Client import launch
                launch_subprocess(launch, name=CLIENT_NAME, args=args)

            components.append(
                Component("Demo Client", game_name="Demo", func=launch_client, component_type=Type.CLIENT)
            )
        """
        self.assertEqual(
            self.parse(source),
            [("worlds.demo.Client", "worlds.demo.Client:launch")],
        )

    def test_multiple_clients_keying_convention(self) -> None:
        # First client keyed `.Client`; additional clients keyed `.Client.<func>`.
        source = """
            from worlds.LauncherComponents import components, Component, launch_subprocess, Type

            def launch_client():
                from .Client import launch
                launch_subprocess(launch, name="Demo Client")

            def launch_alt():
                from .AltClient import launch as alt
                launch_subprocess(alt, name="Alt Client")

            components.append(Component("Demo Client", func=launch_client, component_type=Type.CLIENT))
            components.append(Component("Alt Client", func=launch_alt, component_type=Type.CLIENT))
        """
        self.assertEqual(
            self.parse(source),
            [
                ("worlds.demo.Client", "worlds.demo.Client:launch"),
                ("worlds.demo.Client.launch_alt", "worlds.demo.AltClient:launch"),
            ],
        )

    # --- Non-identifier apworld (2048) ---------------------------------------

    def test_non_identifier_apworld_returns_empty(self) -> None:
        source = """
            from worlds.LauncherComponents import components, Component, launch_subprocess, Type

            def launch_client():
                from .Client import launch
                launch_subprocess(launch, name="2048 Client")

            components.append(Component("2048 Client", func=launch_client, component_type=Type.CLIENT))
        """
        self.assertEqual(self.parse(source, "2048"), [])


if __name__ == "__main__":
    unittest.main()
