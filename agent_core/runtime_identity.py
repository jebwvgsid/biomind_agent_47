"""Safe execution identity in the existing call archive, never provider context.

Only enumerated implementation files are read. Credentials, configuration,
startup files and environment values are outside this capture contract.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import marshal
from pathlib import Path
import subprocess
import sys
from typing import Any

from agent_core.state_contract_store import StateContractStore

SOURCE_PATHS = ('bio_agents.py',
 'supervisor.py',
 'prompts/manager.py',
 'agent_core/runtime_identity.py',
 'agent_core/plan_contract.py',
 'agent_core/plan_tracker.py',
 'agent_core/plan_bindings.py',
 'agent_core/skill_access.py',
 'agent_core/state_contract_store.py',
 'agent_core/approved_resource_locator.py',
 'agent_core/recovery.py',
 'agent_core/step_effects.py',
 'agent_core/inspection_readers.py',
 'skills/genomics/wgs-short-read-mapping/scripts/inspection_readers.py',
 'agent_core/workflow/stage_contracts.py',
 'agent_core/workflow/outcomes.py',
 'agent_core/workflow/recovery_runtime.py',
 'agent_core/workflow/recovery_projection.py',
 'agent_core/workflow/recovery_tools.py',
 'agent_core/workflow/inspection_evidence.py',
 'agent_core/workflow/diagnostic_admission.py',
 'execution/executor.py',
 'agent_core/pi_entry.py',
 'agent_core/request_state.py',
 'agent_core/terminal_facts.py',
 'agent_core/planning_state_facts.py',
 'agent_core/execution_permissions.py',
 'agent_core/action_runtime.py',
 'integrations/pi_runtime/host.ts',
 'integrations/pi_runtime/extensions/biomind.ts',
 'integrations/pi_runtime/python_service.py',
 'integrations/pi_runtime/domain_adapter/service.py')
DEPENDENCIES = ('langgraph', 'langchain-core', 'pydantic', 'PyYAML')
MAX_SOURCE_BYTES = 8 * 1024 * 1024


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()


def _git(root: Path, args: list[str]) -> str:
    # Fixed argv, no shell; restricted to revision metadata and allowlisted paths.
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root), *args], check=True,
        capture_output=True, text=True, timeout=2, env={'PATH': '/usr/bin:/bin',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}).stdout.strip()


def capture_runtime_identity(*, data_path: str, task_id: str, stage: str,
                             skill_versions=None, root: Path | None = None) -> dict[str, Any]:
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    files, loaded, total = {}, {}, 0
    for name in SOURCE_PATHS:
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file() or not path.resolve().is_relative_to(root):
            files[name] = {'status': 'UNAVAILABLE'}
            continue
        with path.open('rb') as source:
            content = source.read(MAX_SOURCE_BYTES - total + 1)
        total += len(content)
        if total > MAX_SOURCE_BYTES:
            raise ValueError('RUNTIME_IDENTITY_SOURCE_SIZE_LIMIT')
        files[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                       'text': content.decode('utf-8')}
        module_name = name.removesuffix('.py').replace('/', '.')
        module = sys.modules.get(module_name)
        if module is None:
            continue
        callables = {}
        for key, value in vars(module).items():
            if inspect.isfunction(value) and value.__module__ == module_name:
                callables[key] = hashlib.sha256(marshal.dumps(value.__code__, 2)).hexdigest()
            elif inspect.isclass(value) and value.__module__ == module_name:
                for member, method in vars(value).items():
                    fn = method.__func__ if isinstance(method, (staticmethod, classmethod)) else method
                    if inspect.isfunction(fn):
                        callables[key + '.' + member] = hashlib.sha256(marshal.dumps(fn.__code__, 2)).hexdigest()
        loaded[module_name] = {'location': str(getattr(module, '__file__', '')),
                               'callable_code_digest': _hash(callables), 'callables': callables}
    revision = {'status': 'UNKNOWN', 'loaded_revision': 'UNKNOWN',
                'meaning': 'disk_at_capture and loaded callable identities are separate; no historical revision is inferred'}
    if (root / '.git').exists():
        try:
            revision.update(status='CAPTURED', disk_head_at_capture=_git(root, ['rev-parse', 'HEAD']),
                related_dirty_paths=_git(root, ['diff', '--name-only', 'HEAD', '--', *SOURCE_PATHS]).splitlines(),
                related_untracked_paths=_git(root, ['ls-files', '--others', '--exclude-standard', '--', *SOURCE_PATHS]).splitlines())
        except (OSError, subprocess.SubprocessError) as exc:
            revision['capture_error_type'] = type(exc).__name__
    manifest = root / 'candidate_manifest.json'
    if not (root / '.git').exists() and manifest.is_file() and not manifest.is_symlink():
        with manifest.open('rb') as source:
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) <= 2 * 1024 * 1024:
            candidate = json.loads(raw)
            revision.update(candidate_base_revision=candidate.get('head'), candidate_mode=candidate.get('mode'),
                            candidate_manifest_digest=hashlib.sha256(raw).hexdigest())
    dependencies = {}
    for name in DEPENDENCIES:
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = 'NOT_INSTALLED'
    snapshot = {'schema_version': 1, 'capture_policy': 'explicit_implementation_sources_v1',
        'source_files': files, 'loaded_modules': loaded, 'revision': revision,
        'dependencies': dependencies, 'python': sys.version.split()[0], 'loaded_digest_format': 'marshal_v2',
        'skill_versions': dict(skill_versions or {})}
    object_ref = StateContractStore(data_path, task_id).put_object('runtime.identity', snapshot)
    return {'schema_version': 1, 'status': 'CAPTURED', 'task_id': task_id, 'stage': stage,
            'object_ref': object_ref, 'source_identity': _hash({k: {a: b for a, b in v.items() if a != 'text'} for k, v in files.items()}),
            'loaded_code_identity': _hash(loaded), 'revision': revision,
            'dependencies': dependencies, 'skill_versions': dict(skill_versions or {})}


def runtime_identity_or_unavailable(**kwargs) -> dict[str, Any]:
    try:
        return capture_runtime_identity(**kwargs)
    except Exception as exc:
        # Observability must not suppress the existing provider/base/fallback.
        return {'schema_version': 1, 'status': 'UNAVAILABLE', 'stage': kwargs.get('stage'),
                'task_id': kwargs.get('task_id'), 'error_code': 'RUNTIME_IDENTITY_CAPTURE_FAILED',
                'error_type': type(exc).__name__}
