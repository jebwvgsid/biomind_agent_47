"""Synthetic execution + real typed graph, readers, Recovery, outer, provider adapter.

Only the scientific process boundary and model responses are controlled. Original
session files are neither loaded nor modified by these acceptance tests.
"""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from agent_core.workflow.execution import TypedExecute
from agent_core.workflow.outcomes import GenerateOutcome
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.managed_stop import current_managed_stop
from execution.profiling import RunProfiler
from bio_agents import OutputRouter
from tests.test_inspection_workflow import make_inspection_fixture, _tag_json
from tests.test_provider_attempt_reliability import _agent

BAD_SCRIPT = "awk -F'[=<,]' '/^##contig=<ID=/{print $3}' approved.vcf"

class SummaryModel:
    def __init__(self, mode="ok"):
        self.mode, self.requests = mode, []
    async def ainvoke(self, messages, config=None):
        self.requests.append(list(messages))
        assert len(messages) == 2
        assert [m.type for m in messages] == ["system", "human"]
        assert not any(getattr(m, "tool_calls", None) for m in messages)
        facts = json.loads(messages[1].content)
        assert facts["stop_reason"]["error_code"]
        if self.mode == "connection": raise ConnectionError("synthetic provider failure")
        if self.mode == "timeout": await asyncio.sleep(10)
        if self.mode == "empty": return AIMessage(content="")
        if self.mode == "tool": return AIMessage(content="", tool_calls=[{"name":"execute", "args":{},"id":"forbidden","type":"tool_call"}])
        if self.mode == "forged": return AIMessage(content=json.dumps({"explanation":"任务已完成", "fact_ids":["origin"]}))
        return AIMessage(content=json.dumps({"explanation":"自动恢复已停止；脚本断言仍需可靠复核，后续步骤尚未执行。", "fact_ids":["origin","stop_reason"]},ensure_ascii=False))

class RecoveryModel:
    def __init__(self, fixture, *, readable=False, transport_failure=False):
        self.fixture, self.readable = fixture, readable
        self.transport_failure = transport_failure
        self.requests, self.results = [], []
    async def ainvoke(self, messages, **kwargs):
        self.requests.append(list(messages))
        seed = _tag_json(list(messages), "recovery_seed")
        manifest = seed["inspection_manifest"]
        self.results = [json.loads(m.content) for m in messages if m.type == "tool"]
        if self.transport_failure and len(self.requests) >= 3:
            # Fail after actual evidence reads and inspections, before the
            # default no-progress bound. Existing transport retry policy stays.
            raise ConnectionError('synthetic Recovery transport failed after recorded reads')
        if len(self.requests) == 1:
            calls=[{"id":"failure", "name":"failure_inspect", "args":{"ref_id":seed["failure_packet"]["source_outcome_ref"]}, "type":"tool_call"},
                   {"id":"undeclared", "name":"step_inspect", "args":{"ref_id":seed["invocation_scope"]["step_id"]}, "type":"tool_call"}]
            execution = next(row for row in manifest if row.get("scope",{}).get("object_kind") == "execution")
            calls.append({"id":"script", "name":"evidence_read", "args":{"evidence_ref":execution["ref_id"]}, "type":"tool_call"})
        elif len(self.requests) == 2:
            # Assert content is on the next provider request, not merely in a tool return.
            text = "\n".join(m.content for m in messages if m.type == "tool")
            assert "print $3" in text and "CONTIG_MISMATCH_REASON" in text, repr(self.results[-1])
            assert "INSPECTION_REF_NOT_DECLARED" in text
            script_message=next(row for row in self.results if row['request_id']=='script')
            assert not script_message['error'] and not script_message['result']['truncated']
            execution=json.loads(script_message['result']['content'])['execution']
            expected_script=getattr(self.fixture,'expected_script',BAD_SCRIPT).strip()
            assert execution['code'].strip()==expected_script
            assert execution.get('returncode',execution.get('exit_code'))==1
            stdout=execution.get('raw_stdout') or execution.get('stdout') or ''
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON=' in stdout and 'false' in stdout
            assert 'CONTIG_MISMATCH_REASON' in (execution.get('raw_stderr') or execution.get('stderr'))
            artifacts = [row for row in manifest if row["kind"] == "artifact" and row["scope"].get("direction") == "input"]
            assert len(artifacts) == 3
            if not self.readable:
                for path in (self.fixture.root / "inputs").iterdir(): path.unlink()
            calls=[{"id":"inspect-"+str(i), "name":"artifact_inspect", "args":{"ref_id":row["ref_id"]}, "type":"tool_call"} for i,row in enumerate(artifacts)]
        elif len(self.requests) == 3:
            artifacts = [row for row in manifest if row["kind"] == "artifact" and row["scope"].get("direction") == "input"]
            calls=[{"id":"read-"+str(i), "name":"artifact_read", "args":{"ref_id":row["ref_id"],"start_line":1,"end_line":50}, "type":"tool_call"} for i,row in enumerate(artifacts)]
        else:
            assert self.readable
            text = "\n".join(m.content for m in messages if m.type == "tool")
            assert "##contig=<ID=chr1" in text and ">chr1" in text and 'gene_id' in text
            import hashlib
            proof=[]
            for row in self.results:
                if row.get('tool') in {'artifact.read','evidence.read'} and not row.get('error'):
                    proof.append({'tool':row['tool'],'request_id':row['request_id'],'ref_id':row['source_ref'],
                        'content_sha256':hashlib.sha256(row['result']['content'].encode()).hexdigest(),
                        'visible_in_model_request_ordinal':len(self.requests),'truncated':row['result'].get('truncated',False)})
            (self.fixture.root/'context_chain_proof.json').write_text(json.dumps(proof,indent=2))
            return AIMessage(content=json.dumps({"schema_version":1,"directive_id":"correct-parser", "task_id":self.fixture.task_id,
                "action":"retry_step", "summary":"The recorded extractor selected ID instead of the chromosome; regenerate the action.",
                "reason_code":"GENERATED_ACTION_FAILED", "target_step_id":seed["invocation_scope"]["step_id"],
                "instruction":"Repair the VCF header extractor while preserving the approved comparison method. Require a new action and approval.",
                "evidence_refs":[seed["failure_packet"]["source_outcome_ref"]]}))
        return AIMessage(content="", tool_calls=calls)

