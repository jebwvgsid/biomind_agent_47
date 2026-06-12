from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


BENCHMARK_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BENCHMARK_DIR.parents[1]
TASKS_PATH = BENCHMARK_DIR / "questions" / "tasks.yaml"
MCQ_PATH = BENCHMARK_DIR / "questions" / "mcq.yaml"
ANSWER_PATH = BENCHMARK_DIR / "answers" / "expected_answers.yaml"
LEGACY_TOY_FIXTURES_MANIFEST_PATH = BENCHMARK_DIR / "manifests" / "legacy_toy_fixtures_manifest.yaml"
PROFESSIONAL_FIXTURES_MANIFEST_PATH = BENCHMARK_DIR / "manifests" / "professional_fixtures_manifest.yaml"
PUBLIC_FIXTURES_MANIFEST_PATH = BENCHMARK_DIR / "manifests" / "public_fixtures_manifest.yaml"
SKILLS_ROOT = REPO_ROOT / "skills"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not load as a mapping")
    return data


def discover_skill_ids(skills_root: Path = SKILLS_ROOT) -> set[str]:
    return {
        skill_md.parent.relative_to(skills_root).as_posix()
        for skill_md in sorted(skills_root.rglob("SKILL.md"))
        if ".skill_runtime_registry" not in skill_md.parts
    }


def _require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def _task_skill_ids(task: dict[str, Any]) -> set[str]:
    return set(task.get("expected_skills") or [])


