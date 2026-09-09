"""Uncleaned historical response and real inspection; continuation is synthetic."""
import asyncio
import json
import hashlib
from pathlib import Path
import httpx
from langchain_core.messages import AIMessage,HumanMessage
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry,RecoveryLocalToolDispatcher
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import RecoveryAgentLoop
from tests.test_provider_attempt_reliability import _agent

F=Path(__file__).parent/'fixtures/tad_resolution_recovery'

def test_uncleaned_diagnosis_inspection_and_same_wire_retry(tmp_path):
    async def scenario():
        durable=json.loads((F/'projection_state.json').read_text())
        projection=project_recovery_seed({'task_id':durable['task_id']},durable)
        assert projection is not None
        seed=projection.recovery_seed;phase=PhaseArchiveStore(str(tmp_path))
        registry=build_recovery_inspection_registry(seed,durable,phase_store=phase)
        dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=phase)
        original=json.loads((F/'response.json').read_text());calls=[]
        class Model:
            def bind_tools(self,tools):self.tools=tools;return self
            async def ainvoke(self,messages,config=None):
                calls.append([m.model_dump() for m in messages])
                if len(calls)==1:return AIMessage(content=original['content'],tool_calls=original['tool_calls'])
                assert original['content'] in [m.content for m in messages]
                tools=[m for m in messages if m.type=='tool']
                assert len(tools)==1
                result=json.loads(tools[0].content)
                assert result['error']=={} and result['tool']=='failure.inspect'
                assert result['source_ref']==original['tool_calls'][0]['args']['ref_id']
                if len(calls)==2:raise httpx.ConnectError('synthetic connection failure')
                assert calls[1]==calls[2]
                return AIMessage(content='Use the registered metadata checker; original diagnosis retained.',tool_calls=[{
                    'id':'synthetic-submission','name':'submit_recovery_directive','args':{
                        'schema_version':1,'directive_id':'synthetic-resolution-fix','task_id':durable['task_id'],
                        'action':'retry_step','target_step_id':'preflight_input_validation','reason_code':'SCRIPT_BUG',
                        'summary':'Incorrect integer/group-path comparison.',
                        'instruction':'Use check_cooler_resolution.py with the approved inputs and resolution. Preserve the original diagnosis and stdout.'}}])
        model=Model();agent=_agent(model,data_path=str(tmp_path));agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=30
        agent.recovery_llm_policy={'sdk_max_retries':0,'application_max_attempts':2}
        agent._schema2_recovery_event_sink=lambda *args:None
        loop=RecoveryAgentLoop(model=model,dispatcher=dispatcher,provider_invoke=agent._schema2_recovery_provider_invoke)
        old=json.loads((F/'request.json').read_text())
        inherited=[HumanMessage(content=m['content'],name=m.get('name'),id=m['id']) for m in old['messages'][2:]]
        result=await loop.ainvoke(seed=seed,durable=durable,inspection_manifest=registry.provider_manifest(),inherited_messages=inherited)
        assert result.get('directive',{}).get('directive_id')=='synthetic-resolution-fix',result
        assert len(calls)==3
        artifact={'provider':'SYNTHETIC','calls':calls,'result':result}
        (tmp_path/'raw-replay-proof.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2,default=str))
    asyncio.run(scenario())
