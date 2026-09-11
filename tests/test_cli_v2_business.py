"""Fixed fake responses through actual nodes, approval, tiny process and Commit."""
import asyncio
from unittest.mock import patch
from tests.test_cli_generate_public_integration import node_scenario
from tests.test_cli_model_progress_integration import artifact
from tests.test_cli_v2_display import service_writer_callbacks


def test_v2_compact_verbose_off_keep_calls_routes_approval_raw_execution_commit(tmp_path):
    fixture=tmp_path/'synthetic-input.txt'
    fixture.write_text('synthetic input\n')
    async def scenario():
        import agent_core.cli_terminal as terminal
        service_writer_callbacks(asyncio.get_running_loop())
        actual=terminal.PlainTerminal
        results={}
        for mode in ['compact','verbose','off']:
            def owner(*args,**kwargs):
                return actual(*args,verbosity=mode,**kwargs)
            with patch.object(terminal,'PlainTerminal',side_effect=owner):
                results[mode]=await node_scenario('off' if mode=='off' else 'plain',fixture,
                    generate_envelope=True,first_kind='syntax_error')
        assert results['compact']==results['verbose']==results['off']
        assert results['compact']['calls']=={'consultant':2,'planner':2,'generate':2}
        assert results['compact']['approvals']==results['compact']['executions']==results['compact']['commits']==1
        assert results['compact']['repair_event_count']==1
        artifact('v2-business-invariants.json',{'scope':'fixed fake responses; synthetic files only',
            'method':'actual graph + syntax repair + original approval callback + tiny Bash + CommitStep',
            'modes':results,'equal':True})
    asyncio.run(scenario())