def _file_entry_path(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return str(entry)


def _file_entry_availability(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("availability") or "available")
    return "available"


def _validate_skill_list(
    errors: list[str],
    task_id: str,
    key: str,
    task: dict[str, Any],
    skill_ids: set[str],
) -> set[str]:
    values = set(task.get(key) or [])
    unknown = sorted(values - skill_ids)
    _require(not unknown, errors, f"{task_id}: unknown {key}: {unknown}")
    return values


def validate_tasks(data: dict[str, Any], skill_ids: set[str], repo_root: Path = REPO_ROOT) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    tasks = data.get("tasks")
    _require(isinstance(tasks, list) and bool(tasks), errors, "tasks.yaml must contain a non-empty tasks list")
    if not isinstance(tasks, list):
        return errors, set(skill_ids)

    seen_ids: set[str] = set()
    covered: set[str] = set()
    for task in tasks:
        _require(isinstance(task, dict), errors, "each task must be a mapping")
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        _require(bool(re.match(r"^BM-[A-Z0-9-]+$", task_id)), errors, f"invalid task id: {task_id!r}")
        _require(task_id not in seen_ids, errors, f"duplicate task id: {task_id}")
        seen_ids.add(task_id)

        for key in ("title", "category", "question_type", "execution_mode", "difficulty", "prompt"):
            _require(bool(task.get(key)), errors, f"{task_id}: missing {key}")

        expected_skills = _task_skill_ids(task)
        _require(bool(expected_skills), errors, f"{task_id}: expected_skills is empty")
        unknown = sorted(expected_skills - skill_ids)
        _require(not unknown, errors, f"{task_id}: unknown expected skill ids: {unknown}")
        _validate_skill_list(errors, task_id, "optional_skills", task, skill_ids)
        _validate_skill_list(errors, task_id, "forbidden_skills", task, skill_ids)
        covered.update(expected_skills)

        reference_steps = task.get("reference_steps") or []
        milestones = task.get("milestones") or []
        _require(isinstance(reference_steps, list) and bool(reference_steps), errors, f"{task_id}: missing reference_steps")
        _require(isinstance(milestones, list) and bool(milestones), errors, f"{task_id}: missing milestones")
        for milestone in milestones:
            _require(isinstance(milestone, dict), errors, f"{task_id}: milestone must be a mapping")
            if isinstance(milestone, dict):
                _require(bool(milestone.get("id")), errors, f"{task_id}: milestone missing id")
                _require(bool(milestone.get("description")), errors, f"{task_id}: milestone missing description")
                _require(isinstance(milestone.get("points"), (int, float)), errors, f"{task_id}: milestone missing numeric points")

        scoring = task.get("scoring") or {}
        _require(isinstance(scoring, dict), errors, f"{task_id}: scoring must be a mapping")
        if isinstance(scoring, dict):
            _require(scoring.get("total_points") == 5, errors, f"{task_id}: expected total_points == 5")
            _require(scoring.get("pass_threshold", 0) >= 4, errors, f"{task_id}: pass_threshold should be at least 4")
            _require(bool(scoring.get("hard_failures")), errors, f"{task_id}: hard_failures missing")

        for rel_path in task.get("repo_files") or []:
            path = repo_root / rel_path
            _require(path.exists(), errors, f"{task_id}: repo file does not exist: {rel_path}")

        data_files = task.get("data_files") or []
        _require(isinstance(data_files, list), errors, f"{task_id}: data_files must be a list")
        for entry in data_files:
            _require(isinstance(entry, dict), errors, f"{task_id}: data_files entries must be mappings")
            if not isinstance(entry, dict):
                continue
            rel_path = str(entry.get("path") or "")
            availability = str(entry.get("availability") or "")
            _require(bool(rel_path), errors, f"{task_id}: data file missing path")
            _require(bool(entry.get("role")), errors, f"{task_id}: data file missing role for {rel_path}")
            _require(availability in {"available", "planned"}, errors, f"{task_id}: invalid data file availability for {rel_path}")
            _require(rel_path in str(task.get("prompt") or ""), errors, f"{task_id}: prompt does not include data path: {rel_path}")
            if availability == "available":
                path = repo_root / rel_path
                _require(path.exists(), errors, f"{task_id}: available data file does not exist: {rel_path}")

        if task.get("question_type") == "result_choice_after_analysis":
            result_question = task.get("result_question") or {}
            _require(isinstance(result_question, dict), errors, f"{task_id}: result_question must be a mapping")
            if isinstance(result_question, dict):
                options = result_question.get("options") or {}
                correct = result_question.get("correct_answer")
                _require(bool(result_question.get("prompt")), errors, f"{task_id}: result_question missing prompt")
                _require(isinstance(options, dict) and len(options) >= 2, errors, f"{task_id}: result_question options missing")
                _require(isinstance(correct, str) and correct in set(options), errors, f"{task_id}: correct_answer not found in options")

    uncovered = sorted(skill_ids - covered)
    return errors, set(uncovered)


def validate_mcq(data: dict[str, Any], skill_ids: set[str]) -> list[str]:
    errors: list[str] = []
    questions = data.get("questions")
    _require(isinstance(questions, list) and bool(questions), errors, "mcq.yaml must contain a non-empty questions list")
    if not isinstance(questions, list):
        return errors

    seen_ids: set[str] = set()
    for item in questions:
        _require(isinstance(item, dict), errors, "each MCQ item must be a mapping")
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "")
        _require(bool(re.match(r"^BM-MCQ-\d{3}$", qid)), errors, f"invalid MCQ id: {qid!r}")
        _require(qid not in seen_ids, errors, f"duplicate MCQ id: {qid}")
        seen_ids.add(qid)

        options = item.get("options") or {}
        answer = item.get("answer")
        _require(isinstance(options, dict) and len(options) >= 2, errors, f"{qid}: options must contain at least two entries")
        option_keys = set(options.keys()) if isinstance(options, dict) else set()
        if item.get("type") == "multi_select":
            _require(isinstance(answer, list) and bool(answer), errors, f"{qid}: multi_select answer must be a list")
            for key in answer or []:
                _require(key in option_keys, errors, f"{qid}: answer option not found: {key}")
        else:
            _require(isinstance(answer, str) and answer in option_keys, errors, f"{qid}: answer option not found: {answer}")

        expected_skills = set(item.get("expected_skills") or [])
        _require(bool(expected_skills), errors, f"{qid}: expected_skills is empty")
        unknown = sorted(expected_skills - skill_ids)
        _require(not unknown, errors, f"{qid}: unknown expected skill ids: {unknown}")
        _require(bool(item.get("rationale")), errors, f"{qid}: missing rationale")

    return errors


