"""Exact runtime log authority and existing Skill snapshot drift checks."""
from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import pytest
from agent_core.action_runtime import build_execution_policy_manifest, resolve_preflight_evidence_binding, validate_action_approval_identity, build_action_approval_identity, ActionRuntimeContractError
from agent_core.skill_access import SkillAccessService, new_skill_access_state, SkillAccessError
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import bind_action_skill_resources, validate_action_skill_resources
from tests.test_preflight_mapping_commit import SID, RESOURCES
from tests.test_preflight_recovery_audit import ROOT, PYTHON, approved_tiny, save


def fixture(tmp_path):
    root=tmp_path/'registry';skill=root/SID;skill.mkdir(parents=True)
    source=ROOT/'skills'/SID
    shutil.copyfile(source/'SKILL.md',skill/'SKILL.md');(skill/'scripts').mkdir()
    for ref in RESOURCES:shutil.copyfile(source/ref.split('#')[1],skill/ref.split('#')[1])
    service=SkillAccessService(SkillManager(str(root)))
    access=service.ensure_selected_skill_refs(new_skill_access_state(),[SID],task_revision=1)
    action=bind_action_skill_resources({'skill_resource_refs':RESOURCES,'code':'exact model body'},service,access,step_id='preflight',cwd=tmp_path)
    policy=build_execution_policy_manifest(action_identity={'action_id':'a','plan_id':'p','step_id':'preflight'},
        working_directory=str(tmp_path),language='Bash',skill_resources=action['formal_skill_resources'],evidence_root=str(tmp_path/'attempts'))
    return service,access,action,policy


def test_attempt_paths_and_policy_authority(tmp_path):
    service,access,action,policy=fixture(tmp_path);root=tmp_path/'attempts'
    first=resolve_preflight_evidence_binding(policy,approved_root=root,attempt_id='attempt_1',action_environment=action['env'])
    assert not Path(first['canonical_path']).exists() and not root.exists()
    Path(first['canonical_path']).mkdir(parents=True);old=Path(first['canonical_path'])/'proof';old.write_text('old evidence')
    original=hashlib.sha256(old.read_bytes()).hexdigest()
    second=resolve_preflight_evidence_binding(policy,approved_root=root,attempt_id='attempt_2',action_environment=action['env'])
    assert first['canonical_path']!=second['canonical_path'] and not Path(second['canonical_path']).exists()
    assert hashlib.sha256(old.read_bytes()).hexdigest()==original
    for approved_root,aid,env in [(tmp_path/'attempts-sibling','attempt_3',action['env']),
            (root,'attempt_1',action['env']),(root,'attempt_3',{**action['env'],'BIOMIND_PREFLIGHT_EVIDENCE_DIR':first['canonical_path']})]:
        with pytest.raises(ActionRuntimeContractError):resolve_preflight_evidence_binding(policy,approved_root=approved_root,attempt_id=aid,action_environment=env)
    corrupted=deepcopy(policy);corrupted['evidence_binding']['approved_root']=str(tmp_path/'attempts-sibling')
    with pytest.raises(ActionRuntimeContractError):resolve_preflight_evidence_binding(corrupted,approved_root=root,attempt_id='attempt_4',action_environment={})
    save('directory-policy',{'first':first,'second':second,'policy':policy,'old_evidence_unchanged':True,'model_attempt_ids':0})


@pytest.mark.parametrize('changed',['inspect_wgs_inputs.py','inspect_fasta_header.py','SKILL.md'])
def test_mapping_bound_resource_drift(tmp_path,changed):
    service,access,action,policy=fixture(tmp_path)
    target=service.skills_root/SID/('SKILL.md' if changed=='SKILL.md' else 'scripts/'+changed)
    target.write_text(target.read_text()+'\n# New revision for isolated drift test\n')
    with pytest.raises(SkillAccessError):validate_action_skill_resources(action,service,access,step_id='preflight',cwd=tmp_path)
    new_service=SkillAccessService(SkillManager(str(service.skills_root)))
    new_access=new_service.ensure_selected_skill_refs(new_skill_access_state(),[SID],task_revision=1)
    fresh=bind_action_skill_resources({'skill_resource_refs':RESOURCES,'code':action['code']},new_service,new_access,step_id='preflight',cwd=tmp_path)
    assert fresh['formal_skill_resources']!=action['formal_skill_resources']
    assert fresh['code']==action['code']


def test_real_helper_rejects_wrong_or_existing_leaf(tmp_path):
    async def scenario():
        expected=tmp_path/'attempt_1'/'preflight';expected.parent.mkdir()
        source=tmp_path/'tiny.fq';source.write_text('@x\nA\n+\nI\n')
        helper=ROOT/'skills'/SID/'scripts/inspect_fastq_batch.py'
        results=[]
        for i,path in enumerate([tmp_path/'attempt_1-sibling'/'preflight',expected,expected]):
            if i==1:path.mkdir();(path/'old').write_text('unchanged')
            argv=[PYTHON,str(helper),'--item','x',str(source),'--observation-id','v','--evidence-dir',str(path)]
            body='export BIOMIND_PREFLIGHT_EVIDENCE_DIR='+shlex.quote(str(expected))+'\nexec '+shlex.join(argv)
            out,err,rc=await approved_tiny(tmp_path/f'case-{i}.sh',body)
            assert rc!=0 and not out
            assert ('PREFLIGHT_EVIDENCE_BINDING_DRIFT' if i==0 else 'FileExistsError') in err
            results.append({'rc':rc,'stderr':err})
        assert (expected/'old').read_text()=='unchanged'
        save('directory-helper',results)
    asyncio.run(scenario())
