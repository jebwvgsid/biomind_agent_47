"""Use the existing declared-output guard before native formal execution.

This protects declared files, not arbitrary shell side effects. Tool caches and
logs still need task-scoped locations. Receipt identity is ownership evidence,
never scientific acceptance or an upgrade of historical fingerprint strength.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat


def _owned_targets(state, targets, data_path):
    from agent_core.action_runtime import _output_path_state
    from agent_core.request_state import request_scope
    owned = {}
    root = Path(data_path).absolute() / 'task_outputs' / state['task_id'] / 'attempts'
    for attempt_id, row in ((state.get('provenance_state') or {}).get('attempts') or {}).items():
        expected = root / attempt_id / 'exit_status.json'
        if row.get('command_receipt') != str(expected) or expected.is_symlink():
            continue
        try:
            raw = expected.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row.get('command_receipt_sha256'):
                continue
            receipt = json.loads(raw)
        except (OSError, ValueError):
            continue
        if (receipt.get('task_id') != state['task_id'] or receipt.get('attempt_id') != attempt_id
                or receipt.get('request_scope') != request_scope(state)
                or receipt.get('terminal') is not True or receipt.get('cleanup_confirmed') is not True
                or receipt.get('status') != 'exited'):
            continue
        admitted = {r['path'] for r in (receipt.get('output_write_guard') or {}).get('targets', [])}
        for path, identity in (receipt.get('output_file_identities') or {}).items():
            if path not in targets or path not in admitted or not identity.get('exists'):
                continue
            try:
                if _output_path_state(Path(path)) == identity:
                    owned[path] = {'producer_attempt_id': attempt_id,
                                   'receipt_ref': str(expected), 'identity': identity}
            except (OSError, ValueError):
                continue
    return owned



def validate_control_output_separation(state, output_rows, *, data_path):
    """Check declared write/move roots only; cwd and arbitrary shell are opaque."""
    from agent_core.action_runtime import ActionRuntimeContractError
    from agent_core.plan_tracker import canonical_task_spec_for_state
    control = Path(data_path).absolute() / 'task_outputs' / state['task_id']
    controls = [control.resolve()]
    task = state.get('task_state') or {}
    for key in ('planner_loop', 'generate_context_episode'):
        path = (task.get(key) or {}).get('draft_file', {}).get('path')
        if path:
            controls.append(Path(path).resolve())
    spec = canonical_task_spec_for_state(state) or {}
    roots = list(output_rows) + [r for r in spec.get('output_contracts') or []
        if isinstance(r, dict) and r.get('role') in {'output_dir', 'backup_dir', 'backup_root'}]
    targets = []
    for row in roots:
        raw = row.get('canonical_path') or row.get('path')
        if not raw or not Path(raw).is_absolute():
            continue
        path = Path(raw)
        normalized = path.resolve()
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ActionRuntimeContractError('OUTPUT_CONTROL_ROOT_SYMLINK', path=str(path))
        if any(normalized == c or normalized.is_relative_to(c) or c.is_relative_to(normalized) for c in controls):
            raise ActionRuntimeContractError('OUTPUT_OVERLAPS_ACTIVE_CONTROL', path=str(path), control=str(control))
        targets.append((row.get('role'), normalized))
    # Existing exact-file preservation copies to a sibling hidden file. It is
    # not a directory-move scope and must not be rejected because its parent
    # is the approved results directory. Check its actual source/destination.
    auth = task.get('output_write_authorization') or {}
    for row in (auth.get('guard') or {}).get('targets') or []:
        if not row.get('backup_path'): continue
        raw = Path(row['backup_path'])
        backup = raw.resolve()
        source = Path(row['path']).resolve()
        if any(p.is_symlink() for p in [raw, *raw.parents]):
            raise ActionRuntimeContractError('OUTPUT_CONTROL_ROOT_SYMLINK', path=str(raw))
        if any(backup == c or backup.is_relative_to(c) or c.is_relative_to(backup) for c in controls):
            raise ActionRuntimeContractError('BACKUP_OVERLAPS_ACTIVE_CONTROL', path=str(backup))
        if backup == source or backup.is_relative_to(source) or source.is_relative_to(backup):
            raise ActionRuntimeContractError('BACKUP_OUTPUT_ROOT_OVERLAP', path=str(backup))
    for role, backup in targets:
        if role not in {'backup_dir', 'backup_root'}: continue
        if any(backup == c or backup.is_relative_to(c) or c.is_relative_to(backup) for c in controls):
            raise ActionRuntimeContractError('BACKUP_OVERLAPS_ACTIVE_CONTROL', path=str(backup))
        if any(backup == output or backup.is_relative_to(output) or output.is_relative_to(backup)
               for other_role, output in targets if other_role not in {'backup_dir', 'backup_root'}):
            raise ActionRuntimeContractError('BACKUP_OUTPUT_ROOT_OVERLAP', path=str(backup))

def prepare_output_protection(state, output_rows, *, data_path, attempt_dir, scope_guard, preparation_observation=None):
    from agent_core.action_runtime import (capture_output_write_guard, validate_output_write_guard,
        preserve_approved_output_files, ActionRuntimeContractError, stable_digest)
    from agent_core.request_state import request_scope
    validate_control_output_separation(state, output_rows, data_path=data_path)
    contracts = [{'expected_path': row['canonical_path'], 'artifact_id': row.get('artifact_id', ''),
                  'artifact_kind': row.get('artifact_kind') or ('directory' if row.get('format') == 'directory' else 'file')}
                 for row in output_rows]
    guard = capture_output_write_guard(contracts)
    targets = {row['path'] for row in guard['targets']}
    owned = _owned_targets(state, targets, data_path)
    conflicts = [row for row in guard['targets'] if row['before']['exists'] and row['path'] not in owned]
    protection = {'owned_targets': owned, 'backups': []}
    if conflicts:
        auth = (state.get('task_state') or {}).get('output_write_authorization') or {}
        approved = auth.get('guard') or {}
        expected_paths = set(auth.get('paths') or [])
        if (auth.get('source') != 'user_explicit' or not auth.get('source_ref')
                or auth.get('task_id') != state['task_id']
                or auth.get('request_scope') != request_scope(state)
                or not {row['path'] for row in conflicts} <= expected_paths):
            raise ActionRuntimeContractError('OUTPUT_REPLACEMENT_NOT_APPROVED',
                paths=[row['path'] for row in conflicts],
                required_action='Read the existing files, then ask for reuse, a new output location, or explicit backup-and-replace. No command was launched.')
        # The host-frozen user decision must still refer to those exact bytes.
        if stable_digest({k: v for k, v in approved.items() if k != 'guard_hash'}) != approved.get('guard_hash'):
            raise ActionRuntimeContractError('OUTPUT_WRITE_GUARD_INVALID')
        approvals = {row['path']: row for row in approved['targets']}
        if any(approvals.get(row['path'], {}).get('before') != row['before'] for row in conflicts):
            raise ActionRuntimeContractError('OUTPUT_TARGET_CHANGED_SINCE_REVIEW')
        subset = {'schema_version': 1, 'policy': 'preserve_existing_files',
                  'targets': [deepcopy(approvals[row['path']]) for row in conflicts]}
        subset['guard_hash'] = stable_digest(subset)
        # Other approved targets may already have been written by an earlier
        # step. Validate only the still-conflicting files admitted here.
        validate_output_write_guard(subset)
        scope_guard()
        receipt_path = Path(attempt_dir) / 'output_write_approval.json'
        if receipt_path.exists():
            if receipt_path.is_symlink():
                raise ActionRuntimeContractError('OUTPUT_BACKUP_CHANGED')
            protection = json.loads(receipt_path.read_text())
            if (protection.get('guard_hash') != subset['guard_hash']
                    or protection.get('source_ref') != auth['source_ref']):
                raise ActionRuntimeContractError('OUTPUT_WRITE_SCOPE_CHANGED')
            backups = protection.get('backups') or []
            if ({(b['original_path'], b['backup_path']) for b in backups}
                    != {(r['path'], r['backup_path']) for r in subset['targets']}
                    or len(backups) != len(subset['targets'])):
                raise ActionRuntimeContractError('OUTPUT_BACKUP_CHANGED')
            for backup in backups:
                digest, size = hashlib.sha256(), 0
                fd = os.open(backup['backup_path'], os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ActionRuntimeContractError('OUTPUT_BACKUP_CHANGED')
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(chunk); size += len(chunk)
                if size != backup['size'] or digest.hexdigest() != backup['sha256']:
                    raise ActionRuntimeContractError('OUTPUT_BACKUP_CHANGED')
        else:
            # Mark the actual effect boundary before copying anything. Failure
            # until the durable approval receipt stays unresolved.
            if preparation_observation is not None:
                preparation_observation.update(safe_to_release=False, phase='output_preservation')
                preparation_observation['effects'].append({'kind':'output_preservation_started',
                    'targets':deepcopy(subset['targets']), 'receipt_ref':str(receipt_path), 'retained':True})
            protection = preserve_approved_output_files(subset, decision='approve_backup_replace',
                approval_identity={'approval_identity_hash': stable_digest({
                    'source_ref': auth['source_ref'], 'guard_hash': subset['guard_hash'],
                    'request_scope': request_scope(state)})})
            protection.update(owned_targets=owned, source_ref=auth['source_ref'])
            from agent_core.command_inspection_owner import _once
            _once(receipt_path, protection)
            if preparation_observation is not None:
                preparation_observation['effects'].append({'kind':'output_preservation_completed',
                    'receipt_ref':str(receipt_path), 'receipt':deepcopy(protection), 'retained':True})
                preparation_observation.update(safe_to_release=True, phase='scoped_preparation')
    scope_guard()
    validate_output_write_guard(guard, contracts)
    return guard, protection


def output_file_identities(guard):
    from agent_core.action_runtime import _output_path_state
    result = {}
    for row in guard.get('targets') or []:
        try:
            result[row['path']] = _output_path_state(Path(row['path']))
        except (OSError, ValueError):
            # Never discard a real process exit if output inspection failed.
            result[row['path']] = {'unverified': True}
    return result
