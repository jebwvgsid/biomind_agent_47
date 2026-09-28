import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from agent_core.plan_tracker import load_plan_state, commit_runtime_evidence_state
from agent_core.runtime_evidence import make_runtime_evidence
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.typed_node import profile_typed_node
from agent_core.workflow.outcomes import VerificationOutcome
from tests.test_inspection_workflow import _tag_json
from tests.a1_unified_fix.test_f6_graph import SID


def receipt(name, value):
    target = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('d2-' + name + '.json')
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


class CaptureBlocked:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(list(messages))
        seed = _tag_json(list(messages), 'recovery_seed')
        scope = seed['invocation_scope']
        return AIMessage(content='', tool_calls=[{
            'name': 'submit_recovery_directive', 'id': 'phase-d-stop', 'args': {
                'schema_version': 1, 'directive_id': 'phase-d-observed-stop',
                'task_id': scope['task_id'], 'target_step_id': scope['step_id'],
                'action': 'blocked', 'summary': 'Offline observation completed.',
                'reason_code': 'OFFLINE_WITNESS_COMPLETE',
                'evidence_refs': [seed['failure_packet']['source_outcome_ref']],
            }}])












def test_v07_v08_rejection_index_priority_unknown_and_omitted_ranges():
    from agent_core.workflow.recovery_projection import _producer_failure_index, _diagnostic_facts
    errors = ["UNKNOWN_FIRST_91"] + ["MISSING_" + str(i) for i in range(40)]
    payload = {"return_code": 0, "status": "exited", "execution_record": {
        "attempt_id": "a", "validation": {"passed": False, "errors": errors},
        "stdout": "irrelevant background" * 100000}}
    index = _producer_failure_index(payload)
    assert index["checks"][0]["code"] == "UNKNOWN_FIRST_91"
    assert index["total_rejecting_checks"] == 41
    assert index["unexpanded_index_range"] == [12, 41]
    assert all(row["observation_status"] == "unknown" for row in index["checks"])
    facts = _diagnostic_facts("execute_outcome", {"payload": payload})
    assert facts[0]["kind"] == "producer_failure_index"
    assert "irrelevant background" not in json.dumps(facts, default=dict)










