"""Real output projection/authority checks; synthetic paths, no process or API."""
import pytest

from agent_core.work_outputs import materialize_outputs, materialize_plan_outputs, WorkOutputError


def state(project, contracts=()):
    return {'task_id':'output-admission-authority',
        'plan_document':{'path_bindings':{'project_dir':str(project)},
                         'parameters':{'samples':['leaf']}},
        'current_task':{'task_spec':{'revision':1, 'goal':'Write the declared output.',
            'path_context':{'project_dir':str(project)},
            'output_contracts':list(contracts)}}}


def test_authorized_external_exact_sample_expansion_preserves_original_projection(tmp_path):
    project=tmp_path/'project';project.mkdir()
    external=tmp_path/'external';external.mkdir()
    target=external/'leaf.bed'
    current=state(project,[{'artifact_id':'leaf_output','canonical_path':str(target),
        'artifact_kind':'file','source':'user_explicit'}])
    rows=[str(external/'{sample}.bed')]*2
    strict=materialize_outputs(current,rows,project)
    assert len(strict)==1 and strict[0]['canonical_path']==str(target)
    diagnostics=[]
    assert materialize_plan_outputs(current,rows,project,diagnostics=diagnostics)==strict
    assert diagnostics==[]


def test_conflicting_known_unauthorized_destination_is_not_deferred(tmp_path):
    project=tmp_path/'project';project.mkdir()
    current=state(project)
    current['plan_document']['path_bindings']['result']=str(project/'leaf.bed')
    current['plan_document']['output_contracts']=[{'artifact_id':'result',
        'path':str(tmp_path/'outside'/'{sample}.bed')}]
    with pytest.raises(WorkOutputError) as caught:
        materialize_plan_outputs(current,[{'artifact_ref':'result'}],project)
    assert caught.value.issue['code']=='OUTPUT_PATH_NOT_AUTHORIZED'


def test_missing_reference_explicit_unauthorized_fallback_is_not_deferred(tmp_path):
    project=tmp_path/'project';project.mkdir()
    current=state(project)
    with pytest.raises(WorkOutputError) as caught:
        materialize_plan_outputs(current,[{'artifact_ref':'not_bound',
            'path':str(tmp_path/'outside'/'leaf.bed')}],project)
    assert caught.value.issue['code']=='OUTPUT_PATH_NOT_AUTHORIZED'


@pytest.mark.parametrize('path',['.env.private/{UNKNOWN}/result.txt','../outside/{UNKNOWN}/result.txt'])
def test_unknown_template_cannot_hide_private_or_unauthorized_target(tmp_path,path):
    project=tmp_path/'project';project.mkdir()
    with pytest.raises(WorkOutputError) as caught:
        materialize_plan_outputs(state(project),[str(project/path)],project)
    assert caught.value.issue['code']=='OUTPUT_PATH_NOT_AUTHORIZED'


def test_conflicting_authorized_exact_templates_are_retained_without_fake_path(tmp_path):
    project=tmp_path/'project';project.mkdir()
    external=tmp_path/'external';external.mkdir()
    targets=[external/'leaf.bed',external/'leaf.tsv']
    current=state(project,[{'canonical_path':str(target),'artifact_kind':'file',
                          'source':'user_explicit'} for target in targets])
    current['plan_document']['path_bindings']['result']=str(external/'{sample}.bed')
    current['plan_document']['output_contracts']=[{'artifact_id':'result',
        'path':str(external/'{sample}.tsv')}]
    declared=[{'artifact_ref':'result'}];diagnostics=[]
    assert materialize_plan_outputs(current,declared,project,diagnostics=diagnostics)==declared
    assert diagnostics[0]['code']=='OUTPUT_MAPPING_CONFLICT'
    with pytest.raises(WorkOutputError,match='OUTPUT_MAPPING_CONFLICT'):
        materialize_outputs(current,declared,project)
