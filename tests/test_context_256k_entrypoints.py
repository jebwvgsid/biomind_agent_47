"""Offline production request/Recovery re-entry regressions; no provider I/O."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import replace
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from agent_core.workflow.context_compaction import ContextBudgetPolicy, configured_context_policy, TokenEstimate
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher
from agent_core.context_lineage import ContextLineageStore
from agent_core.plan_contract import StateCASConflict
from agent_core.workflow.recovery_projection import build_causal_failure_record
from tests.test_recovery_projection import _durable, _execute_failure
import copy


def final():
    return AIMessage(content=_directive(), response_metadata={'finish_reason': 'stop'})


def calls(*ids, content=''):
    return AIMessage(content=content, tool_calls=[{'id':i,'name':'exec_command',
        'args':{'cmd':i}} for i in ids], response_metadata={'finish_reason':'tool_calls'})


class Selector:
    def __init__(self, *, retain_latest_group=False):
        self.requests=[]
        self.retain_latest_group=retain_latest_group
    async def __call__(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        human=messages[1]['content'] if isinstance(messages[1],dict) else messages[1].content
        wire=json.loads(human.split('\n',1)[1].rsplit('\n',1)[0]);r=wire['request']
        if r.get('operation') == 'map_source_segment':
            return AIMessage(content=json.dumps(_valid_segment_map(r)))
        if r.get('operation') == 'merge_segment_summaries':
            return AIMessage(content=json.dumps(_valid_summary_merge(r)))
        required=set(r.get('selection_policy',{}).get('must_retain_exact_unit_ids',[]))
        # selection_policy is in the request envelope produced by the manager.
        required.update(wire.get('selection_policy',{}).get('must_retain_exact_unit_ids',[]))
        units=r.get('source_units') or r['source_manifest']['units'];required.update(u['unit_id'] for u in units if u['selected'] or u.get('provenance',{}).get('required_exact'))
        if self.retain_latest_group:
            latest=next((u.get('protocol_group_id') for u in reversed(units) if u.get('protocol_group_id')), '')
            required.update(u['unit_id'] for u in units if latest and u.get('protocol_group_id')==latest)
        summary_ids=[u['unit_id'] for u in units if u['unit_id'] not in required]
        return AIMessage(content=json.dumps({'schema_version':1,
            'compaction_request_id':r['compaction_request_id'],'source_epoch_id':r['source_epoch_id'],
            'source_manifest_hash':r['source_manifest_hash'],'mode':'normal',
            'retain_exact_unit_ids':list(required),'summary_source_unit_ids':summary_ids,
            'summary':'Earlier attempts archived. PLOIDY=2 confirmed; scope/approval unchanged; cause unconfirmed.' if summary_ids else '',
            'material_retrieval_state':[],'unresolved_items':[], 'selection_rationale':'offline fixture retaining current evidence'}),
            response_metadata={'finish_reason':'stop'})




def result_for(cmd, size=100):
    return {'tool':'exec_command','status':'completed','exit_code':0,'stdout':f'{cmd}:FACT\n'+'x'*size,
        'stderr':'','receipt_ref':f'/receipts/{cmd}.json','timing':{'scans':['telemetry-root/'*1000]},
        'activity_id':'operational-only'}


























def test_default_config_is_real_256k_input_and_legacy_total_is_not_reinterpreted():
    from config.config import SCHEMA2_INPUT_SOFT_LIMIT_TOKENS, SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS
    assert SCHEMA2_INPUT_SOFT_LIMIT_TOKENS==256000
    policy=configured_context_policy()
    assert policy.input_soft_limit_tokens==256000
    assert policy.application_context_limit_tokens==SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS
    wide=replace(policy,application_context_limit_tokens=400000,known_provider_context_limit_tokens=400000)
    assert wide.trigger_tokens==256000 and wide.output_reserve_tokens==50000


def test_telemetry_projection_keeps_business_json_errors_and_old_message_evidence():
    from agent_core.command_execution import command_message_for_model, command_observation_for_model
    raw=result_for('failed');raw.update(status='failed',exit_code=2,stderr='Missing /input/required.fastq',
        stdout=json.dumps({'timing':{'scientific':'retain'},'read_paths':['/input/scientific.fastq']}))
    shown=command_observation_for_model(raw)
    assert shown['stderr']==raw['stderr'] and shown['stdout']==raw['stdout']
    envelope={'schema_version':1,'request_id':'old','tool':'exec.command','result':raw}
    original=ToolMessage(content=json.dumps(envelope),tool_call_id='old',name='exec_command')
    new=command_message_for_model(original)
    assert 'timing' not in json.loads(new.content)['result']
    assert json.loads(new.content)['result']['stdout']==raw['stdout']
    assert 'timing' in json.loads(original.content)['result']




# Bounded offline fixture builders (no imports from untracked test suites).
class _CodepointEstimator:
    name = "recovery_fixture_codepoints"
    version = "1"
    exact_or_estimated = "exact"

    def estimate(self, text: str) -> TokenEstimate:
        return TokenEstimate(
            tokens=len(text),
            estimator_name=self.name,
            estimator_version=self.version,
            exact_or_estimated=self.exact_or_estimated,
        )


class _Store:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        if int(candidate.get("state_generation") or 0) != int(
            self.value.get("state_generation") or 0
        ):
            raise StateCASConflict("fixture stale generation")
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


def _directive() -> str:
    return json.dumps({
        "schema_version": 1,
        "directive_id": "directive-compacted-recovery",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The failed generated action requires regeneration.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate only the failed action.",
        "evidence_refs": ["execution-1"],
    })


def _fixture(*, durable=None):
    durable = copy.deepcopy(durable) if durable is not None else _durable()
    record = build_causal_failure_record(
        "execute_outcome",
        _execute_failure(),
        durable,
        task_id="task-recovery",
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    state = {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
        "messages": ["global history must remain excluded"],
    }
    return durable, record, state


def _valid_segment_map(request: Mapping[str, Any]) -> dict[str, Any]:
    parent = request["parent_source_unit"]
    segment = request["segment_source_unit"]
    return {
        "schema_version": 1,
        "operation": "map_source_segment",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "segment_unit_id": segment["unit_id"],
        "parent_source_unit_id": parent["unit_id"],
        "parent_source_content_hash": parent["content_hash"],
        "segment_content_hash": segment["content_hash"],
        "coverage": segment["coverage"],
        "summary": (
            "Lossy map for exact range "
            f"{segment['coverage']}; source remains externally recoverable."
        ),
        "missing_details": "Exact characters are retained only in source archive.",
    }


def _valid_summary_merge(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "merge_segment_summaries",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "merge_request_id": request["merge_request_id"],
        "input_summary_ids": request["input_summary_ids"],
        "input_summary_hashes": request["input_summary_hashes"],
        "coverage_spans": request["coverage_spans"],
        "summary": (
            "Merged lossy coverage for "
            f"{len(request['input_summary_ids'])} child nodes."
        ),
    }