async def run_execution_case(tmp_path, summary_mode="ok", *, readable=False, summary_policy=None, inject=None,
                             recovery_transport_failure=False):
    fixture=make_inspection_fixture(tmp_path, execution_returncode=1, claimed_value=False)
    fixture.execution.update(code=BAD_SCRIPT, raw_stderr="CONTIG_MISMATCH_REASON=VCF contigs missing in FASTA/GTF")
    # Synthetic adapter input: the already launched failed attempt is not replayed.
    async def recorded_execute(_state): return {"execution_evidence":deepcopy(fixture.execution)}
    execute=bind_causal_failure_node(TypedExecute(executor=recorded_execute,data_path=str(tmp_path)).run,
        binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field="execute_outcome")
    recovery_model=RecoveryModel(fixture, readable=readable, transport_failure=recovery_transport_failure)
    recovery=Schema2RecoveryNode(data_path=str(tmp_path),model=recovery_model,
        phase_store=PhaseArchiveStore(str(tmp_path)))
    generates=[]
    async def generate(state):
        generates.append(deepcopy(state))
        if len(generates)>1:
            assert readable and state["recovery_outcome"]["type"] == "candidate_repair_required"
            # Deliberately pause at the real correction handoff, not claim execution.
            return {"generate_outcome":GenerateOutcome(schema_version=1,type="user_input_needed",summary="Review the corrected action.",scope={"task_id":fixture.task_id},
                payload={"question_id":"approval", "question":"Approve the new candidate", "requested_slots":[], "owner":"approval"}).to_dict()}
        return {"generate_outcome":GenerateOutcome(schema_version=1,type="action_ready",summary="Recorded failed execution boundary",scope={"task_id":fixture.task_id},payload={
            "action":{},"action_id":fixture.execution["action_id"],"candidate_fingerprint":"synthetic-recorded", "validation_evidence":{}}).to_dict()}
    async def forbidden(_): raise AssertionError("No scientific runner or commit is authorized on a stop")
    graph=build_schema2_runtime_graph({"generate":generate,"execute":execute,"supervisor":forbidden,"recovery":recovery.run,"commit_step":forbidden,"final_audit":forbidden}).compile(checkpointer=InMemorySaver())
    config={"configurable":{"thread_id":fixture.task_id},"recursion_limit":15}
    summary=SummaryModel(summary_mode)
    agent=_agent(summary,data_path=str(tmp_path),attempts=1)
    agent.llm_timeout_seconds=agent.llm_readonly_timeout_seconds=agent.llm_total_timeout_seconds=1
    agent.terminal_summary_llm=summary
    agent.terminal_summary_policy={"enabled":True,"timeout_seconds":0.05 if summary_mode=="timeout" else 1,**(summary_policy or {})}
    agent.state_manager=None
    agent.profiler=RunProfiler(tmp_path/'task_outputs');agent.profiler.start_task(fixture.task_id)
    agent._app_for_task=lambda _:graph
    async def go(_input,**kwargs): await graph.ainvoke(deepcopy(fixture.state),config)
    agent.go=go
    if inject: inject(agent,fixture)
    result=await agent._run_agent_interrupt_loop("synthetic execute failure",fixture.task_id)
    return fixture,agent,graph,config,recovery_model,summary,result,generates

