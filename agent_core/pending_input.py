"""Atomic domain-state mutation and durable commit confirmation.

Pi owns input delivery and session queues.  The legacy queue, input owner and
provider boundary are retired; existing domain callers retain the same CAS and
fsync behavior, including historical receipt dependencies.
"""
import hashlib
import json
import os

from agent_core.plan_tracker import load_plan_state, save_plan_state, plan_state_path, StateCASConflict
from agent_core.state_contract_store import StateContractStore

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def confirm_commit(data_path, task_id):
    """Strictly sync the root's immutable dependency closure, then root/dirs.

    Repeating after a lost ACK/replace-success-sync-error confirms the same ID.
    Errors propagate: a visible root alone is not a durable acknowledgment.
    """
    root = plan_state_path(data_path, task_id)
    store = StateContractStore(data_path, task_id)
    directories, seen = {root.parent}, set()
    def sync_file(path):
        with path.open('rb') as handle: os.fsync(handle.fileno())
        directories.add(path.parent)
    def visit(value, owner_store=store):
        if isinstance(value, dict):
            if value.get('loop_key') and value.get('task_id') and 'sources_ref' in value:
                owner_store=StateContractStore(data_path,value['task_id'])
            for child in value.values(): visit(child, owner_store)
        elif isinstance(value, list):
            for child in value: visit(child, owner_store)
        elif isinstance(value, str) and value.startswith(('object:sha256:', 'audit:sha256:')) and (owner_store.task_id,value) not in seen:
            seen.add((owner_store.task_id,value))
            kind, _, sha = value.split(':')
            path = (owner_store.object_root if kind == 'object' else owner_store.event_root) / (sha + '.json')
            content = json.loads(path.read_text(encoding='utf-8'))
            visit(content, owner_store)
            sync_file(path)
    committed = json.loads(root.read_text(encoding='utf-8'))
    # Strictness is scoped to the newly promised input dependency chain.
    # Historical cross-task read views were already committed by their owner;
    # their embedded references are not local execution objects to relink.
    visit((committed.get('task_state') or {}).get('input_delivery') or {})
    visit(committed.get('conversation_evidence') or {})
    # save_plan_state also publishes an audit head. Its immutable file was
    # fsynced on creation; confirm its directory entry as part of root recovery.
    if store.event_root.exists(): directories.add(store.event_root)
    sync_file(root)
    # Includes newly created task/object directories and their parent entries.
    for directory in list(directories):
        while directory != root.parents[2] and directory != directory.parent:
            directories.add(directory)
            directory = directory.parent
        directories.add(directory)
    for directory in sorted(directories, key=lambda p:len(p.parts), reverse=True):
        fd = os.open(str(directory), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def mutate(data_path, task_id, delta, *, strict=False):
    for _ in range(12):
        state = load_plan_state(data_path, task_id)
        before = digest(state)
        result = delta(state)
        if digest(state) == before:
            if strict: confirm_commit(data_path, task_id)
            return result
        try:
            save_plan_state(data_path, task_id, state)
        except StateCASConflict:
            continue
        if strict: confirm_commit(data_path, task_id)
        return result
    raise StateCASConflict('INPUT_CAS_RETRY_EXHAUSTED')
