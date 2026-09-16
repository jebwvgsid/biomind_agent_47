import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage,HumanMessage
from agent_core.plan_tracker import load_plan_state,commit_runtime_evidence_state
from agent_core.runtime_evidence import make_runtime_evidence
from agent_core.workflow.recovery_runtime import RecoveryResourcePolicy
from tests.a1_unified_fix.test_f6_graph import F6Graph,SID,save

async def drive(f, skill_id=SID):
    import bio_agents
    runner=f.case._runner_state(f.task);runner.update(workflow_routing_schema_version=2,messages=[HumanMessage(content=f.goal)])
    scope=f.agent._runtime_evidence_scope(skill_id)
    evidence=make_runtime_evidence({'skill_id':skill_id,'checked_env':scope['environment_id'],'status':'ready',
        'resolved_runtime_paths':{'RESOLVED_JAVA_BIN':str(f.java),'RESOLVED_SNPEFF_JAR':str(f.jar)}},
        task_id=f.task,skill_scope=scope,observed_at=datetime.now(timezone.utc).isoformat(),source_ref='phase-e-new-synthetic-runtime')
    commit_runtime_evidence_state(f.root,f.task,[evidence])
    app=f.agent._app_for_task(f.task,runner);config={'configurable':{'thread_id':f.task},'recursion_limit':90}
    incoming=runner
    async def callbacks():
        while True:await asyncio.sleep(.001)
    f.service=asyncio.create_task(callbacks())
    with patch.object(bio_agents,'run_script_path',f.execute):
        for _ in range(12):
            async for event in app.astream(incoming,config=config,stream_mode='updates'):
                f.events.append(event)
                if 'commit_step' in event:f.commits.append(deepcopy(event['commit_step']))
            snap=app.get_state(config)
            if not snap.next:break
            assert snap.tasks and snap.tasks[0].interrupts
            incoming=bio_agents.Command(resume='y')
        else:raise AssertionError('GRAPH_NOT_BOUNDED')
    snapshot=app.get_state(config)
    f.values=snapshot.values
    f.terminal=await f.agent._finalize_managed_terminal(f.task,snapshot)
    f.durable=load_plan_state(f.root,f.task)
    return snapshot

class MissingThenRepair(F6Graph):
    def __init__(self,mode='e-missing-repair'):
        super().__init__(mode=mode,variant='E 新布局 中文');self.prepares=0
    def action(self,operation,messages):
        value=super().action(operation,messages)
        if operation=='prepare_database':
            self.prepares+=1
            if self.prepares==1:value['argv'][-1]=value['argv'][-1].replace('print(emit_effect_evidence(', '(db/"data"/genome/"genes.gtf").unlink()\nprint(emit_effect_evidence(')
        return value
    class Recovery(F6Graph.Recovery):
        async def ainvoke(self,messages,**kwargs):
            value=await super().ainvoke(messages,**kwargs)
            value.tool_calls[0]['args']['action']='retry_step'
            value.usage_metadata={'input_tokens':4000,'output_tokens':100,'total_tokens':4100}
            return value


def test_chain2_missing_file_first_cause_new_approval_and_commit():
    async def scenario():
        f=MissingThenRepair()
        with patch.dict(os.environ,BIOMIND_RECOVERY_RESOURCE_POLICY='phase_e_candidate_v2'):
            await f.setup()
        assert f.recovery_node.resource_policy==RecoveryResourcePolicy()
        try:
            await drive(f)
            assert f.durable['task_state']['status']=='completed',f.values.get('recovery_outcome')
            assert len(f.executions)==len(f.approvals)==7 and len(f.commits)==6
            assert f.prepares==2 and f.counts['recovery']==1
            wire=next(r for r in f.requests if r['stage']=='recovery')
            assert 'WORKSPACE_MUTATION_NOT_ATTESTED' in json.dumps(wire)
            assert 'formal_target_path' in json.dumps(wire)
            attempts=[e for e in f.executions if e['operation']=='prepare_database']
            assert attempts[0]['attempt_id']!=attempts[1]['attempt_id']
            assert attempts[0]['approved_sha256']!=attempts[1]['approved_sha256']
            save('e-chain2',{'calls':f.counts,'approvals':len(f.approvals),'executions':len(f.executions),'commits':len(f.commits),
                'first_request':wire,'prepare_attempts':attempts,'production_overrides':[]})
        finally:await f.close()
    asyncio.run(scenario())

class ParserFailure(F6Graph):
    def __init__(self):super().__init__(mode='e-parser')
    def action(self,operation,messages):
        value=super().action(operation,messages)
        if operation=='check_contig_compatibility':
            source=value['argv'][-1]
            start=source.index('print(emit_effect_evidence(')
            value['argv'][-1]=source[:start]+'print("BIOMIND_EFFECT_EVIDENCE_JSON={broken")\nPY\n'
        return value


