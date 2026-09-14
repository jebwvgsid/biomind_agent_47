"""Original failure origin is separate from the managed stopping boundary.

Three fresh formal graphs retain real transport, approvals, terminal persistence
and reports. Only the external provider and declared science leaf are fixed.
"""
import asyncio
from copy import deepcopy

import pytest

from tests.a1_unified_fix.test_f6_graph import F6Graph, save
from tests.a1_unified_fix.test_f6_repeat_graph import RejectedCandidateGraph


@pytest.mark.parametrize('mode', ['generate_repeat', 'generate_blocked', 'execute_recovery_stop'])
def test_origin_tracks_actual_failure_producer_and_keeps_execution_parent(mode):
    async def scenario():
        fixture = (RejectedCandidateGraph('repeat') if mode == 'generate_repeat'
                   else F6Graph('voluntary_stop' if mode == 'generate_blocked' else 'recovery_event_failure'))
        await fixture.setup()
        try:
            values, durable = await fixture.run()
            diagnostic = fixture.terminal['terminal_diagnostic']
            report = fixture.outputs['report'].read_text()
            witness = {'mode': mode, 'counts': fixture.counts,
                'diagnostic_stage': diagnostic['stage'], 'origin': diagnostic['origin'],
                'causal_execution': diagnostic['causal_execution'],
                'execution_attempts': [{key: row.get(key) for key in ('attempt_id', 'rc')}
                    for row in fixture.executions],
                'report_has_generate_origin': '来源阶段：generate' in report,
                'report_has_execute_origin': '来源阶段：execute' in report,
                'report_receipts': diagnostic.get('report_receipts')}
            if mode == 'generate_repeat':
                # A missing owner remains unknown. This calls the real pure
                # assembler on a copy of this newly produced terminal boundary;
                # it writes no state and grants no approval or execution status.
                from agent_core.workflow.managed_stop import build_managed_stop
                boundary = deepcopy(durable['task_state']['managed_run_stop']['boundary'])
                primary = boundary['outcome']['payload']['primary_cause']
                for field in ('owner', 'error_owner'):
                    primary.pop(field, None)
                unknown = build_managed_stop(fixture.task, values, durable, boundary)
                witness['missing_owner_projection'] = unknown['origin']
            save('f3-stop-origin-' + mode, witness)
            if mode == 'generate_repeat':
                assert fixture.counts['recovery'] == 0 and fixture.executions == []
                assert diagnostic['stage'] == 'generate'
                assert diagnostic['origin']['source_stage'] == 'generate'
                assert diagnostic['origin']['source_owner'] == 'GENERATED_ACTION'
                assert diagnostic['origin']['error_code'] == 'ACTION_SCHEMA_TYPE_ERROR'
                assert diagnostic['origin']['attempt_id'] == ''
                assert diagnostic['origin']['returncode'] is None
                assert diagnostic['causal_execution'] == {'status': 'UNKNOWN'}
                assert witness['report_has_generate_origin']
                assert unknown['origin']['source_stage'] == 'generate'
                assert unknown['origin']['source_owner'] == 'UNKNOWN'
            elif mode == 'generate_blocked':
                assert values['generate_outcome']['type'] == 'blocked'
                assert diagnostic['stage'] == 'generate'
                assert diagnostic['origin']['source_stage'] == 'generate'
                assert diagnostic['origin']['failure_type'] == 'blocked'
                assert diagnostic['origin']['source_owner'] == 'UNKNOWN'
                assert diagnostic['origin']['attempt_id'] == ''
                assert diagnostic['causal_execution'] == {'status': 'UNKNOWN'}
                assert len(fixture.executions) == len(fixture.commits) == 1
                assert witness['report_has_generate_origin']
            else:
                attempt = fixture.executions[0]
                assert attempt['rc'] == 7
                assert diagnostic['stage'] == 'recovery'
                assert diagnostic['origin']['source_stage'] == 'execute'
                assert diagnostic['origin']['returncode'] == 7
                assert diagnostic['origin']['attempt_id'] == attempt['attempt_id']
                assert diagnostic['causal_execution']['attempt_id'] == attempt['attempt_id']
                assert witness['report_has_execute_origin']
        finally:
            await fixture.close()
    asyncio.run(scenario())
