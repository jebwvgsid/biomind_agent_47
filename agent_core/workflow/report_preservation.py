"""Exact, user-authorized report preservation using the existing mutation journal.

Consult only projects prerequisites. Approved execution freezes each source
identity and destination before no-clobber rename; retries verify that identity.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import stat

from agent_core.plan_tracker import load_plan_state, save_plan_state, StateCASConflict, plan_approval_is_current
from agent_core.workspace_attestation import (WorkspaceAdmission, prepare_mutation_intents,
    start_mutation_attestation, finalize_mutation_attestation, persist_mutation_journal,
    load_mutation_journal, _digest)


def preservation_execution_allowed(state, revision):
    """Use the active workflow's existing execution admission; never add review."""
    if (state.get("task_state") or {}).get("status") == "cancelled" or state.get("task_status") == "cancelled":
        return False
    if plan_approval_is_current(state, revision):
        return True
    admission = (state.get('task_state') or {}).get('intent_admission') or {}
    if (state.get('generated_from_task_revision') != revision or state.get('plan_status') != 'ready'
            or not state.get('plan_snapshot_ref') or not state.get('current_step_id')
            or not state.get('steps') or any(row.get('step_protocol_version') != 3 for row in state['steps'])
            or admission.get('request_kind') != 'execute'
            or admission.get('execution_scope') not in {'simple', 'managed'}):
        return False
    approval = state.get('current_approval') or {}
    return (not admission.get('review_before_execution') or
        (approval.get('status') == 'approved' and approval.get('plan_snapshot_ref') == state.get('plan_snapshot_ref')))


def _error(code):
    from agent_core.workflow.runtime_recovery_reporting import RuntimeReportError
    return RuntimeReportError(code)


def preservation_authorization(state, paths):
    """Accept host-resolved authorization only for exact declared report paths."""
    auth = dict((state.get('task_state') or {}).get('report_preservation_authorization') or {})
    allowed = set(map(str, paths))
    requested = auth.get('paths') or []
    if (auth.get('operation') != 'preserve_reports_date_suffix'
            or auth.get('source') != 'user_explicit' or not auth.get('source_ref')
            or auth.get('naming', 'date_suffix') != 'date_suffix'
            or not isinstance(requested, list) or not requested
            or any(not isinstance(p, str) or p not in allowed for p in requested)):
        return {}
    return auth


def preservation_prerequisites(state, paths, conflicts):
    auth = preservation_authorization(state, paths)
    if not auth:
        return []
    suffix = str(auth.get('date_suffix') or datetime.now().strftime('%Y%m%d'))
    if not re.fullmatch(r'\d{8}', suffix):
        return []
    result = []
    for row in conflicts:
        if row['status'] != 'conflict' or row['path'] not in auth['paths']:
            continue
        source = Path(row['path'])
        # A protected directory/symlink is not a report that may be preserved.
        from agent_core.workflow.runtime_recovery_reporting import _open_directory
        parent = _open_directory(source.parent)
        try:
            info = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            index = 0
            while True:
                name = source.name + '.' + suffix + ('.' + str(index) if index else '')
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    break
                index += 1
            target = str(source.with_name(name))
        finally:
            os.close(parent)
        identity = hashlib.sha256(json.dumps([state.get('task_id'), auth['source_ref'], str(source)], ensure_ascii=False).encode()).hexdigest()
        result.append({'operation': 'preserve_reports_date_suffix', 'operation_id': identity,
            'source': str(source), 'target': target, 'authorization_ref': auth['source_ref'],
            'status': 'planned', 'ownership': 'unknown_or_other_task'})
    return result


def _identity(parent, name):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_UNVERIFIED')
        body = b''
        while len(body) <= 1024 * 1024:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_CHANGED')
        return {'device': before.st_dev, 'inode': before.st_ino, 'size': before.st_size,
            'mtime_ns': before.st_mtime_ns, 'sha256': hashlib.sha256(body).hexdigest()}
    finally:
        os.close(descriptor)