def test_v06_real_effect_parser_failure_without_structured_receipt():
    async def scenario():
        f=ParserFailure();await f.setup()
        try:
            await drive(f)
            assert len(f.executions)==1 and f.executions[0]['rc']==0 and not f.commits
            assert f.counts['recovery']==1
            wire=next(r for r in f.requests if r['stage']=='recovery')
            assert 'EFFECT_EMISSION_JSON_INVALID' in json.dumps(wire),wire
            assert '{broken' in f.executions[0]['stdout']
            save('e-parser-failure',{'counts':f.counts,'wire':wire,'executed':f.executions,'events':f.events},full=True)
        finally:await f.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',['scalar','missing'])
def test_v20_non_snpeff_operations_complete_real_graph(tmp_path,failure):
    """Reuse F6's external fixture only; register a distinct public Skill.

    Changed operation/capability IDs, binding name, environment, layout and
    scalar are exercised through every real graph owner and synthetic process.
    No production source or admission/validation result is rewritten.
    """
    import types
    import config.config as inert_config
    import tests.a1_unified_fix.test_f6_graph as original
    replacements=[('genomics/snpeff-annotation','fixture/bundle'),('genomics.snpeff_annotation','fixture.bundle'),
        ('prepare_database','prepare_bundle'),('build_database','compile_bundle'),('annotate_variants','apply_bundle'),
        ('check_contig_compatibility','validate_bundle_inputs'),('summarize_ann','summarize_bundle'),('finalize_outputs','deliver_bundle'),
        ('genome_version','dataset_label'),('GENOME_VERSION','DATASET_LABEL'),('SNPEFF_DB','BUNDLE_DIR'),
        ('snpeff_data_dir','bundle_workspace'),('snpeff_db','bundle_ref'),('new_fixture_v2','release_73'),
        ('data/genomes/','catalog/sequences/'),('data/','catalog/'),('relative_path: data}', 'relative_path: catalog}'),
        ('"data"','"catalog"'),("'data'","'catalog'"),('"genomes"','"sequences"'),('genes.gtf','features.gtf')]
    def translate(value):
        for old,new in replacements:value=value.replace(old,new)
        return value
    skill=Path(original.__file__).parents[2]/'skills/genomics/snpeff-annotation/SKILL.md'
    # Source is the allowlisted public Skill, never a host task/input/config.
    target=tmp_path/'public_skills/fixture/bundle/SKILL.md';target.parent.mkdir(parents=True)
    target.write_text(translate(skill.read_text()).replace('name: snpeff-annotation','name: bundle'))
    source=Path(original.__file__).read_text().split('def test_IT01_')[0]
    module=types.ModuleType('phase_e_bundle_fixture')
    exec(compile(translate(source),'<phase-e-fixture-only>','exec'),module.__dict__)
    class Bundle(module.F6Graph):
        def __init__(self):super().__init__(mode='e-bundle-'+failure,variant='Bundle 独立路径');self.prepares=0
        def action(self,operation,messages):
            value=super().action(operation,messages)
            if operation=='prepare_bundle':
                self.prepares+=1
                if self.prepares==1:
                    if failure=='scalar':value['produced_bindings']['dataset_label']['value']='${CATALOG_RELEASE}'
                    else:value['argv'][-1]=value['argv'][-1].replace('print(emit_effect_evidence(', '(db/"catalog"/genome/"features.gtf").unlink()\nprint(emit_effect_evidence(')
            return value
        class Recovery(module.F6Graph.Recovery):
            async def ainvoke(self,messages,**kwargs):
                value=await super().ainvoke(messages,**kwargs)
                value.tool_calls[0]['args']['action']='retry_step'
                return value
    async def scenario():
        with patch.object(inert_config,'SKILLS_DIR',str(tmp_path/'public_skills')), patch.dict(os.environ,BIOMIND_RECOVERY_RESOURCE_POLICY='phase_e_candidate_v2'):
            f=Bundle();await f.setup()
        try:
            await drive(f,skill_id='fixture/bundle')
            assert f.durable['task_state']['status']=='completed',f.values
            assert f.prepares==2 and len(f.commits)==6
            assert len(f.executions)==len(f.approvals)==(6 if failure=='scalar' else 7)
            assert f.counts['recovery']==(0 if failure=='scalar' else 1)
            assert all(e['operation']!='prepare_database' for e in f.executions)
            save('e-v20-'+failure,{'counts':f.counts,'operations':[e['operation'] for e in f.executions],
                'commits':len(f.commits),'executions':len(f.executions),'approvals':len(f.approvals),'scalar':'release_73',
                'layout':'catalog/sequences/<dataset_label>.fa; catalog/<dataset_label>/features.gtf','public_skill':target.read_text()})
        finally:await f.close()
    asyncio.run(scenario())
