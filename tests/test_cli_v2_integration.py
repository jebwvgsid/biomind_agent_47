"""Real display producers with synthetic payloads; never load an original task."""
import asyncio
from copy import deepcopy
import io
import json
import logging
from unittest.mock import patch

import pytest

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from tests.test_cli_v2_display import service_writer_callbacks


@pytest.mark.parametrize('verbosity', ['compact', 'verbose'])
def test_v2_actual_planner_observer_keeps_diagnostics_and_hides_routine_rows(verbosity):
    import bio_agents
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer=io.StringIO(); owner=PlainTerminal(writer,verbosity=verbosity)
        old=bind_terminal(owner)
        router=bio_agents.OutputRouter()
        agent=bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
        callbacks=[]; logs=[]
        agent.planner_progress_callback=lambda event,row:callbacks.append((event,deepcopy(row)))
        agent.planner_progress_enabled=True
        class Capture(logging.Handler):
            def emit(self, record):logs.append(record.getMessage())
        handler=Capture(); logger=bio_agents.logger
        old_level=logger.level; logger.setLevel(logging.INFO); logger.addHandler(handler)
        try:
            with patch.object(bio_agents,'output',router):
                for index in range(30):
                    agent._planner_progress('v2-synthetic','attempt_progress',{'candidate_ordinal':1,
                        'content_char_count':index*37,'elapsed_seconds':index/10,
                        'payload_idle_seconds':.1,'observation_at':f'fixed-{index}'})
                observed=agent._planner_observed_call('v2-synthetic',{'candidate_ordinal':1},
                    'parse_envelope',json.loads,'{"synthetic":true}')
                agent._planner_progress('v2-synthetic','attempt_failed',{'candidate_ordinal':1,
                    'failure_type':'synthetic_timeout','observation_at':'failure'})
            await owner.drain(1)
            text=writer.getvalue()
            assert observed=={'synthetic':True}
            assert len(callbacks)==33
            assert len([line for line in logs if line.startswith('Planner progress:')])==33
            assert ('正文 1073 字符' in text)==(verbosity=='verbose')
            assert ('parse_envelope 完成' in text)==(verbosity=='verbose')
            assert 'synthetic_timeout' in text
            assert any('正文 1073 字符' in row for row in owner.model.details)
            assert callbacks[29][1]['content_char_count']==1073
        finally:
            logger.removeHandler(handler);logger.setLevel(old_level)
            bind_terminal(old);owner.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('verbosity',['compact','verbose'])
def test_v2_actual_observation_outlet_keeps_full_machine_record_in_details(verbosity):
    from bio_agents import OutputRouter
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer=io.StringIO();owner=PlainTerminal(writer,verbosity=verbosity);old=bind_terminal(owner)
        try:
            original='BIOMIND_EFFECT_EVIDENCE_JSON={"value":true,"api_key":"synthetic-secret"}'
            OutputRouter().print_observation(original,item_id='synthetic-output',
                summary='进程已退出 · rc=0；正式核验中')
            await owner.drain(1)
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON' not in writer.getvalue()
            assert 'synthetic-secret' not in writer.getvalue()
            assert '正式核验中' in writer.getvalue()
            assert any('BIOMIND_EFFECT_EVIDENCE_JSON' in row for row in owner.model.details)
            assert all('synthetic-secret' not in row for row in owner.model.details)
            assert original.endswith('"synthetic-secret"}')
        finally:bind_terminal(old);owner.close()
    asyncio.run(scenario())
