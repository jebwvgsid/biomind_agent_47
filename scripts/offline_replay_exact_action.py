#!/usr/bin/env python3
"""Read-only replay of a legacy session through the exact-action boundary.

The replay never invokes a provider or executes the scientific command. It
extracts the approved-plan identity, the recorded Generate action, and its
artifact facts; removes only legacy Controller-owned artifact metadata; then
passes the executable fields through the current validator/environment builder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_core.action_runtime import build_exact_action_environment
from agent_core.managed_artifacts import (
    MANAGED_EXACT,
    ManagedArtifactBinding,
    concrete_artifact_provider_view,
    validate_concrete_action_artifacts,
)


UNRESOLVED_PREFIXES = ("{input:", "{output:", "{artifact:")
EXECUTABLE_FIELDS = (
    "type",
    "action_type",
    "language",
    "code",
    "argv",
    "env",
    "working_directory",
    "cwd",
    "script_adapter",
)


def _snapshot(paths: Iterable[Path]) -> Dict[str, Tuple[int, int]]:
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in paths
    }


def _contains_unresolved(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_unresolved(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unresolved(item) for item in value)
    if isinstance(value, str):
        return any(prefix in value for prefix in UNRESOLVED_PREFIXES)
    return False


def _load_action(record_path: Path) -> Dict[str, Any]:
    candidates = []
    with record_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            action = row.get("action_contract") if isinstance(row, Mapping) else None
            if not isinstance(action, Mapping):
                continue
            paths = action.get("materialized_artifacts") or {}
            if isinstance(paths, Mapping):
                candidates.append((len(paths), line_number, dict(action)))
    if not candidates:
        raise RuntimeError("No action_contract with artifact bindings was found")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _binding(handle: str, path: str) -> ManagedArtifactBinding:
    direction = handle.split(".", 1)[0]
    if direction not in {"input", "output"}:
        raise RuntimeError(f"Unsupported artifact direction in {handle!r}")
    suffix = Path(path).suffix.lower().lstrip(".") or "file"
    return ManagedArtifactBinding(
        artifact_id=f"offline-replay:{handle}",
        semantic_handle=handle,
        direction=direction,
        format=suffix,
        path_policy=MANAGED_EXACT,
        canonical_path=path,
        required_basename=Path(path).name,
        required=True,
        source_authority="legacy_session_read_only_replay",
    )


def replay(session_root: Path) -> Dict[str, Any]:
    session_root = session_root.resolve(strict=True)
    plan_path = session_root / "plan_state.json"
    record_path = session_root / "executions" / "execution_records.jsonl"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    recorded = _load_action(record_path)

    generated_ref = recorded.get("generated_script") or {}
    generated_path = Path(
        str(
            (generated_ref.get("path") if isinstance(generated_ref, Mapping) else "")
            or recorded.get("generated_script_path")
            or ""
        )
    ).resolve(strict=True)
    source = generated_path.read_text(encoding="utf-8")
    read_paths = (plan_path, record_path, generated_path)
    before = _snapshot(read_paths)

    raw_paths = recorded.get("materialized_artifacts") or {}
    bindings = {
        str(handle): _binding(str(handle), str(path))
        for handle, path in dict(raw_paths).items()
        if str(handle) and str(path)
    }
    if not bindings:
        raise RuntimeError("Recorded action has no concrete artifact paths")

    # Keep only the recorded executable representation. The omitted
    # controller_artifact_environment is legacy Controller metadata, not part
    # of the provider-authored scientific command.
    replay_action = {
        key: deepcopy(recorded.get(key))
        for key in EXECUTABLE_FIELDS
        if recorded.get(key) not in (None, "", [], {})
    }
    replay_action["code"] = source
    validated, violations = validate_concrete_action_artifacts(
        replay_action,
        bindings,
    )
    if violations:
        raise RuntimeError(json.dumps(
            [item.to_dict() for item in violations],
            ensure_ascii=False,
            sort_keys=True,
        ))

    base_env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "TMPDIR")
        if key in os.environ
    }
    action_env = dict(validated.get("env") or {})
    execute_env = build_exact_action_environment(
        base_env,
        action_environment=action_env,
    )
    concrete_paths = sorted(binding.canonical_path for binding in bindings.values())
    env_values = set(execute_env.values())
    after = _snapshot(read_paths)

    assertions = {
        "approved_plan_extracted": bool(
            str(plan.get("approval_status") or "").lower() == "approved"
            or str((plan.get("approval_snapshot") or {}).get("decision") or "").lower()
            == "approved"
        ),
        "three_concrete_paths_extracted": len(concrete_paths) == 3,
        "all_inputs_exist_and_are_readable": all(
            Path(path).is_file() and os.access(path, os.R_OK)
            for path in concrete_paths
        ),
        "provider_projection_has_concrete_paths": all(
            row.get("path") in concrete_paths
            for row in concrete_artifact_provider_view(bindings)
        ),
        "validated_action_has_no_unresolved_reference": not _contains_unresolved(validated),
        "validated_argv_unchanged": validated.get("argv") == replay_action.get("argv"),
        "validated_code_unchanged": validated.get("code") == replay_action.get("code"),
        "exact_environment_preserves_all_concrete_paths": all(
            path in env_values for path in concrete_paths
        ),
        "exact_environment_has_no_placeholder_value": not _contains_unresolved(execute_env),
        "legacy_session_files_unchanged": before == after,
        "provider_called": False,
        "scientific_command_executed": False,
    }
    passed = all(
        value is True
        for key, value in assertions.items()
        if key not in {"provider_called", "scientific_command_executed"}
    ) and assertions["provider_called"] is False and assertions["scientific_command_executed"] is False

    legacy_controller_env = [
        dict(row)
        for row in recorded.get("controller_artifact_environment", []) or []
        if isinstance(row, Mapping)
    ]
    return {
        "schema_version": 1,
        "replay_status": "PASS" if passed else "FAIL",
        "session_root": str(session_root),
        "approved_plan": {
            "plan_id": str(plan.get("plan_id") or ""),
            "plan_version": str(plan.get("plan_version") or ""),
            "decision": str(
                (plan.get("approval_snapshot") or {}).get("decision")
                or plan.get("approval_status")
                or ""
            ),
        },
        "generate_action": {
            "step_id": str(recorded.get("step_id") or ""),
            "selected_skill_id": str(recorded.get("selected_skill_id") or ""),
            "concrete_artifact_paths": concrete_paths,
            "legacy_controller_artifact_metadata_present": bool(legacy_controller_env),
            "legacy_controller_placeholder_values": sorted({
                str(row.get("placeholder") or "")
                for row in legacy_controller_env
                if str(row.get("placeholder") or "")
            }),
        },
        "current_exact_action_boundary": {
            "controller_metadata_removed_before_validation": [
                "controller_artifact_environment"
            ],
            "controller_synthesized_scientific_env": False,
            "recorded_executable_env_keys": sorted(action_env),
            "validated_action_env": action_env,
            "execute_env_artifact_values": sorted(
                value for value in execute_env.values() if value in concrete_paths
            ),
            "violations": [],
        },
        "assertions": assertions,
        "read_only_files": [
            {
                "path": path,
                "size": size,
                "mtime_ns": mtime,
            }
            for path, (size, mtime) in sorted(after.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "session_root",
        type=Path,
        help="Read-only BioMind task_outputs/session_* directory",
    )
    args = parser.parse_args()
    result = replay(args.session_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["replay_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
