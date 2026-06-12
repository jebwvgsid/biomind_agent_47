#!/usr/bin/env python3
"""Compatibility wrapper for the canonical crop GWAS SNP-to-gene mapper."""

from pathlib import Path
import runpy
import sys


COMMON = Path(__file__).resolve().parents[2] / "_gwas_common" / "scripts"
sys.path.insert(0, str(COMMON))
runpy.run_path(str(COMMON / "map_snp_to_gene.py"), run_name="__main__")
