"""BioMind product entry. Pi owns sessions, model calls and tool continuation."""
from __future__ import annotations

from agent_core.pi_entry import maybe_dispatch


def main(argv=None) -> int:
    return maybe_dispatch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