def validate_expected_answers(
    answer_data: dict[str, Any],
    task_data: dict[str, Any],
    mcq_data: dict[str, Any],
    skill_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    source_alignment = answer_data.get("source_alignment") or {}
    _require(isinstance(source_alignment, dict), errors, "expected_answers.yaml: source_alignment must be a mapping")
    _require(
        bool(source_alignment.get("reference_benchmark")),
        errors,
        "expected_answers.yaml: source_alignment.reference_benchmark is missing",
    )

    open_answers = answer_data.get("open_task_answers") or {}
    _require(isinstance(open_answers, dict), errors, "expected_answers.yaml: open_task_answers must be a mapping")
    if not isinstance(open_answers, dict):
        open_answers = {}
    tasks = [task for task in task_data.get("tasks") or [] if isinstance(task, dict)]
    task_ids = {str(task.get("id")) for task in tasks}
    answer_task_ids = set(open_answers)
    _require(answer_task_ids == task_ids, errors, "expected_answers.yaml: open_task_answers ids do not match tasks.yaml ids")

    for task in tasks:
        task_id = str(task.get("id"))
        answer = open_answers.get(task_id) or {}
        _require(isinstance(answer, dict), errors, f"{task_id}: answer entry must be a mapping")
        if not isinstance(answer, dict):
            continue
        answer_skills = set(answer.get("expected_skills") or [])
        task_skills = set(task.get("expected_skills") or [])
        _require(answer_skills == task_skills, errors, f"{task_id}: answer expected_skills do not match task")
        _require(
            set(answer.get("optional_skills") or []) == set(task.get("optional_skills") or []),
            errors,
            f"{task_id}: answer optional_skills do not match task",
        )
        _require(
            set(answer.get("forbidden_skills") or []) == set(task.get("forbidden_skills") or []),
            errors,
            f"{task_id}: answer forbidden_skills do not match task",
        )
        unknown = sorted(answer_skills - skill_ids)
        _require(not unknown, errors, f"{task_id}: answer unknown expected skill ids: {unknown}")
        _require(
            len(answer.get("required_reference_steps") or []) == len(task.get("reference_steps") or []),
            errors,
            f"{task_id}: answer reference step count does not match task",
        )
        _require(
            len(answer.get("reference_milestones") or []) == len(task.get("milestones") or []),
            errors,
            f"{task_id}: answer milestone count does not match task",
        )
        scoring = answer.get("scoring") or {}
        task_scoring = task.get("scoring") or {}
        _require(isinstance(scoring, dict), errors, f"{task_id}: answer scoring must be a mapping")
        if isinstance(scoring, dict):
            _require(scoring.get("total_points") == task_scoring.get("total_points"), errors, f"{task_id}: answer total_points mismatch")
            _require(scoring.get("pass_threshold") == task_scoring.get("pass_threshold"), errors, f"{task_id}: answer pass_threshold mismatch")
            _require(
                list(scoring.get("hard_failures") or []) == list(task_scoring.get("hard_failures") or []),
                errors,
                f"{task_id}: answer hard_failures mismatch",
            )
        if task.get("question_type") == "result_choice_after_analysis":
            task_rq = task.get("result_question") or {}
            answer_rq = answer.get("result_question") or {}
            _require(isinstance(answer_rq, dict), errors, f"{task_id}: answer result_question must be a mapping")
            if isinstance(task_rq, dict) and isinstance(answer_rq, dict):
                _require(answer_rq.get("correct_answer") == task_rq.get("correct_answer"), errors, f"{task_id}: answer correct_answer mismatch")
                if "options" in answer_rq:
                    _require(answer_rq.get("options") == task_rq.get("options"), errors, f"{task_id}: answer result options mismatch")

    mcq_answers = answer_data.get("mcq_answers") or {}
    _require(isinstance(mcq_answers, dict), errors, "expected_answers.yaml: mcq_answers must be a mapping")
    if not isinstance(mcq_answers, dict):
        mcq_answers = {}
    mcq_items = [item for item in mcq_data.get("questions") or [] if isinstance(item, dict)]
    mcq_ids = {str(item.get("id")) for item in mcq_items}
    _require(set(mcq_answers) == mcq_ids, errors, "expected_answers.yaml: mcq_answers ids do not match mcq.yaml ids")

    for item in mcq_items:
        qid = str(item.get("id"))
        answer = mcq_answers.get(qid) or {}
        _require(isinstance(answer, dict), errors, f"{qid}: MCQ answer entry must be a mapping")
        if not isinstance(answer, dict):
            continue
        _require(answer.get("answer") == item.get("answer"), errors, f"{qid}: MCQ answer does not match mcq.yaml")
        _require(
            set(answer.get("expected_skills") or []) == set(item.get("expected_skills") or []),
            errors,
            f"{qid}: MCQ expected_skills do not match mcq.yaml",
        )
        _require(bool(answer.get("rationale")), errors, f"{qid}: MCQ answer rationale missing")

    return errors


def resolve_fixture_path(rel_path: str) -> Path:
    path = Path(rel_path)
    if path.is_absolute():
        return path
    repo_candidate = REPO_ROOT / path
    if repo_candidate.exists():
        return repo_candidate
    return BENCHMARK_DIR / path


def validate_fixture_manifest(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return errors
    data = load_yaml(path)
    fixtures = data.get("task_fixtures") or {}
    _require(isinstance(fixtures, dict), errors, "fixtures_manifest.yaml task_fixtures must be a mapping")
    if not isinstance(fixtures, dict):
        return errors
    for task_id, rel_paths in fixtures.items():
        _require(isinstance(rel_paths, list), errors, f"{task_id}: fixture paths must be a list")
        if not isinstance(rel_paths, list):
            continue
        for entry in rel_paths:
            rel_path = _file_entry_path(entry)
            availability = _file_entry_availability(entry)
            _require(availability in {"available", "planned"}, errors, f"{task_id}: invalid fixture availability for {rel_path}")
            if availability == "planned":
                continue
            fixture_path = resolve_fixture_path(rel_path)
            _require(fixture_path.exists(), errors, f"{task_id}: fixture file does not exist: {rel_path}")
    return errors


def validate_benchmark() -> tuple[list[str], dict[str, int]]:
    skill_ids = discover_skill_ids()
    task_data = load_yaml(TASKS_PATH)
    mcq_data = load_yaml(MCQ_PATH)
    answer_data = load_yaml(ANSWER_PATH)
    task_errors, uncovered_skills = validate_tasks(task_data, skill_ids)
    errors = task_errors
    errors.extend(validate_mcq(mcq_data, skill_ids))
    errors.extend(validate_expected_answers(answer_data, task_data, mcq_data, skill_ids))
    errors.extend(validate_fixture_manifest(PROFESSIONAL_FIXTURES_MANIFEST_PATH))
    errors.extend(validate_fixture_manifest(PUBLIC_FIXTURES_MANIFEST_PATH))
    summary = {
        "skills": len(skill_ids),
        "open_tasks": len(task_data.get("tasks") or []),
        "mcq_questions": len(mcq_data.get("questions") or []),
        "open_answers": len(answer_data.get("open_task_answers") or {}),
        "mcq_answers": len(answer_data.get("mcq_answers") or {}),
        "uncovered_skills": len(uncovered_skills),
    }
    return errors, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the BioMind benchmark dataset.")
    parser.parse_args()
    errors, summary = validate_benchmark()
    if errors:
        print("Benchmark validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Benchmark validation passed: "
        f"{summary['open_tasks']} open tasks, "
        f"{summary['mcq_questions']} MCQ questions, "
        f"{summary['open_answers']} open answers, "
        f"{summary['mcq_answers']} MCQ answers, "
        f"{summary['skills']} skills discovered, "
        f"{summary['uncovered_skills']} skills without open-task coverage."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
