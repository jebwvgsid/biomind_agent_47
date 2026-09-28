"""Positive guidance/discovery acceptance. Synthetic providers, real production seams."""
import asyncio, hashlib, json, os
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.memory_tools import preference_snapshot, preference_path, preference_projection, scope_for, dispatch_memory_tool, accepted_memory_context, effective_memory_context
from agent_core.command_execution import CommandExecutionService
from memory.wiki_memory import WikiMemoryManager

OUT=Path(__file__).resolve().parents[2]/'evidence'
A='PREF_A_7c943a_language_Chinese_explain_concisely'
B='PREF_B_49eb21_language_English_explain_in_detail'
AG='AGENTS_PROJECT_5ed312'
CORE='CORE_PROFILE_8cb019'
LEARNED='LEARNED_AGENT_HISTORY_22b71'
PROJECT='CORE_PROJECT_HISTORY_3daf1'
def record(name, value):
    OUT.mkdir(exist_ok=True)
    (OUT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
def wire(messages):
    return [{'role':m.type,'name':m.name,'id':m.id,'content':m.content,'tool_calls':getattr(m,'tool_calls',[]),'tool_call_id':getattr(m,'tool_call_id',None)} for m in messages]
def text(messages):return '\n'.join(str(m.content) for m in messages)
def capture(scenario,messages,path=None,extra=None):
    raw=text(messages)
    blocks=[m for m in messages if m.name=='current_host_guidance']
    assert len(blocks)==1
    from agent_core.provider_message_safety import assert_v2_llm_messages_clean
    assert_v2_llm_messages_clean(messages)
    row={'scenario':scenario,'provider':'synthetic final LangChain ainvoke capture; real production context and dispatch',
         'messages':wire(messages),'characters':len(raw),'token_count':'NOT_MEASURED; offline character fallback only',
         'markers':{m:raw.count(m) for m in [A,B,AG,CORE,LEARNED,PROJECT]},'source':str(path) if path else None,
         'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None,**(extra or {})}
    with (OUT/'PROVIDER_CONTEXT_WITNESSES.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
    return row
def prefs(ag,root):
    path=root/'editable-preferences.md';path.write_text(A)
    ag.core_memory_store.user_profile.write_text(CORE)
    ag.core_memory_store.agent_memory.write_text(LEARNED)
    ag.core_memory_store.project_path(ag.default_project_id).write_text(PROJECT)
    (root/'AGENTS.md').write_text(AG)
    return path
def handoff(f,i=20,memory=None):
    args={'summary':'Synthetic report, no science execution.','goal':'Produce a report','selected_skills':[fixture.SKILL_B],
          'output_dir':str(f.root/'report'),'outputs':[{'role':'report','artifact_kind':'file','format':'txt'}]}
    if memory:args['memory_context']=memory
    return native('submit_planning_handoff',args,i)












def test_discovery_chinese_pagination_invalidation(tmp_path):
    manager=WikiMemoryManager(None,tmp_path/'wiki')
    assert manager.discover_projects('') == {'hits':[], 'offset':0,'has_more':False,'next_offset':None,
        'coverage':'Current valid Wiki index only; archive-only tasks require source=archive.'}
    scopes=['project-opaque-one','task-opaque-two','default']
    records=[]
    for i,scope in enumerate(scopes):
        result=manager.apply_patch_result({'operations':[{'op':'create_page','path':f'methods/analysis-{i}.md',
            'title':'单细胞差异表达记录'+str(i), 'content':'以前观察的小鼠分析，位置未核实。', 'sources':['synthetic:'+str(i)]}]},scope)
        assert result['written'],result
        records.append(result['written'][0])
    with manager._connect() as conn:
        conn.execute('INSERT INTO memory_project_roots VALUES (?,?)',('/unverified/old-root',scopes[0]))
        conn.execute('INSERT INTO memory_project_roots VALUES (?,?)',('/unverified/old-root','project-another'))
        conn.execute('INSERT INTO memory_project_roots VALUES (?,?)',('/no/knowledge','project-no-pages'));conn.commit()
    page=manager.discover_projects('以前的单细胞结果在哪里',limit=1)
    assert len(page['hits'])==1 and page['has_more']
    pages=[page]
    while pages[-1]['has_more']:pages.append(manager.discover_projects('以前的单细胞结果在哪里',limit=1,offset=pages[-1]['next_offset']))
    hits=[h for p in pages for h in p['hits']]
    assert {h['scope_kind'] for h in hits}=={'project','task_private','unassigned'}
    assert 'project-no-pages' not in str(hits) and not manager.discover_projects('unlikely_no_match')['hits']
    # Actual disk/index version disagreement excludes the page without auto refresh.
    target=Path(records[0]['absolute_path']);original=target.read_text();target.write_text(original+'external')
    assert scopes[0] not in [h['project_id'] for h in manager.discover_projects('')['hits']]
    target.write_text(original)
    with manager._connect() as conn:
        conn.execute('INSERT INTO memory_exclusions VALUES (?,?,?)',(scopes[1],'synthetic:1','test'));conn.commit()
    assert scopes[1] not in [h['project_id'] for h in manager.discover_projects('')['hits']]
    result=manager.apply_patch_result({'operations':[{'op':'update_frontmatter','path':records[2]['path'],'values':{'status':'forgotten'}}]},scopes[2])
    assert result['status'] in {'written','noop'},result
    assert scopes[2] not in [h['project_id'] for h in manager.discover_projects('')['hits']]
    record('discovery-pagination',{'pages':pages,'after_exclusion_and_forget':manager.discover_projects('')})










