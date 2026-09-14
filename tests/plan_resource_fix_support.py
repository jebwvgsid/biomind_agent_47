"""Owned fixed-provider fixture. Production parser, validators, claims and stores stay real.
No original session is loaded. Synthetic paths are not scientific inputs.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage

AUDIT=Path(__file__).resolve().parents[1]/'docs/audits/session_1789369423_plan_resource_cycle'
SID='genomics/snpeff-annotation'

def save(name, value):
    root=Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']);root.mkdir(parents=True,exist_ok=True)
    (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')

class RouterStub:
    def bind(self, **kwargs):return self
    def model_copy(self, update=None):return self
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=json.dumps({'schema_version':1,'request_kind':'execute','execution_scope':'managed',
            'managed_intent_explicit':True,'retrieval_mode':'kb','confidence':1.0,'reason':'explicit synthetic interface test'}))

class PlanFixGraphCase:
    async def asyncSetUp(self):
        import bio_agents
        self.temp=tempfile.TemporaryDirectory(prefix='plan-resource-fix-')
        self.root=Path(self.temp.name)
        self.patches=[patch.object(bio_agents,'init_chat_model',return_value=RouterStub()),
            patch.object(bio_agents,'BIOMIND_MEMORY_DIR',str(self.root/'memory')),
            patch.object(bio_agents,'BIOMIND_WIKI_ENABLED',False),
            patch.object(bio_agents,'ENABLE_ROOT_CAUSE_ANALYZER',False)]
        for item in self.patches:item.start()
        self.agent=bio_agents.BioAnalystAgent(data_path=str(self.root),enable_persistence=False,use_llm_supervisor=False)
        async def no_memory(*args,**kwargs):return None
        async def no_compress(messages,task_id):return messages,False
        self.agent._observe_session_memory=no_memory
        self.agent._finalize_task_memory=no_memory
        self.agent.memory_manager.maybe_compress=no_compress
        self.agent._consultant_runtime_seed_evidence=lambda *a,**k:([],{},'')
        self.agent._consultant_runtime_preflight=lambda *a,**k:([],'')
    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        loop=asyncio.get_running_loop();executor=getattr(loop,'_default_executor',None)
        if executor is not None:
            executor.shutdown(wait=False,cancel_futures=True);loop._default_executor=None
        self.temp.cleanup()
    def _runner_state(self, task_id):
        from agent_core.plan_tracker import save_plan_state, canonical_task_spec_for_state
        prompt='Explicit synthetic interface test, no scientific execution.'
        durable,_,_=self.agent._persist_user_submission(task_id,prompt)
        admission={'schema_version':1,'request_kind':'execute','execution_scope':'managed','managed_intent_explicit':True,
            'task_mode':'managed','retrieval_mode':'kb','confidence':1.0,'admission_status':'managed_execution_admitted',
            'reason':'fixture user authorization for isolated interfaces','decision_source':'test_fixture'}
        durable['task_state']['intent_admission']=admission
        save_plan_state(self.root,task_id,durable)
        return {'messages':[HumanMessage(content=prompt)],'task_id':task_id,'session_id':task_id,
            'project_id':self.agent.default_project_id,'conversation_evidence':dict(durable.get('conversation_evidence') or {}),
            'task_spec':canonical_task_spec_for_state(durable),'task_revision':0,'selected_skills':[],
            'resolved_bindings':{},'consultation_state':{},'planner_feedback':{},'request_intent':admission,
            'intent_admission':admission,'managed_intent_admitted':True}

async def scenario(candidates, name, *, late_cancel=False, readonly=False, after_plan=None, before_dispatch=None, bounded_rejection=True, setup=None):
    import bio_agents
    from tests.plan_resource_fix_support import PlanFixGraphCase
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    case = PlanFixGraphCase()
    case.SKILL_ID = SID
    await case.asyncSetUp()
    task = 'synthetic-resource-cycle-'+name
    declarations = json.loads((AUDIT/'task_declarations.json').read_text())
    # The graph's Consultant inspects paths before model dispatch. Give that
    # separate boundary nonexistent synthetic paths; never relax the read guard.
    for x in declarations['inputs']:
        x['canonical_path']=str(case.root/'输入 中文'/Path(x['canonical_path']).name)
    declarations['output_contracts'][0]['canonical_path']=str(case.root/'结果 新根'/'medium')
    paths = [x['canonical_path'] for x in declarations['inputs']]
    # Declaration strings are preserved and never opened. All state stays in /tmp.
    goal = 'Structure audit only: check contigs, prepare a NEW task-local workspace, build in it, annotate and summarize. Inputs read only: '+', '.join(paths)
    goal += '; Output root '+declarations['output_contracts'][0]['canonical_path']+'; required outputs annotated_vcf ann_summary report reproduce_script stage_status.'
    if readonly:
        declarations['output_contracts']=[]
        goal='Read only contig compatibility observation, no files requested. Inputs: '+', '.join(paths)
    persist = case.agent._persist_user_submission
    case.agent._persist_user_submission = lambda tid,_text:persist(tid,goal)
    operations = [{'operation':'set_goal','value':goal,'supporting_evidence':['U1']}]
    for x in declarations['inputs']:
        operations.append({'operation':'upsert_input','input':{'role':x['role'],'path':x['canonical_path'],'required':True},'supporting_evidence':['U1']})
    for x in declarations['output_contracts']:
        row={k:v for k,v in x.items() if k in ['output_id','role','artifact_kind','canonical_path','root_ref','path_policy','required','user_visible']}
        operations.append({'operation':'upsert_output_contract','output_contract':row,'supporting_evidence':['U1']})
    counts={'consultant':0,'planner':0}; requests=[]; states=[]; calls=[]; events=[]
    final_states=[]; external_stop=[]
    progress=case.agent._planner_progress
    def progress_observer(tid, event, payload):
        progress(tid,event,payload)
        if event=='candidate_rejected':
            states.append(deepcopy(load_plan_state(case.root,task)))
            if bounded_rejection and counts['planner']==len(candidates) and not late_cancel:
                external_stop.append('after durable rejection, before next dispatch')
                raise asyncio.CancelledError('external bounded audit ceiling')
    case.agent._planner_progress=progress_observer
    capture = case.agent._planner_observed_call
    def observe(tid, observation, phase, operation, *args, **kwargs):
        result = capture(tid, observation, phase, operation, *args, **kwargs)
        # Exact actual arguments to resource/effect/admission validators in this synthetic run.
        if phase in ['validate_wire_and_bind','compile_resource_bindings','compile_step_effects','compile_dataflow','validate_candidate','normalize_plan_contracts']:
            calls.append({'ordinal':counts['planner'],'phase':phase,'args':deepcopy(args),'kwargs':deepcopy(kwargs),'result':deepcopy(result)})
        return result
    case.agent._planner_observed_call=observe
    async def provider(messages, *, stage, **kwargs):
        assert stage in counts, 'No Generate, scientific execution or extra external stage allowed: '+stage
        if stage=='consultant':
            counts[stage]+=1
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Structure only synthetic task.','selected_skills':[SID],'task_spec_operations':operations,'unresolved_ambiguities':[],'planner_feedback':None}))
        index=counts['planner']
        assert index < len(candidates), 'External bounded witness ceiling; not autonomous convergence'
        counts[stage]+=1
        requests.append([{'type':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages])
        if late_cancel:
            durable=load_plan_state(case.root,task)
            case.agent._commit_user_cancellation(task, source='audit', reason='Synthetic cancellation before a late response')
        if before_dispatch is not None:
            await before_dispatch(case, task, counts, index)
        value=candidates[index]
        return value if isinstance(value, AIMessage) else AIMessage(content=json.dumps(value), response_metadata={'finish_reason':'stop'})
    case.agent._ainvoke_node_llm=provider
    def forbidden(*a, **k): raise AssertionError('Approval/execution forbidden in audit')
    case.agent._prompt_user=forbidden
    try:
        runner=case._runner_state(task)
        if setup is not None:
            await setup(case, task, runner, counts)
        app=bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer, interrupt_after=['planner'])
        async def consume():
            for index in range(len(candidates)):
                stream=app.astream(runner if index==0 else None,config={'configurable':{'thread_id':task},'recursion_limit':60},stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if 'planner' in event:
                            d=load_plan_state(case.root,task)
                            final_states.append(deepcopy(d))
                finally: await stream.aclose()
        try: await asyncio.wait_for(consume(),45)
        except asyncio.CancelledError:
            assert external_stop, 'Unexpected cancellation'
        if not final_states: final_states.append(deepcopy(load_plan_state(case.root,task)))
        result={'counts':counts,'states':states,'requests':requests,'validator_calls':calls,'events':events,
            'final_states':final_states,'external_stop':external_stop,
            'production_source':'exact export; no validator mocked','stop':'external bounded witness ceiling' if external_stop else 'production routing',
            'registry':case.agent.skill_manager.get_execution_capability_registry([SID])}
        if after_plan is not None:
            result['after_plan'] = await after_plan(case, task, final_states[-1], runner)
        save(name,result)
        return result
    finally: await case.asyncTearDown()
