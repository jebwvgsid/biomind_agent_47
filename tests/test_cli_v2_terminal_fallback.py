"""Exercise the production owner entry on synthetic dumb/closed terminals."""
import asyncio
import io

import pytest
from agent_core.cli_display import emit, emit_public, flush_public, terminal
from agent_core.cli_terminal import run_terminal
from tests.test_cli_v2_display import service_writer_callbacks


@pytest.mark.parametrize('verbosity',['compact','verbose'])
@pytest.mark.parametrize('failure',[False,True])
def test_v2_term_dumb_and_broken_pipe_preserve_runtime_and_add_no_ansi(monkeypatch,verbosity,failure):
    import agent_core.cli_terminal as module
    class Writer(io.StringIO):
        def isatty(self):return True
        def write(self,value):
            if failure:raise BrokenPipeError('synthetic output closed')
            return super().write(value)
    writer=Writer();modes=[];facts=[]
    monkeypatch.setattr(module.sys,'stdout',writer)
    monkeypatch.setattr(module.sys.stdin,'isatty',lambda:True)
    monkeypatch.setenv('TERM','dumb')
    monkeypatch.setenv('BIOMIND_UI','')
    monkeypatch.setenv('BIOMIND_CLI_VERBOSITY',verbosity)
    async def runtime():
        service_writer_callbacks(asyncio.get_running_loop())
        modes.append(terminal().mode)
        emit('message','synthetic timer',purpose='telemetry')
        emit_public('当前合成检查已返回。')
        await flush_public()
        facts.extend(['same business return','same approval eligibility'])
    asyncio.run(run_terminal(runtime))
    assert modes==['plain'] and facts==['same business return','same approval eligibility']
    value=writer.getvalue()
    assert '\x1b' not in value
    if failure:assert value==''
    else:
        assert '当前合成检查已返回。' in value
        assert ('synthetic timer' in value)==(verbosity=='verbose')
