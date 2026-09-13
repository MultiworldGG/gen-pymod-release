"""Seed-build override for an id1 (ap_gen_tool 2.0) world: vendor worlds/_id1common
as the ``.id1common`` fallback package. Implementation in ../id1common_vendor.py.
"""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_spec = spec_from_file_location(
    "id1common_vendor", Path(__file__).resolve().parents[1] / "id1common_vendor.py"
)
_vendor = module_from_spec(_spec)
_spec.loader.exec_module(_vendor)
TOUCHES, apply = _vendor.TOUCHES, _vendor.apply
