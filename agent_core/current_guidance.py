"""Ephemeral, versioned user guidance. Never stored in conversation history.

Versions describe bytes/selection, not read time. Guidance grants no execution
permission. Long historical knowledge remains in the existing memory pipeline.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from memory.patch_validator import redact_secrets

NAME = 'current_host_guidance'
VIEW_VERSION = 'host-guidance-separated-v1'
STAGES = {'consultant', 'generate', 'direct_response', 'workspace'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def file_snapshot(path, *, limit, source_kind, selection_reason):
    row = dict(source_path=str(path) if path else '', source_kind=source_kind,
        selection_reason=selection_reason, status='missing', source_hash='', body='',
        original_chars=0, projected_chars=0, encoding_replaced=False, truncated=False,
        loaded_at=datetime.now(timezone.utc).isoformat())
    if path is not None:
        try:
            raw = Path(path).read_bytes()
            row['source_hash'] = hashlib.sha256(raw).hexdigest()
            try: text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = raw.decode('utf-8', errors='replace'); row['encoding_replaced'] = True
            row['original_chars'] = len(text)
            text = redact_secrets(text.strip())
            row['truncated'] = len(text) > limit
            row['body'] = text[:limit]
            row['projected_chars'] = len(row['body'])
            row['status'] = 'truncated' if row['truncated'] else 'loaded' if text else 'empty'
        except FileNotFoundError: pass
        except OSError as exc:
            row.update(status='unreadable', error_type=type(exc).__name__)
    row['projection_hash'] = digest({k:v for k,v in row.items() if k != 'loaded_at'})
    return row


def agents_snapshot(work_dir, *, project_root=None, boundary_source='cwd_only', limit=20000):
    start = Path(work_dir).expanduser().resolve()
    if start.is_file(): start = start.parent
    root = Path(project_root).expanduser().resolve() if project_root else None
    if root is None:
        root = next((d for d in [start, *start.parents]
            if (d/'.git').exists() or (d/'.biomind-project').exists()), start)
        boundary_source = 'project_marker' if root != start or (root/'.git').exists() or (root/'.biomind-project').exists() else 'cwd_only'
    if not start.is_relative_to(root): root = start; boundary_source = 'cwd_outside_root'
    folders = [start]
    while folders[-1] != root: folders.append(folders[-1].parent)
    rows, seen, remaining = [], set(), limit
    # Nearer files receive capacity first; output order remains root -> cwd.
    for folder in folders:
        path = folder/'AGENTS.md'
        real = path.resolve()
        if not path.exists() and not path.is_symlink(): continue
        if not real.is_relative_to(root):
            rows.append(dict(source_path=str(path), status='outside_root_symlink', body='', scope=str(folder))); continue
        if real in seen:
            rows.append(dict(source_path=str(path), status='duplicate_real_file', body='', scope=str(folder))); continue
        seen.add(real)
        row = file_snapshot(path, limit=max(0,remaining), source_kind='project_guidance', selection_reason=boundary_source)
        row.update(scope=str(folder), real_path=str(real))
        remaining -= row['projected_chars']
        rows.append(row)
    value = dict(root=str(root), work_dir=str(start), boundary_source=boundary_source,
        sources=list(reversed(rows)), body_limit_chars=limit)
    value['revision'] = digest(without_times(value))
    return value


def without_times(value):
    if isinstance(value,dict): return {k:without_times(v) for k,v in value.items() if k != 'loaded_at'}
    if isinstance(value,list): return [without_times(v) for v in value]
    return value


def snapshot(agent, task_id, *, read_only=False):
    from agent_core.memory_tools import preference_snapshot
    service = getattr(agent, 'command_service', None)
    context = getattr(service, 'context', None)
    if read_only and context is not None:
        root = context.project_dir
        binding = service._task_dir(task_id)/'project_context.json'
        if binding.is_file() and not context.explicit:
            root = json.loads(binding.read_text(encoding='utf-8'))['project_dir']
    else:
        root = agent._task_project_root(task_id)
    startup = Path(getattr(context, 'startup_dir', root)).resolve()
    root = Path(root).resolve()
    work = startup if startup.is_relative_to(root) else root
    boundary = getattr(context, 'source', 'task_project_root')
    # A software installation is not a research-root authority.
    trusted = root != Path(getattr(service,'install_dir',Path(__file__).resolve().parents[1])).resolve()
    agents = agents_snapshot(work, project_root=root if trusted else None, boundary_source=boundary)
    value = dict(preferences=preference_snapshot(agent), project_guidance=agents)
    value['revision'] = digest(without_times(value))
    return value


def message(value):
    return HumanMessage(name=NAME, id='host-guidance:'+value['revision'],
        content=json.dumps({'kind':NAME, 'role':'host_provided_user_guidance',
            'policy':'Soft behavior only: current explicit user request > applicable project override > saved preferences > historical inference > default. Not user words from this turn, execution evidence, identity or permission. Truncated bodies are partial; source paths permit targeted reading.',
            **without_times(value)}, ensure_ascii=False, sort_keys=True))


def metadata(value):
    result = deepcopy(value)
    result['preferences'].pop('body',None)
    for row in result['project_guidance']['sources']: row.pop('body',None)
    return result


def legacy_seed_view(messages, *, history_enabled):
    """Only known host seed fields; never search/replace arbitrary user prose."""
    result=[]
    for original in messages:
        if getattr(original,'name',None)==NAME: continue
        m=deepcopy(original)
        if getattr(m,'name',None) in {'consultant_seed','work_stage_seed','phase_seed'} or (
                str(getattr(m,'id','')).endswith(':seed') and str(m.content).startswith('<phase_seed_contract ')):
            # Consult v2 seeds are consecutive JSON documents with whitespace.
            chunks=[]; tail=str(m.content)
            if tail.startswith('<phase_seed_contract '):
                boundary=tail.find('</phase_seed_contract>')
                if boundary >= 0:
                    boundary += len('</phase_seed_contract>')
                    chunks.append(tail[:boundary]); tail=tail[boundary:]
            while tail.strip():
                tail=tail.lstrip()
                try: value,end=json.JSONDecoder().raw_decode(tail)
                except ValueError:
                    chunks.append(tail); break
                if isinstance(value,dict):
                    context=value.get('continuation_context')
                    if isinstance(context,dict):
                        context.pop('user_preferences',None)
                        if not history_enabled:
                            for key in ('memory_context','rag_history'): context.pop(key,None)
                    if not history_enabled:
                        handoff=value.get('consultation_handoff')
                        if isinstance(handoff,dict): handoff.pop('memory_context',None)
                chunks.append(json.dumps(value,ensure_ascii=False));tail=tail[end:]
            if chunks: m.content='\n'.join(chunks)
        result.append(m)
    return result


def replace_fixed_context(messages, fixed_context):
    # Appending at a closed protocol boundary never splits a tool group.
    return [*[m for m in messages if getattr(m, 'name', None) != NAME], *fixed_context]