@pytest.mark.parametrize("mode",["ok","connection","timeout","empty","tool","forged"])
def test_execution_stop_real_outer_provider_and_reports(tmp_path,capsys,mode):
    async def scenario():
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path,mode)
        stop=current_managed_stop(f.reload())
        assert stop, result
        assert stop["origin"]["failure_type"]=="execution_failed"
        assert stop["execution"]["returncode"]==1
        assert not stop["execution"]["committed"] and not stop["execution"]["deterministic_passed"]
        assert stop["stop_reason"]["error_code"]=="RECOVERY_NO_PROGRESS_LIMIT"
        assert len(stop["inspection_failures"])==7
        assert {x['tool_call_id'] for x in stop['inspection_failures']} >= {'read-0','read-1','read-2'}
        # This deterministic Recovery stop now always skips terminal narration,
        # including when the configured model would fail or invent success.
        assert len(r.requests)==3 and not m.requests
        assert stop['summary_status']=='SKIPPED'
        assert stop['summary_skip_reason']=='RECOVERY_STOP_NO_MODEL'
        assert stop['summary_llm_calls']==0
        assert not stop.get('summary_attempted')
        assert not stop.get('summary_text') and not stop.get('summary_unverified_prose')
        assert '任务已完成' not in result.reason
        assert result.failure_code=='RECOVERY_NO_PROGRESS_LIMIT' and result.stage=='recovery'
        assert 'CONTIG_MISMATCH_REASON' in result.reason and '未经独立复核' in result.reason
        assert '确定性检查：通过' not in result.reason
        assert set(stop['report_paths'])=={'report.md','stage_status.tsv','reproduce.sh'}
        for path in stop['report_paths'].values(): assert Path(path).is_file()
        assert 'print $3' in Path(stop['report_paths']['reproduce.sh']).read_text()
        report=Path(stop['report_paths']['report.md']).read_text()
        assert 'read-2' in report and 'returncode=1' in report
        metrics=json.loads((tmp_path/'task_outputs'/f.task_id/'run_metrics.json').read_text())
        assert metrics['final_result']['terminal_diagnostic']['stop_id']==stop['stop_id']
        assert metrics['final_result']['terminal_diagnostic']['summary_llm_calls']==0
        commitments=deepcopy(f.reload()['task_state'].get('typed_step_commit_receipts') or {})
        protected_reports={path:Path(path).read_bytes() for path in stop['report_paths'].values()}
        assert OutputRouter().render_terminal_result(result)
        output=capsys.readouterr().out
        assert 'RECOVERY_NO_PROGRESS_LIMIT' in output and '模型总结未运行' in output
        # Reentry preserves persisted diagnosis, formal commitments and reports.
        await a._finalize_managed_terminal(f.task_id,g.get_state(c))
        assert not m.requests and len(r.requests)==3
        assert current_managed_stop(f.reload())['stop_id']==stop['stop_id']
        assert (f.reload()['task_state'].get('typed_step_commit_receipts') or {})==commitments
        assert all(Path(path).read_bytes()==body for path,body in protected_reports.items())
    asyncio.run(scenario())

def test_canonical_inputs_content_reaches_next_request_and_correction(tmp_path):
    async def scenario():
        f,a,g,c,r,m,result,generates=await run_execution_case(tmp_path,readable=True)
        assert len(r.requests)==4 and len(generates)==2
        assert generates[-1]['recovery_outcome']['type']=='candidate_repair_required'
        assert len(m.requests)==0
        assert not current_managed_stop(f.reload())
        assert not f.reload()['task_state'].get('typed_step_commit_receipts')
    asyncio.run(scenario())

@pytest.mark.parametrize('policy,reason', [({'enabled':False},'CONFIG_DISABLED'),({'remaining_tokens':0},'RECOVERY_STOP_NO_MODEL'),({'provider_available':False},'RECOVERY_STOP_NO_MODEL')])
def test_summary_legal_skips_use_base(tmp_path,policy,reason):
    async def scenario():
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path,summary_policy=policy)
        stop=current_managed_stop(f.reload())
        assert not m.requests and stop['summary_skip_reason']==reason
        assert 'read-2' in result.reason
    asyncio.run(scenario())
