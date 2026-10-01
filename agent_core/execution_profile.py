"""Frozen host execution configuration in the existing per-session task store.

This is configuration/lifecycle bookkeeping, not a sandbox against the same UID.
Pi still owns session identity and history; tree navigation cannot roll it back.
"""
import os
from pathlib import Path
import time

from agent_core.pending_input import mutate
from agent_core.plan_tracker import load_plan_state

PROFILES = frozenset({'native', 'protected'})
SCHEMA = 1


def profile_value(value):
    if value not in PROFILES:
        raise ValueError('EXECUTION_PROFILE_INVALID')
    return value


def _process_identity(pid):
    try:
        # PID plus kernel start time avoids interpreting PID reuse as a live host.
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _live_hosts(binding):
    return [p for p in binding.get('hosts', [])
            if p.get('start') and _process_identity(p.get('pid')) == p['start']]


def bind_host_profile(service, selected, *, source='host', existing=False, migrate=False):
    """Called only by host process construction, never by a model API method."""
    selected = profile_value(selected)
    host = {'pid': os.getpid(), 'start': _process_identity(os.getpid())}

    def update(state):
        task = state.setdefault('task_state', {})
        old = task.get('pi_execution_profile')
        if old is not None and (not isinstance(old, dict) or old.get('schema') != SCHEMA):
            raise ValueError('EXECUTION_PROFILE_METADATA_UNSUPPORTED')
        prior = profile_value(old['profile']) if old else ('protected' if existing or task.get('pi_request_id') else None)
        if old and (old.get('session_id') != service.session_id or old.get('project') != str(service.cwd)):
            raise ValueError('EXECUTION_PROFILE_OWNER_MISMATCH')
        hosts = _live_hosts(old or {})
        changing = prior is not None and prior != selected
        if changing:
            if not migrate:
                raise ValueError(f'EXECUTION_PROFILE_RESTART_REQUIRED: session={prior}; requested={selected}')
            claims = task.get('execution_permissions', {}).get('requests', {}).values()
            # Unsettled effects and a still-connected host require explicit
            # shutdown/reconciliation. Never cancel someone else's work here.
            if hosts or service.active_jobs() or service.formal.active_runs(service.task_id,None) or any(r.get('claim') and not r.get('result') for r in claims):
                raise ValueError('EXECUTION_PROFILE_MIGRATION_BUSY')
            for row in task.get('execution_permissions', {}).get('requests', {}).values():
                if row.get('status') in {'pending', 'allowed'} and not row.get('claim'):
                    row.update(status='cancelled', reason='execution_profile_migration', migration_from=prior)
            if task.get('pi_request_id'):
                task['pi_pause'] = {'reason': 'execution_profile_migration_requires_new_request',
                                    'request_id': task['pi_request_id']}
                task['pi_profile_requires_new_request'] = task['pi_request_id']
            task.setdefault('pi_profile_migrations', []).append(
                {'from': prior, 'to': selected, 'source': source, 'at': time.time()})
        if host not in hosts:
            hosts.append(host)
        if len(hosts) > 64:
            raise ValueError('EXECUTION_PROFILE_HOST_LIMIT')
        binding = {**(old or {}), 'schema': SCHEMA, 'profile': selected,
                   'session_id': service.session_id, 'project': str(service.cwd),
                   'source': source if changing or not old else old['source'], 'hosts': hosts}
        task['pi_execution_profile'] = binding
        return dict(binding)

    return mutate(service.data_root, service.task_id, update)


def require_host_profile(service):
    task = load_plan_state(service.data_root, service.task_id).get('task_state', {})
    binding = task.get('pi_execution_profile', {})
    if binding.get('schema') != SCHEMA or binding.get('profile') != service.execution_profile:
        raise ValueError('EXECUTION_PROFILE_CHANGED_RESTART_REQUIRED')
    return binding


def release_host_profile(service):
    def update(state):
        binding = state.get('task_state', {}).get('pi_execution_profile')
        if binding:
            binding['hosts'] = [p for p in _live_hosts(binding) if p.get('pid') != os.getpid()]
    mutate(service.data_root, service.task_id, update)
