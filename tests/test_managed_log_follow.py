import asyncio
import hashlib
import logging
from pathlib import Path
import pytest
from execution.executor import run_script_path,get_last_execution_metadata

BODY='printf first > "$BIOMIND_TEST_DIR/out"\nprintf err > "$BIOMIND_TEST_DIR/err"\n/usr/bin/sleep 0.6\nprintf last >> "$BIOMIND_TEST_DIR/out"\nexit 7\n'

@pytest.mark.parametrize('mode',['normal','timeout','cancel','reader_failure','stall'])
def test_real_runner_follows_before_exit_without_replay(tmp_path,caplog,mode,monkeypatch):
    async def scenario():
        p=tmp_path/'observed.sh';p.write_text(BODY)
        caplog.set_level(logging.INFO)
        if mode=='stall':
            monkeypatch.setenv('BIOMIND_EXEC_STALL_TIMEOUT_SECONDS','0.15')
            monkeypatch.setenv('BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS','0.2')
        if mode=='reader_failure':
            import builtins
            import execution.executor as executor
            original=executor.os.open
            def denied(path,*args,**kwargs):
                if str(path)==str(tmp_path/'out'):raise PermissionError('injected log reader failure')
                return original(path,*args,**kwargs)
            monkeypatch.setattr(executor.os,'open',denied)
        call=asyncio.create_task(run_script_path(p,expected_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
            interpreter='bash',profile=False,timeout=0.25 if mode=='timeout' else 5,
            env={'PATH':'/usr/bin:/bin','BIOMIND_TEST_DIR':str(tmp_path)},
            managed_log_paths={'stdout':str(tmp_path/'out'),'stderr':str(tmp_path/'err')}))
        await asyncio.sleep(0.35)
        if mode!='reader_failure':assert 'Execution output stream=stdout' in caplog.text
        if mode=='cancel':
            call.cancel()
            with pytest.raises(asyncio.CancelledError):await call
            meta=get_last_execution_metadata()
            assert meta['status']=='cancelled' and meta['cancellation_source']=='caller_unspecified'
            assert meta['process_reaped']
            return
        stdout,stderr,rc=await call
        assert stderr.startswith('err') and (tmp_path/'err').read_text()=='err'
        if mode!='reader_failure':assert stdout.count('first')==1
        assert rc==(124 if mode in {'timeout','stall'} else 7)
        if mode=='normal':assert stdout=='firstlast'
        meta=get_last_execution_metadata();assert meta['effective_timeouts']['wall_seconds']==(0.25 if mode=='timeout' else 5)
        assert bool(meta['log_observation_errors'])==(mode=='reader_failure')
        if mode=='reader_failure':assert (tmp_path/'out').read_text()=='firstlast'
    asyncio.run(scenario())

BULK_BODY='printf "%0100000d" 0 > "$BIOMIND_TEST_DIR/out"\nprintf "%0100000d" 1 > "$BIOMIND_TEST_DIR/err"\nexit 0\n'

@pytest.mark.parametrize('writer_failure',[False,True])
def test_bulk_zero_exit_and_archive_writer_failure(tmp_path,monkeypatch,writer_failure):
    import builtins
    import execution.executor as executor
    out_archive=tmp_path/'archive.out';err_archive=tmp_path/'archive.err'
    monkeypatch.setattr(executor,'_execution_stream_log_paths',lambda pid:(str(out_archive),str(err_archive)))
    original=builtins.open
    class FailingWriter:
        def write(self,data):raise OSError('injected archive write failure')
        def flush(self):pass
        def close(self):pass
    if writer_failure:
        def open_file(path,*args,**kwargs):
            if str(path)==str(out_archive) and args and args[0]=='wb':return FailingWriter()
            return original(path,*args,**kwargs)
        monkeypatch.setattr(builtins,'open',open_file)
    async def scenario():
        p=tmp_path/'observed.sh';p.write_text(BULK_BODY)
        stdout,stderr,rc=await run_script_path(p,expected_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
            interpreter='bash',profile=False,timeout=5,
            env={'PATH':'/usr/bin:/bin','BIOMIND_TEST_DIR':str(tmp_path)},
            managed_log_paths={'stdout':str(tmp_path/'out'),'stderr':str(tmp_path/'err')})
        meta=get_last_execution_metadata()
        assert rc==0 and meta['process_returncode']==0
        assert len((tmp_path/'out').read_bytes())==len((tmp_path/'err').read_bytes())==100000
        assert err_archive.read_bytes()==(tmp_path/'err').read_bytes()
        if writer_failure:
            assert meta['log_observation_errors']==[{'stage':'execution.log_writer','stream':'stdout','error':'OSError'}]
        else:
            assert not meta['log_observation_errors']
            assert out_archive.read_bytes()==(tmp_path/'out').read_bytes()
    asyncio.run(scenario())
