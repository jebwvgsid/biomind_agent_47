from copy import deepcopy
from pathlib import Path
import json
import pytest
from agent_core.task_spec import create_task_spec, task_semantic_hash, task_record_hash
from agent_core.approved_resource_locator import approved_resource_locator, ResourceLocatorError
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher, RecoveryToolError
from tests import test_recovery_tools as fixtures

@pytest.mark.parametrize('field',['canonical_path','path','resolved_path'])
def test_formal_locator_real_registry_reader_and_serialized_reload(tmp_path,field):
    fixture=fixtures.RecoveryToolsTests(methodName='runTest')
    fixture.setUp()
    try:
        path=tmp_path/'approved-data.vcf';path.write_text('##contig=<ID=chr1,length=4>\nchr1\n')
        durable=deepcopy(fixture.durable)
        task=create_task_spec("Read approved input",task_id="task-recovery",base_dir=tmp_path,project_root=tmp_path,structured_source={"input_files":[{"role":"variant_vcf","path":str(path),"format":"vcf"}]})
        row=task['inputs'][0]
        ref='artifact:'+row['artifact_id']
        # Formal canonical record; legacy compatibility is covered below.
        durable['current_task'].update(task_spec=task,task_revision=task['revision'],task_semantic_hash=task_semantic_hash(task),task_record_hash=task_record_hash(task))
        for reloaded in [durable,json.loads(json.dumps(durable))]:
            registry=build_recovery_inspection_registry(fixture.seed,reloaded,phase_store=fixture.phase_store)
            dispatcher=RecoveryLocalToolDispatcher(seed=fixture.seed,durable=reloaded,registry=registry,phase_store=fixture.phase_store)
            inspected=dispatcher.dispatch('artifact.inspect',{'ref_id':ref})
            read=dispatcher.dispatch('artifact.read',{'ref_id':ref})
            assert inspected['exists'] and inspected['is_file']
            assert '##contig=<ID=chr1' in read['content']
            with pytest.raises(RecoveryToolError,match='AUTHORITY_ARGUMENT_FORBIDDEN'):
                dispatcher.dispatch('artifact.read',{'ref_id':ref,'path':'/unapproved'})
            with pytest.raises(RecoveryToolError,match='NOT_DECLARED'):
                dispatcher.dispatch('artifact.read',{'ref_id':'another-task-input'})
    finally: fixture.tearDown()

@pytest.mark.parametrize('row,code',[
    ({'value':'/tmp/arbitrary'},'ARTIFACT_LOCATOR_MISSING'),
    ({'canonical_path':'/tmp/a','path':'/tmp/b'},'ARTIFACT_PATH_CONFLICT'),
    ({'canonical_path':'relative'},'ARTIFACT_TRUSTED_ROOT_MISSING'),
    ({'canonical_path':'/tmp/../escape'},'ARTIFACT_PATH_DENIED'),
])
def test_untrusted_or_conflicting_locator_rejected(row,code):
    with pytest.raises(ResourceLocatorError,match=code): approved_resource_locator(row)

def test_declared_root_and_symlink_escape(tmp_path):
    path=tmp_path/'data';path.mkdir();(path/'input').write_text('input')
    assert approved_resource_locator({'path':'input'},trusted_root=str(path))['path']==str(path/'input')
    link=tmp_path/'link';link.symlink_to(path,target_is_directory=True)
    with pytest.raises(ResourceLocatorError,match='SYMLINK_DENIED'):
        approved_resource_locator({'canonical_path':str(link/'input')})


@pytest.mark.parametrize("field", ["canonical_path", "path", "resolved_path"])
def test_legacy_and_formal_source_fields_are_explicit(tmp_path, field):
    path=tmp_path/"file";path.write_text("bounded input")
    value=approved_resource_locator({field:str(path)})
    assert value["source_fields"]==[field] and value["path"]==str(path)


def test_secure_open_rejects_link_swap(tmp_path):
    from agent_core.approved_resource_locator import open_approved_resource
    path=tmp_path/"large";path.write_bytes(b"A"*200000)
    with open_approved_resource(path) as stream: assert stream.read(12)==b"A"*12
    path.unlink();path.symlink_to(tmp_path/"elsewhere")
    with pytest.raises(ResourceLocatorError,match="SYMLINK_DENIED"):
        with open_approved_resource(path): pass
