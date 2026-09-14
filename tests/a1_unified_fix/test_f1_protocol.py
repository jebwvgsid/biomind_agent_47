"""F1 public examples sampled unchanged from real Generate provider requests.

An admitted example intentionally has no scientific implementation and is never
claimed to complete the step. The F6 fixed model owns the scientific positive;
the separate demo computes values only under its own synthetic declaration.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph, OPS, PYTHON


def save(name,value):
    root=Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    loaded={name:{'path':module.__file__,'sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
        for name,module in tuple(sys.modules.items()) if getattr(module,'__file__',None) and
        (name=='bio_agents' or name=='prompts.manager' or name.startswith(('agent_core.','execution.','memory.'))) and str(module.__file__).endswith('.py')}
    (root/'f1-protocol-loaded.json').write_text(json.dumps(loaded,ensure_ascii=False,indent=2)+'\n')
    p=root/('f1-protocol-'+name+'.json')
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


class ProtocolGraph(F6Graph):
    def __init__(self,case):
        super().__init__('recovery' if case=='recovery' else 'protocol-'+case)
        self.protocol_case=case;self.public_examples=[]

    async def provider(self,messages,*,stage,**kwargs):
        if stage!='generate':return await super().provider(messages,stage=stage,**kwargs)
        # F6's fixed model alone supplies the actual scientific first action
        # when an execution/Recovery or successor is required to reach a seam.
        if self.counts['generate']==0 and self.protocol_case in {'recovery','blocked'}:
            return await super().provider(messages,stage=stage,**kwargs)
        self.counts['generate']+=1
        protocol=json.loads(next(m.content for m in messages if getattr(m,'name','')=='current_generate_output_protocol'))
        runtime=deepcopy(kwargs['node_context'].llm_payload.to_provider_dict()['current_focus']['runtime'])
        wire='\n'.join(str(m.content) for m in messages)
        save('capture-'+self.protocol_case+'-'+str(self.counts['generate']),
            {'messages':[{'name':getattr(m,'name',None),'content':m.content} for m in messages]})
        seeds=re.findall(r'<current_generate_output_protocol>\s*(.*?)\s*</current_generate_output_protocol>',wire,re.S)
        nested=[]
        def scan(value):
            if isinstance(value,dict):
                if isinstance(value.get('current_generate_output_protocol'),dict):nested.append(value['current_generate_output_protocol'])
                for item in value.values():scan(item)
            elif isinstance(value,list):
                for item in value:scan(item)
        for m in messages:
            try:scan(json.loads(str(m.content)))
            except (TypeError,ValueError):pass
        published=[json.loads(value) for value in seeds]+nested
        assert published
        # The frozen prompt and current append-only material use the same
        # mode and current operation/resources; no second hand-written action.
        assert any(seed['action_example']==protocol['action_example'] for seed in published)
        assert '/opt/tool/bin/tool' not in wire and '/data/reference.fasta' not in wire
        selected=protocol['action_example'];raw=json.dumps(selected,ensure_ascii=False)
        if self.protocol_case=='local' and self.counts['generate']==1:
            raw=json.dumps({'action_json':selected},ensure_ascii=False)
        self.public_examples.append({'request':len(self.public_examples)+1,'protocol':protocol,'runtime':runtime,
            'selected_raw':raw,'request_messages':[{'name':getattr(m,'name',None),'type':m.type,'content':m.content} for m in messages]})
        return AIMessage(content=raw,response_metadata={'finish_reason':'stop'})

    async def collect(self):
        import bio_agents
        from agent_core.plan_tracker import load_plan_state
        runner=self.case._runner_state(self.task);runner['workflow_routing_schema_version']=2
        app=self.agent._app_for_task(self.task,runner);config={'configurable':{'thread_id':self.task},'recursion_limit':45}
        incoming=runner;self.service=asyncio.create_task(self._pulse())
        try:
            with patch.object(bio_agents,'run_script_path',self.execute):
                for _ in range(6):
                    stream=app.astream(incoming,config=config,stream_mode='updates')
                    try:
                        async for event in stream:
                            self.events.append(deepcopy(event))
                            outcome=event.get('generate',{}).get('generate_outcome',{})
                            if self.public_examples and outcome.get('type') == 'action_ready':
                                self.values=app.get_state(config).values;self.durable=load_plan_state(self.root,self.task)
                                return outcome
                    finally:await stream.aclose()
                    snap=app.get_state(config)
                    if not snap.next:
                        self.values=snap.values;self.durable=load_plan_state(self.root,self.task)
                        if self.protocol_case=='blocked':
                            self.terminal=await self.agent._finalize_managed_terminal(self.task,snap)
                            self.durable=load_plan_state(self.root,self.task)
                        return self.values.get('generate_outcome',{})
                    assert snap.tasks and snap.tasks[0].interrupts
                    incoming=bio_agents.Command(resume='y')
            raise AssertionError('public example did not reach Generate admission/stop')
        finally:
            self.values=getattr(self,'values',app.get_state(config).values)
            self.durable=load_plan_state(self.root,self.task)

    async def _pulse(self):
        while True:await asyncio.sleep(.001)


@pytest.mark.parametrize('case',['normal','local','recovery','blocked'])
def test_examples_from_actual_requests_reach_current_step_admission_or_stop(case):
    async def run():
        from bio_agents import BioAnalystAgent
        fixture=ProtocolGraph(case);await fixture.setup()
        try:
            outcome=await fixture.collect();assert fixture.public_examples
            example=fixture.public_examples[-1]['protocol']['action_example']
            raw=fixture.public_examples[-1]['selected_raw']
            assert json.loads(raw)==example
            parsed=BioAnalystAgent._extract_structured_action_result(raw);assert not parsed.error
            if case=='blocked':
                assert example['action_type']=='blocked' and 'genome_version' in example['reason']
                assert outcome['type']=='blocked'
                assert fixture.terminal['status'] in {'blocked','failed'}
                assert fixture.terminal['terminal_diagnostic']['persistence_status']=='committed'
                assert len(fixture.executions)==1
                from agent_core.step_effects import generate_output_examples
                current=fixture.public_examples[-1]['runtime'];requirements=current['binding_input_requirements']
                assert requirements['produced_bindings']['snpeff_db']['allowed_handles']
                for handles in [[],requirements['produced_bindings']['snpeff_db']['allowed_handles']*2]:
                    changed=deepcopy(requirements);changed['produced_bindings']['snpeff_db']['allowed_handles']=handles
                    bad=generate_output_examples({'operation':'prepare_database'},script_protocol=False,
                        runtime_context=current,binding_requirements=changed)
                    assert bad['action_example']['action_type']=='blocked'
                    assert 'produced_bindings.snpeff_db.value' in bad['action_example']['reason']
            else:
                assert example['action_type']=='execute' and example['operation']==OPS[0]
                assert outcome['type']=='action_ready',outcome
                for path in [fixture.vcf,fixture.reference,fixture.annotation]:assert str(path) in example['code']
                assert len(fixture.executions)==(1 if case=='recovery' else 0)
                assert 'raise RuntimeError' in example['code']
                assert 'synthetic_demo_matches' not in example['code']
            if case=='local':
                assert len(fixture.public_examples)==2
                assert 'action_type' in str(fixture.public_examples[-1]['request_messages'])
            if case=='recovery':
                assert fixture.counts['recovery']==1
                assert any('recovery' in str(m.get('name') or '') for m in fixture.public_examples[-1]['request_messages'])
            save(case,{'status':'CURRENT_STEP_ADMISSION_PASS' if case!='blocked' else 'FORMAL_STOP_PASS',
                'scientific_example_status':'NOT_IMPLEMENTED; no example execution or current scientific claim',
                'outcome':outcome,'requests':fixture.public_examples,'events':fixture.events,'executions':fixture.executions,
                'fixed_models':'Consultant/Planner/Recovery; F6 fixed scientific model only for predecessor/failed attempt',
                'substitutes':'new synthetic VCF/FASTA/GTF; no formal data/runtime/API'})
        finally:await fixture.close()
    asyncio.run(run())


def test_legacy_script_selected_mode_example_from_actual_request_is_admitted():
    async def run():
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.ger_fixture_support import configure_protocol,generate_until_ready_or_stopped
        from agent_core.public_response import project_generate_response
        from agent_core.controller_audit import parse_executable_script_block
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp();configure_protocol(case);case._configure_one_step_no_output_plan()
        async def pulse():
            while True:await asyncio.sleep(.001)
        timer=asyncio.create_task(pulse());chosen=[]
        def candidate(payload,ordinal):
            assert ordinal==1
            wire=payload['_provider_request']
            protocol=json.loads(re.findall(r'<current_generate_output_protocol>\s*(.*?)\s*</current_generate_output_protocol>',wire,re.S)[0])
            assert protocol['mode']=='script_block_v1'
            raw=json.dumps(protocol['action_example'],ensure_ascii=False);chosen.append({'raw':raw,'protocol':protocol})
            projected=project_generate_response(raw,script_protocol=True)
            block=parse_executable_script_block(projected.payload);assert block.language=='python'
            return raw
        try:
            events,requests,state=await generate_until_ready_or_stopped(case,'f1-legacy-protocol',candidate)
            assert state['generate_outcome']['type']=='action_ready',state.get('generate_outcome')
            save('legacy',{'status':'SCRIPT_MODE_CURRENT_STEP_ADMISSION_PASS','chosen':chosen,'requests':requests,'events':events,
                'scientific_example_status':'NOT_IMPLEMENTED; example deliberately fails before publishing any fact; no execution dispatched'})
        finally:
            timer.cancel();await case.asyncTearDown()
    asyncio.run(run())


def test_separate_public_demo_computes_false_and_empty_under_only_demo_declarations(tmp_path):
    from agent_core.step_effects import generate_output_examples,declared_effect_emission_contract,materialize_effect_template,structured_effect_parse_receipt,extract_structured_effect_evidence
    from tests.test_preflight_recovery_audit import approved_tiny
    published=generate_output_examples({},script_protocol=False)
    demo=published['synthetic_protocol_demo'];step=demo['step']
    frozen=declared_effect_emission_contract({'steps':[step]},step['step_id'])
    body=materialize_effect_template(demo['code'],frozen)
    out,err,rc=asyncio.run(approved_tiny(tmp_path/'new-demo.sh','exec '+shlex.join([PYTHON,'-I','-S','-c',body])))
    assert rc==0 and not err
    facts=extract_structured_effect_evidence(out)
    assert facts['produces_observations']==[{'identity':'synthetic_demo_matches','value':False,'items':[]}]
    receipt=structured_effect_parse_receipt(out,step)
    assert 'synthetic_demo_matches' not in str(published['action_example'])
    save('demo',{'status':'ISOLATED_DEMO_EXECUTION_PASS','published':demo,'stdout':out,'stderr':err,'rc':rc,'parser_receipt':receipt,
        'scope':'Standalone demonstration only, not a current-step candidate or observation','body_sha256':hashlib.sha256(body.encode()).hexdigest()})
