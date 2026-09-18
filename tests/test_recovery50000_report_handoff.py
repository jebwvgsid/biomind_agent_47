"""Report ownership is checked in the real Consultant commit before Planner."""
from copy import deepcopy
from pathlib import Path
from agent_core.plan_tracker import load_plan_state, has_materialized_plan
from agent_core.task_spec import create_task_spec
from agent_core.workflow.early_nodes import TypedConsultant
from tests import test_consultant_initial_skill_binding as consultant_fixtures


def test_exact_report_conflict_becomes_durable_question_before_planning(tmp_path):
    fixture = consultant_fixtures.ConsultantInitialSkillBindingTests()
    task_id = "new-report-task"
    before = fixture._admit_initial_state(str(tmp_path), task_id)
    target = tmp_path / "medium" / "report.md"
    target.parent.mkdir(); target.write_text("Old protected report.\n")
    update = fixture._ready_update(task_id)
    update["task_spec"] = create_task_spec("Write report to " + str(target), task_id=task_id,
        base_dir=str(tmp_path), project_root=str(tmp_path), structured_source={"contract_source_type":"user_explicit",
        "expected_outputs":[{"role":"report", "path":str(target), "artifact_kind":"file", "required":True}]})
    agent = fixture._agent(str(tmp_path))
    result = agent._commit_consultant_update(task_id, update)
    assert result["canonical_commit"]["succeeded"]
    assert result["next_step"] == "ask_user"
    durable = load_plan_state(str(tmp_path), task_id)
    pending = durable["task_state"]["pending_question"]
    assert pending == result["pending_human_question"]
    assert pending["reason_code"] == "RUNTIME_REPORT_EXISTING_FILE_PROTECTED"
    assert pending["evidence"]["targets"][0]["path"] == str(target)
    typed = TypedConsultant(data_path=str(tmp_path), source=lambda _: None).classify(before, result, durable)
    assert typed["type"] == "user_input_needed"
    assert not has_materialized_plan(durable)
    assert target.read_text() == "Old protected report.\n"
    # The source-owned TaskSpec has not been silently redirected.
    assert str(target) in str(durable["current_task"]["task_spec"])


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
