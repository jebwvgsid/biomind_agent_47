"""Read-only historical conversation projections for existing memory stores.

Pi owns live conversation, branching and input delivery.  These helpers only
read saved context/evidence references; they never create a provider seed,
mutate a conversation cursor or deliver work to the retired control loop.
"""
from copy import deepcopy
import hashlib
import json

CONTEXT_TASK_FIELDS = ('accepted_consultant_handoff', 'work_plan',
    'skill_access_state', 'skill_material_residency', 'selected_skills',
    'consultation_state', 'memory_scope')
CONTEXT_ROOT_FIELDS = ('current_task', 'current_plan', 'steps', 'current_step_id', 'selected_skill_ids')


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def _sources(data_path, cursor):
    from agent_core.state_contract_store import StateContractStore
    if not cursor or not cursor.get('sources_ref'): return {}
    return StateContractStore(data_path,cursor['task_id']).read_object(
        cursor['sources_ref'],expected_type='conversation_context_sources')


def project_state(data_path, task_id, state):
    """Read-only overlay; callers must never save this projected state."""
    view=(state.get('task_state') or {}).get('conversation_view') or {}
    cursor=view.get('execution_cursor')
    if not cursor: return state
    sources=_sources(data_path,cursor)
    projected=deepcopy(state);task=projected.setdefault('task_state',{})
    baseline=view.get('local_field_baseline') or {}
    for level, keys, original in (('root',CONTEXT_ROOT_FIELDS,state),
                                  ('task',CONTEXT_TASK_FIELDS,state.get('task_state') or {})):
        target=projected if level=='root' else task
        for key in keys:
            # A local change after navigation belongs to the new branch. The
            # old global value is not an implicit sibling fallback.
            if _hash(original.get(key)) == baseline.get(level+':'+key, _hash(None)):
                target[key]=deepcopy((sources.get(level) or {}).get(key))
    # Cached summaries from a sibling are never a historical source fallback.
    task['phase_request_views']={k:v for k,v in (task.get('phase_request_views') or {}).items()
        if v.get('selection_revision') == view.get('cursor_revision')}
    return projected


def selected_turns(data_path, task_id, state=None, _seen=None):
    from agent_core.plan_tracker import load_plan_state
    from agent_core.state_contract_store import StateContractStore
    state=load_plan_state(data_path,task_id) if state is None else state
    view=(state.get('task_state') or {}).get('conversation_view') or {}
    local=[{**turn,'source_task_id':task_id} for turn in StateContractStore(data_path,task_id).read_conversation_evidence(state.get('conversation_evidence') or {})]
    cursor=view.get('execution_cursor')
    if not cursor:return local
    seen=set(_seen or ())
    identity=_hash(cursor)
    if identity in seen:raise ValueError('CONVERSATION_SOURCE_CYCLE')
    seen.add(identity)
    sources=_sources(data_path,cursor)
    source_state={'conversation_evidence':cursor.get('evidence') or sources.get('evidence') or {},
        'task_state':{'conversation_view':{'execution_cursor':sources.get('upstream'),
            'local_evidence_base':sources.get('local_evidence_base') or {}}}}
    inherited=selected_turns(data_path,cursor['task_id'],source_state,seen)
    base=int((view.get('local_evidence_base') or {}).get('turn_count') or 0)
    return [*inherited,*[t for t in local if int(t.get('ordinal') or 0)>base]]