def _save_rows(data_path, task_id, scope_guard, transform):
    for _ in range(3):
        state = load_plan_state(data_path, task_id)
        if state.get('task_id') != task_id or not scope_guard(state):
            raise _error('RUNTIME_REPORT_SCOPE_CHANGED')
        rows = state.setdefault('task_state', {}).setdefault('report_preservation_operations', {})
        transform(rows)
        try:
            save_plan_state(data_path, task_id, state)
        except StateCASConflict:
            continue
        return deepcopy(rows)
    raise _error('RUNTIME_REPORT_RECEIPT_CAS_CONFLICT')


def _rename_noreplace(parent, source, target):
    """Linux renameat2: one-file atomic move that never replaces a competitor."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise _error('RUNTIME_REPORT_NO_CLOBBER_RENAME_UNAVAILABLE')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(source), parent, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise _error('RUNTIME_REPORT_PRESERVATION_TARGET_CHANGED')
        raise OSError(code, os.strerror(code))


def _perform_one(data_path, task_id, operation, scope_guard):
    from agent_core.workflow.runtime_recovery_reporting import _open_directory
    state = load_plan_state(data_path, task_id)
    revision = int((state.get('current_task') or {}).get('task_revision') or state.get('task_revision') or 0)
    auth = (state.get('task_state') or {}).get('report_preservation_authorization') or {}
    if (not scope_guard(state) or not preservation_execution_allowed(state, revision)
            or revision != operation['plan_revision']
            or auth.get('source_ref') != operation['authorization_ref']
            or operation['source'] not in (auth.get('paths') or [])):
        raise _error('RUNTIME_REPORT_SCOPE_CHANGED')
    source, target = Path(operation['source']), Path(operation['target'])
    parent = _open_directory(source.parent)
    journal_path = Path(data_path) / 'task_outputs' / task_id / 'mutation_journals' / ('report-preserve-' + operation['operation_id'] + '.json')
    try:
        expected = operation['source_identity']
        current, backup = _identity(parent, source.name), _identity(parent, target.name)
        if backup is not None and backup != expected:
            if not journal_path.exists() and current == expected:
                # A competitor arrived after planning/freezing but before any
                # operation started. Select within the same approved suffix
                # family; a started operation's destination is immutable.
                match = re.fullmatch(re.escape(source.name) + r'(\.\d{8})(?:\.\d+)?', target.name)
                if not match:
                    raise _error('RUNTIME_REPORT_PRESERVATION_TARGET_CHANGED')
                index = 1
                while True:
                    name = source.name + match.group(1) + '.' + str(index)
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        break
                    index += 1
                updated = deepcopy(operation)
                updated['target'] = str(source.with_name(name))
                updated['frozen_scope']['target'] = updated['target']
                def change(rows):
                    if rows.get(operation['operation_id']) != operation:
                        raise _error('RUNTIME_REPORT_SCOPE_CHANGED')
                    rows[operation['operation_id']] = updated
                _save_rows(data_path, task_id, scope_guard, change)
                return _perform_one(data_path, task_id, updated, scope_guard)
            raise _error('RUNTIME_REPORT_PRESERVATION_TARGET_CHANGED')
        if operation['status'] == 'completed':
            if backup != expected:
                raise _error('RUNTIME_REPORT_PRESERVATION_BACKUP_UNVERIFIED')
            return operation  # A new report at source is now permitted.
        if current is not None and current != expected:
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_CHANGED')
        if current is None and backup is None:
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_UNVERIFIED')
        if journal_path.exists():
            journal = load_mutation_journal(journal_path)
            if journal.get('report_preservation') != operation['frozen_scope']:
                raise _error('RUNTIME_REPORT_PRESERVATION_JOURNAL_SCOPE_CHANGED')
        else:
            if current != expected or backup is not None:
                raise _error('RUNTIME_REPORT_PRESERVATION_JOURNAL_MISSING')
            admission = WorkspaceAdmission(status='passed', mutation_intents=({'formal_concrete_path': str(target),
                'canonical_resource_id': 'report:' + operation['operation_id'], 'resource_kind': 'file',
                'operation': 'preserve_report', 'mutation': 'rename',
                'evidence_contract': {'postconditions': [{'kind': 'file_exists', 'relative_path': ''}]}},))
            intents = prepare_mutation_intents(admission, task_id=task_id, plan_id=operation['plan_id'],
                plan_revision=operation['plan_revision'], step_id='report-preservation',
                action_id=operation['operation_id'], action_digest=_digest(operation['frozen_scope']), action={})
            journal = start_mutation_attestation(intents, attempt_id=operation['operation_id'])
            journal['report_preservation'] = deepcopy(operation['frozen_scope'])
            journal['journal_digest'] = _digest({k: v for k, v in journal.items() if k != 'journal_digest'})
            persist_mutation_journal(journal_path, journal)
        if backup is None:
            _rename_noreplace(parent, source.name, target.name)
            os.fsync(parent)
        if _identity(parent, target.name) != expected:
            # A concurrently changed source was moved, never deleted. Restore
            # it only if the original name is still free, without overwrite.
            if _identity(parent, source.name) is None:
                _rename_noreplace(parent, target.name, source.name)
                os.fsync(parent)
            raise _error('RUNTIME_REPORT_PRESERVATION_BACKUP_UNVERIFIED')
        if _identity(parent, source.name) is not None:
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_CHANGED')
        # Crash after move but before finalization is recognized by the frozen
        # inode/content proof plus started journal, never mere path presence.
        if journal.get('status') != 'completed':
            journal = finalize_mutation_attestation(journal, process_exit_code=0)
            persist_mutation_journal(journal_path, journal)
        if journal.get('status') != 'completed':
            raise _error('RUNTIME_REPORT_PRESERVATION_ATTESTATION_UNCONFIRMED')
        completed = {**operation, 'status': 'completed', 'journal_path': str(journal_path),
            'journal_digest': journal['journal_digest']}
        _save_rows(data_path, task_id, scope_guard, lambda rows: rows.__setitem__(operation['operation_id'], completed))
        return completed
    finally:
        os.close(parent)


def execute_report_preservation(data_path, task_id, *, scope_guard):
    """Approved execution hook; all scoped old reports precede any new report."""
    from agent_core.workflow.runtime_recovery_reporting import _requested_reports, report_publication_preflight
    state = load_plan_state(data_path, task_id)
    revision = int((state.get('current_task') or {}).get('task_revision') or state.get('task_revision') or 0)
    if not scope_guard(state) or not preservation_execution_allowed(state, revision):
        raise _error('RUNTIME_REPORT_PRESERVATION_PLAN_APPROVAL_REQUIRED')
    root = Path(data_path) / 'task_outputs' / task_id
    paths = _requested_reports(state, root)
    auth = preservation_authorization(state, paths.values())
    if not auth:
        if (state.get('task_state') or {}).get('report_preservation_authorization'):
            raise _error('RUNTIME_REPORT_PRESERVATION_AUTHORIZATION_INVALID')
        return []
    existing = deepcopy((state.get('task_state') or {}).get('report_preservation_operations') or {})
    preflight = report_publication_preflight(state, root)
    if not preflight.get('planning_allowed'):
        raise _error('RUNTIME_REPORT_PRESERVATION_SCOPE_UNRESOLVED')
    operations = list(preflight.get('prerequisites') or [])
    # Freeze the entire set before the first file move, so partial execution
    # keeps exactly the original destinations on recovery.
    for operation in operations:
        if operation['operation_id'] in existing:
            continue
        source = Path(operation['source'])
        from agent_core.workflow.runtime_recovery_reporting import _open_directory
        parent = _open_directory(source.parent)
        try:
            identity = _identity(parent, source.name)
        finally:
            os.close(parent)
        if identity is None:
            raise _error('RUNTIME_REPORT_PRESERVATION_SOURCE_CHANGED')
        operation.update(source_identity=identity, plan_id=str(state.get('plan_id') or 'report-preservation'), plan_revision=revision)
        operation['frozen_scope'] = {k: deepcopy(operation[k]) for k in ('operation_id', 'source', 'target', 'authorization_ref', 'source_identity', 'plan_id', 'plan_revision')}
        existing[operation['operation_id']] = operation
    rows = _save_rows(data_path, task_id, scope_guard,
        lambda rows: [rows.setdefault(key, value) for key, value in existing.items()])
    results = []
    for operation in rows.values():
        if operation.get('authorization_ref') != auth['source_ref'] or operation.get('source') not in auth['paths']:
            raise _error('RUNTIME_REPORT_PRESERVATION_AUTHORIZATION_CHANGED')
        results.append(_perform_one(data_path, task_id, operation, scope_guard))
    return results
