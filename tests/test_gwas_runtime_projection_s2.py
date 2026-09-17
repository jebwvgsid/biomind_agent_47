"""Projection boundaries: scope drift must not advertise a usable candidate."""
import asyncio
from copy import deepcopy
from unittest.mock import patch

from tests.test_gwas_runtime_discovery import discovery, discover, SID, CAP, BID


def test_changed_scope_downgrades_provider_observation_and_ledger_together(discovery):
    agent, _leaf, _binary = discovery
    checked = discover(agent)
    assert checked['availability'] == 'AVAILABLE'
    expected = deepcopy(checked['selected_candidate']['runtime_observation_scope'])
    expected['skill_digest'] = 'sha256:' + '0' * 64
    async def completed_inspection(*args, **kwargs):
        return checked
    with patch.object(agent, '_discover_skill_runtime_candidates', completed_inspection), \
         patch.object(agent, '_runtime_evidence_scope', return_value=expected), \
         patch('bio_agents.load_plan_state', return_value={'task_state': {}}):
        audits, _evidence, observations = asyncio.run(agent._consultant_operation_runtime_evidence(
            [SID], 'discovery-task', [{'skill_id': SID, 'execution_capability_id': CAP}]))
    projected = agent._runtime_operation_observation(observations)[0]
    assert projected['availability'] == 'UNKNOWN'
    assert projected['selected_environment'] == '' and not projected['install_required']
    assert projected['reason_code'] == 'RUNTIME_EVIDENCE_SCOPE_CHANGED'
    assert projected['candidates'][-1]['availability'] == 'UNKNOWN'
    assert projected['candidates'][-1]['resolved_runtime_paths'] == {}
    assert audits[0]['runtime_evidence_record']['facts'][BID]['availability'] == 'UNKNOWN'
