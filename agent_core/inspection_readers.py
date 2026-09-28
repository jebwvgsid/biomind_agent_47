"""Compatibility exports for the portable WGS bounded readers (single source)."""
from importlib import import_module
_implementation = import_module('skills.genomics.wgs-short-read-mapping.scripts.inspection_readers')
globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith('__')})
