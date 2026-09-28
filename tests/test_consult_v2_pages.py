"""Exact Skill pages at reader and actual provider boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from agent_core.skill_manager import SkillManager
from agent_core.skill_access import SkillAccessService, SkillAccessError
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture


def reader(tmp_path,text=None):
    root=tmp_path/'skills';package=root/'local'/'procedure';package.mkdir(parents=True)
    content=text or '---\nname: procedure\ndescription: Read exact source pages\n---\n# Procedure\n'+''.join(f'line {i} '+('quoted "value" '*20)+'\n' for i in range(160))
    (package/'SKILL.md').write_text(content)
    manager=SkillManager(str(root));service=SkillAccessService(manager)
    state,docs=service.load_skill_documents({},['local/procedure'],task_revision=0,max_new_chars=6000)
    return service,state,docs,content,package


def assert_exact(page,content):
    assert page['exact_text']==content[page['source_start_char']:page['source_end_char']]
    assert '[omitted middle' not in page['exact_text']
    assert page['body_delivered']==bool(page['exact_text'])


def test_T03_T04_catalog_pagination_and_missing_root(tmp_path):
    root=tmp_path/'catalog';root.mkdir()
    for i in range(31):
        package=root/f'skill-{i:02d}';package.mkdir()
        (package/'SKILL.md').write_text(f'---\nname: skill-{i:02d}\ndescription: Method {i} purpose\n---\nInstructions\n')
    manager=SkillManager(str(root));first=manager.catalog_page();second=manager.catalog_page(**first['next_request'])
    assert first['shown']==24 and second['shown']==7
    assert len({r['skill_id'] for r in first['cards']+second['cards']})==31
    assert first['has_more'] and not second['has_more'] and first['catalog_complete']
    missing=SkillManager(str(tmp_path/'missing')).catalog_page()
    assert missing['cards']==[] and not missing['catalog_complete'] and missing['catalog_errors']


def test_T05_cache_hit_full_and_range_have_executable_continuations(tmp_path):
    service,state,docs,content,_=reader(tmp_path)
    page=docs[0]['provider_excerpt'];collected=[]
    for _ in range(40):
        assert_exact(page,content);collected.append(page['exact_text'])
        if not page['has_more']:break
        previous=page['source_end_char'];page=service._serve_one(state,page['next_request'],'continuation')
        assert page['source_start_char']==previous
    else:pytest.fail('no page convergence')
    assert ''.join(collected)==content
    state,cached=service.load_skill_documents(state,['local/procedure'],task_revision=0,max_new_chars=6000)
    assert cached[0]['cache_hit']
    assert cached[0]['provider_excerpt']['exact_text']==docs[0]['provider_excerpt']['exact_text']
    assert len(json.dumps(cached[0]['provider_excerpt'],ensure_ascii=False))<6000
    ranged=service._serve_one(state,{'skill_id':'local/procedure','operation':'range','start_line':7,'end_line':20},'range')
    assert_exact(ranged,content)
    assert ranged['requested_start_line']==7 and ranged['requested_end_line']==20


def test_T06_distant_search_long_line_resource_and_versions(tmp_path):
    single='x'*23000
    content='---\nname: procedure\ndescription: exact pages\n---\n# Start\nNEEDLE first\n'+single+'\n'+'filler\n'*120+'NEEDLE last\n'
    service,state,docs,content,package=reader(tmp_path,content)
    hit=service._serve_one(state,{'skill_id':'local/procedure','operation':'search','query':'NEEDLE'},'search')
    assert len(hit['matched_ranges'])==2 and hit['requested_end_line']<20
    assert_exact(hit,content)
    req={'skill_id':'local/procedure','operation':'range','start_line':7,'end_line':7}
    collected=[]
    for _ in range(10):
        page=service._serve_one(state,req,'line');assert_exact(page,content);collected.append(page['exact_text'])
        if not page['has_more']:break
        req=page['next_request']
    assert ''.join(collected)==single+'\n'
    old_snapshot=docs[0]['snapshot_id'];(package/'SKILL.md').write_text(content+'new version\n')
    old=service._serve_one(state,{'skill_id':'local/procedure','operation':'full','snapshot_id':old_snapshot},'old')
    assert old['snapshot_id']==old_snapshot;assert_exact(old,content)
    resource=package/'references'/'example.txt';resource.parent.mkdir();resource.write_text(single+'\nend\n')
    req={'skill_id':'local/procedure','operation':'resource','resource_path':'references/example.txt'}
    page=service._serve_one(state,req,'resource');assert_exact(page,single+'\nend\n')
    resource.write_text('new exact resource\n')
    with pytest.raises(SkillAccessError,match='SKILL_RESOURCE_CHANGED'):service._serve_one(state,page['next_request'],'changed')
    fresh=service._serve_one(state,{**req,'refresh_resource':True},'refresh')
    assert fresh['exact_text']=='new exact resource\n' and fresh['resource_content_hash']!=page['resource_content_hash']
    again=service._serve_one(state,req,'new-cache')
    assert again['exact_text']=='new exact resource\n'
    with pytest.raises(SkillAccessError,match='SKILL_RESOURCE_CHANGED'):
        service._serve_one(state,page['next_request'],'old-page-after-explicit-refresh')






def test_T17_pre_v2_excerpt_cache_identity_remains_readable(tmp_path):
    from agent_core.skill_access import _digest, _stable_id
    service,state,docs,source,_=reader(tmp_path,'---\nname: procedure\ndescription: legacy method\n---\n# Method\nLegacy source body.\n')
    snapshot=state['snapshots'][docs[0]['snapshot_id']]
    req={'skill_id':'local/procedure','operation':'range','start_line':6,'end_line':6}
    exact='Legacy source body.'
    excerpt_id=_stable_id('skill-excerpt',{'snapshot_id':snapshot['snapshot_id'],'request':req,'content_hash':_digest(exact)})
    cache_key=_digest({'snapshot_id':snapshot['snapshot_id'],'operation':'range','heading':None,'query':'','start_line':6,'end_line':6,'resource_path':''})
    state['excerpts'][excerpt_id]={'excerpt_id':excerpt_id,'snapshot_id':snapshot['snapshot_id'],'skill_id':'local/procedure',
        'operation':'range','start_line':6,'end_line':6,'content_hash':_digest(exact),'exact_text':exact,'request_id':'pre-v2','request_spec':req}
    state['excerpt_cache'][cache_key]=excerpt_id
    assert service._verified_excerpt(state,excerpt_id)['exact_text']==exact
