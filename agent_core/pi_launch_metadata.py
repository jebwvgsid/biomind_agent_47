"""Small subprocess bridge; no model, session or domain client construction."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("model", "roots", "register", "validate"))
    parser.add_argument("--project")
    parser.add_argument("--record")
    parser.add_argument("--memory-root")
    parser.add_argument("--wiki-root")
    args = parser.parse_args(argv)
    try:
        if args.operation == "validate":
            from agent_core.project_directory import ProjectDirectoryError, validate_project_directory
            try:
                if not args.project:
                    raise ProjectDirectoryError("Project directory is required")
                result = {"valid": True, "project_dir": str(validate_project_directory(args.project))}
            except ProjectDirectoryError as error:
                result = {"valid": False, "code": error.code, "message": str(error)}
        elif args.operation == "model":
            from agent_core.pi_model_config import resolve_model_config
            selection, source = resolve_model_config(record_path=args.record,
                repo=Path(__file__).resolve().parents[1], project=args.project)
            registry = (str(Path(source["software_root"]) / "software_registry.json")
                        if source.get("software_root") and not os.environ.get("BIOMIND_PI_REGISTRY_PATH") else None)
            result = {"selection": selection, "registryPath": registry}
        else:
            from memory.wiki_memory import WikiMemoryManager
            if not args.memory_root:
                raise ValueError("MEMORY_ROOT_REQUIRED")
            result = (WikiMemoryManager.read_project_roots(args.memory_root, wiki_dir=args.wiki_root)
                      if args.operation == "roots" else
                      WikiMemoryManager.register_project_root(args.memory_root, args.project, wiki_dir=args.wiki_root))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, ImportError):
        # Never echo file content, model endpoint or credential-bearing errors.
        print(json.dumps({"error": "LAUNCH_METADATA_UNAVAILABLE_OR_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
