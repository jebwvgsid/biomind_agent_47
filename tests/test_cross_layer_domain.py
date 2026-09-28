"""Explicit user decisions and registered diagnostic resources, no science runs."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import save


def test_F35_registered_directory_feedback_and_log_reader(tmp_path):
    from tests.test_recovery_agent_loop import _seed_fixture
    from agent_core.workflow.recovery_tools import InspectionRef,TypedInspectionRegistry,RecoveryLocalToolDispatcher,RecoveryToolError
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    durable,seed=_seed_fixture();packet=seed.failure_packet
    registry=TypedInspectionRegistry(task_id=packet.task_id,plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id)
    directory=tmp_path/'不是 文本';directory.mkdir();log=tmp_path/'本轮 log.txt';log.write_text('new bounded diagnostic\n')
    for name,path in [('bundle',directory),('log',log)]:
        registry.register(InspectionRef(ref_id='artifact:'+name,kind='artifact',task_id=packet.task_id,
            plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id,backend='artifact_ledger',digest='new-'+name,size_hint=0),locator={'path':str(path)})
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=PhaseArchiveStore(str(tmp_path)))
    errors=[]
    for reader in (dispatcher._artifact_inspect,dispatcher._artifact_read):
        with pytest.raises(RecoveryToolError) as error:reader({'ref_id':'artifact:bundle','start_line':1,'end_line':1})
        assert error.value.code=='ARTIFACT_FORMAT_UNSUPPORTED'
        detail=json.loads(error.value.detail);assert detail['actual_resource_kind']=='directory'
        assert detail['automatic_followup'] is False
        assert [r['ref_id'] for r in detail['registered_alternatives']]==['artifact:log']
        errors.append(detail)
    inspected=dispatcher._artifact_inspect({'ref_id':'artifact:log'})
    read=dispatcher._artifact_read({'ref_id':'artifact:log','start_line':1,'end_line':1})
    assert read['content']=='new bounded diagnostic'
    save('F35-reader',{'errors':errors,'inspect':inspected,'read':read,'model_calls':0,'automatic_tool_followups':0})






def test_F32_F33_approved_explicit_runtime_stub_and_capability_boundary(tmp_path):
    import hashlib,shlex,sys
    from unittest.mock import patch
    from agent_core.middleware import HITLMiddleware
    from execution.executor import run_script_path
    from tests.test_preflight_recovery_audit import PYTHON
    # Explicit environment prefix and separate bounded capability stubs are
    # part of the exact approved body, as required by the current script path.
    # This does not consult server Conda/scientific executables or legacy auto binding.
    code='export BIOMIND_SYNTHETIC_RUNTIME_SCOPE="approved-new-environment"\n'
    code+='dictionary_stub() { return 0; }; markduplicates_stub() { return 9; }\n'
    code+='dictionary_stub || exit 21\n'
    code+='if markduplicates_stub; then exit 22; else printf "markduplicates unavailable; dictionary available\\n" >&2; fi\n'
    code+=shlex.quote(PYTHON)+" -I -S - <<'PY'\nimport os\nassert os.environ['BIOMIND_SYNTHETIC_RUNTIME_SCOPE']=='approved-new-environment'\nprint('explicit environment reached; dictionary stage allowed; dedup blocked')\nPY\n"
    path=tmp_path/'new-approved.sh';middleware=HITLMiddleware(auto_approve_safe=False)
    with patch('agent_core.middleware.interrupt',return_value='y'):
        verdict,body=middleware.review_code(code,'Bash')
    assert verdict=='approve' and body==code
    path.write_text(body);sha=hashlib.sha256(path.read_bytes()).hexdigest()
    clean={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'};bootstrap=sys.modules['__main__']
    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)]=clean
    try:out,err,rc=asyncio.run(run_script_path(path,expected_sha256=sha,env=clean,profile=False))
    finally:
        bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path),None)
    assert rc==0 and 'explicit environment reached' in out and 'markduplicates unavailable' in err
    assert hashlib.sha256(path.read_bytes()).hexdigest()==sha
    save('F32-F33-explicit-environment',{'approved_script':body,'approved_sha256':sha,'stdout':out,'stderr':err,'rc':rc,
        'approvals':1,'executions':1,'capabilities':{'dictionary':'synthetic_available','markduplicates':'synthetic_unavailable'},
        'actual_scientific_environment':'NOT_TESTED','automatic_tool_selection':False})


