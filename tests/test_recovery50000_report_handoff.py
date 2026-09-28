"""Report ownership is checked in the real Consultant commit before Planner."""
from copy import deepcopy
from pathlib import Path
from agent_core.plan_tracker import load_plan_state, has_materialized_plan
from agent_core.task_spec import create_task_spec
from tests import test_consultant_initial_skill_binding as consultant_fixtures




def test_empty_exact_report_path_can_reach_original_planning_handoff(tmp_path):
    fixture = consultant_fixtures.ConsultantInitialSkillBindingTests(); task_id="ready-report-task"
    fixture._admit_initial_state(str(tmp_path), task_id)
    target=tmp_path/"allowed"/"report.md"
    update=fixture._ready_update(task_id)
    update["task_spec"]=create_task_spec("Write report", task_id=task_id,
        base_dir=str(tmp_path), project_root=str(tmp_path), structured_source={"contract_source_type":"user_explicit",
        "expected_outputs":[{"role":"report", "path":str(target), "artifact_kind":"file", "required":True}]})
    result=fixture._agent(str(tmp_path))._commit_consultant_update(task_id,update)
    assert result["next_step"]=="planner"
    assert result["consultation_state"]["report_publication_preflight"]["status"]=="ready"
    assert not target.parent.exists()
