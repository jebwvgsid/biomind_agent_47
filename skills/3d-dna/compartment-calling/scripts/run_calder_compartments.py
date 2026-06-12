#!/usr/bin/env python
"""Compatibility entry point for the CALDER2 sub-compartment skill.

CALDER2 is implemented under skills/3d-dna/calder2-subcompartment. Keep this
thin forwarder so older saved commands fail less abruptly, but do not use this
path in new skills or runtime contracts.
"""

from __future__ import annotations

import runpy
from pathlib import Path


TARGET = (
    Path(__file__).resolve().parents[2]
    / "calder2-subcompartment"
    / "scripts"
    / "run_calder_compartments.py"
)

if not TARGET.is_file():
    raise SystemExit(f"[ERROR] CALDER2 wrapper not found: {TARGET}")

runpy.run_path(str(TARGET), run_name="__main__")
