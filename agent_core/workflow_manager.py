import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class WorkflowManager:
    """Load workflow templates that define stage coverage before skill use."""

    def __init__(self, workflows_root: str):
        self.workflows_root = Path(workflows_root)
        self.workflows: Dict[str, Dict[str, Any]] = {}
        self.workflow_aliases: Dict[str, str] = {}
        self._discover_workflows()

    def _discover_workflows(self) -> None:
        if not self.workflows_root.is_dir():
            return
        for path in sorted(self.workflows_root.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            workflow_id = self._normalize_workflow_id(data.get("workflow_id") or path.stem)
            if not workflow_id:
                continue
            data["workflow_id"] = workflow_id
            data["_workflow_path"] = str(path.resolve())
            data["stages"] = self._normalize_stages(data.get("stages") or [])
            self.workflows[workflow_id] = data
            self.workflow_aliases[workflow_id] = workflow_id
            for alias in self._ensure_list(data.get("aliases")):
                normalized_alias = self._normalize_workflow_id(alias)
                if normalized_alias:
                    self.workflow_aliases.setdefault(normalized_alias, workflow_id)

    @staticmethod
    def _normalize_workflow_id(value: Any) -> str:
        return str(value or "").replace("\\", "/").strip().strip("/")

    @staticmethod
    def _ensure_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple) or isinstance(value, set):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,\n;]+", value) if part.strip()]
        return [str(value).strip()] if str(value).strip() else []

    def _normalize_stages(self, stages: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        if not isinstance(stages, list):
            return normalized
        for index, item in enumerate(stages, start=1):
            if isinstance(item, str):
                stage = {"stage": item}
            elif isinstance(item, dict):
                stage = dict(item)
            else:
                continue
            stage_id = str(stage.get("stage") or stage.get("stage_id") or "").strip()
            if not stage_id:
                continue
            stage["stage"] = stage_id
            stage["batch_id"] = str(stage.get("batch_id") or stage.get("batch") or index).strip()
            for key in ("task_types", "inputs", "outputs", "optional_inputs", "required_inputs"):
                if key in stage:
                    stage[key] = self._ensure_list(stage.get(key))
            if stage.get("default_skill"):
                stage["default_skill"] = str(stage["default_skill"]).strip()
            normalized.append(stage)
        return normalized

    def has_workflow(self, workflow_id: str) -> bool:
        return bool(self.resolve_workflow_id(workflow_id))

    def resolve_workflow_id(self, workflow_id: str) -> str:
        normalized = self._normalize_workflow_id(workflow_id)
        return self.workflow_aliases.get(normalized, "")

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        resolved = self.resolve_workflow_id(workflow_id)
        return dict(self.workflows.get(resolved) or {})

    def get_workflow_catalog_prompt(self) -> str:
        if not self.workflows:
            return "<workflow_catalog>\nNo workflow templates available.\n</workflow_catalog>"
        lines = [
            "<workflow_catalog>",
            "Workflow templates define required stages. Use a workflow only when it clearly matches the requested analysis; otherwise skip workflow handoff and select local skill(s) directly.",
        ]
        for workflow_id in sorted(self.workflows):
            workflow = self.workflows[workflow_id]
            task_types = ", ".join(self._ensure_list(workflow.get("task_types"))) or "none"
            triggers = ", ".join(self._ensure_list(workflow.get("triggers"))) or "none"
            stages = ", ".join(stage.get("stage", "") for stage in workflow.get("stages", []) if stage.get("stage")) or "none"
            aliases = ", ".join(self._ensure_list(workflow.get("aliases"))) or "none"
            lines.extend([
                f"- workflow_id: {workflow_id}",
                f"  aliases: {aliases}",
                f"  name: {workflow.get('name', workflow_id)}",
                f"  task_types: {task_types}",
                f"  triggers: {triggers}",
                f"  stages: {stages}",
                f"  description: {workflow.get('description', 'No description')}",
            ])
        lines.append("</workflow_catalog>")
        return "\n".join(lines)

    def load_workflow_prompt(self, workflow_id: str) -> str:
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            return ""
        public = {key: value for key, value in workflow.items() if not key.startswith("_")}
        return yaml.safe_dump(public, allow_unicode=True, sort_keys=False)

    def get_batch_skills(self, workflow_id: str, batch_id: Any) -> List[str]:
        workflow = self.get_workflow(workflow_id)
        batch = str(batch_id or "").strip()
        skills: List[str] = []
        for stage in workflow.get("stages", []):
            if str(stage.get("batch_id") or "").strip() != batch:
                continue
            skill = str(stage.get("default_skill") or "").strip()
            if skill and skill not in skills:
                skills.append(skill)
        return skills


def parse_workflow_handoff(text: str) -> Dict[str, Any]:
    payload = str(text or "")
    decision = _parse_yaml_block(payload, "WORKFLOW_DECISION", default={})
    stages = _parse_yaml_block(payload, "WORKFLOW_STAGES", default=[])
    if isinstance(stages, dict):
        stages = stages.get("stages", [])
    if not isinstance(stages, list):
        stages = []

    active_skills = _parse_skill_block(payload, "ACTIVE_SKILLS")
    future_skills = _parse_skill_block(payload, "FUTURE_SKILLS")
    required_all = _parse_skill_block(payload, "REQUIRED_SKILLS_ALL")
    candidate_skills = _parse_candidate_block(payload)

    workflow_id = ""
    active_batch_id = ""
    if isinstance(decision, dict):
        workflow_id = str(decision.get("workflow_id") or "").strip()
        active_batch_id = str(decision.get("active_batch_id") or decision.get("current_batch") or "").strip()
    for stage in stages:
        if isinstance(stage, dict) and stage.get("active_now") is True and not active_batch_id:
            active_batch_id = str(stage.get("batch_id") or "").strip()

    return {
        "workflow_id": workflow_id,
        "workflow_decision": decision if isinstance(decision, dict) else {},
        "workflow_stages": [dict(stage) for stage in stages if isinstance(stage, dict)],
        "active_batch_id": active_batch_id,
        "active_skills": active_skills,
        "future_skills": future_skills,
        "required_skills_all": required_all,
        "candidate_skills": candidate_skills,
    }


def validate_workflow_handoff(
    handoff: Dict[str, Any],
    skill_manager,
    workflow_manager: Optional[WorkflowManager] = None,
    max_active_skills: int = 3,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    normalized = dict(handoff or {})
    workflow_id = str(normalized.get("workflow_id") or "").strip()
    stages = normalized.get("workflow_stages") or []
    is_workflow = bool(workflow_id or stages)

    if workflow_id and workflow_manager:
        resolved_workflow_id = workflow_manager.resolve_workflow_id(workflow_id)
        if not resolved_workflow_id:
            errors.append(f"Unknown workflow_id: {workflow_id}")
        else:
            workflow_id = resolved_workflow_id
            normalized["workflow_id"] = resolved_workflow_id
    if is_workflow and not stages:
        errors.append("Workflow task must include WORKFLOW_STAGES.")

    active_skills = _normalize_skills(skill_manager, normalized.get("active_skills") or [])
    future_skills = _normalize_skills(skill_manager, normalized.get("future_skills") or [])
    required_skills_all = _normalize_skills(skill_manager, normalized.get("required_skills_all") or [])
    candidate_skills = _normalize_skills(skill_manager, normalized.get("candidate_skills") or [], keep_unknown=True)
    normalized["active_skills"] = active_skills
    normalized["future_skills"] = future_skills
    normalized["required_skills_all"] = required_skills_all
    normalized["candidate_skills"] = candidate_skills

    if len(active_skills) > max_active_skills:
        errors.append(f"Too many active skills: {len(active_skills)} > {max_active_skills}")

    stage_names = set()
    skill_to_stage: Dict[str, List[str]] = {}
    active_stage_skills: List[str] = []
    normalized_stages: List[Dict[str, Any]] = []
    active_batch_id = str(normalized.get("active_batch_id") or "").strip()

    for raw_stage in stages:
        stage = dict(raw_stage or {})
        stage_name = str(stage.get("stage") or stage.get("stage_id") or "").strip()
        status = str(stage.get("status") or "").strip()
        skill = str(stage.get("skill") or "").strip()
        if not stage_name:
            errors.append("Each stage must have a stage name.")
            continue
        if stage_name in stage_names:
            errors.append(f"Duplicate stage: {stage_name}")
        stage_names.add(stage_name)
        if status not in {"selected_skill", "no_skill_needed", "blocked"}:
            errors.append(f"Stage {stage_name} has invalid status: {status}")

        stage["stage"] = stage_name
        stage["status"] = status
        stage["batch_id"] = str(stage.get("batch_id") or "").strip()

        if status == "selected_skill":
            canonical = skill_manager.normalize_selected_skills([skill], max_results=1)[0:1] if skill_manager else []
            if not skill or skill == "none":
                errors.append(f"Stage {stage_name} selected_skill but no skill provided.")
            elif not canonical:
                errors.append(f"Stage {stage_name} selected unknown skill: {skill}")
            else:
                skill = canonical[0]
                stage["skill"] = skill
                meta = skill_manager.get_skill_meta(skill)
                stage_tags = skill_manager.metadata_list(meta, "stage_tags") if hasattr(skill_manager, "metadata_list") else []
                if stage_tags and stage_name not in stage_tags:
                    errors.append(f"Skill {skill} does not declare support for stage {stage_name}.")
                elif not stage_tags:
                    warnings.append(f"Skill {skill} has no stage_tags; accepted via legacy fallback for stage {stage_name}.")
                if skill in skill_to_stage:
                    previous_stages = skill_to_stage[skill]
                    covered_stages = [*previous_stages, stage_name]
                    if not stage_tags or not all(item in stage_tags for item in covered_stages):
                        errors.append(
                            f"Skill {skill} selected for multiple stages: {', '.join(previous_stages)} and {stage_name}"
                        )
                skill_to_stage.setdefault(skill, []).append(stage_name)
                if stage.get("active_now") is True or (active_batch_id and stage["batch_id"] == active_batch_id):
                    active_stage_skills.append(skill)
        elif status == "no_skill_needed":
            if not str(stage.get("reason") or "").strip():
                errors.append(f"Stage {stage_name} no_skill_needed requires reason.")
            if not str(stage.get("direct_method") or "").strip():
                errors.append(f"Stage {stage_name} no_skill_needed requires direct_method.")
        elif status == "blocked":
            if not str(stage.get("reason") or "").strip():
                errors.append(f"Stage {stage_name} blocked requires reason.")

        normalized_stages.append(stage)

    if not active_stage_skills and active_skills:
        selected_stage_skills = [
            str(stage.get("skill") or "").strip()
            for stage in normalized_stages
            if stage.get("status") == "selected_skill"
        ]
        active_stage_skills = [skill for skill in selected_stage_skills if skill in active_skills]

    extra = sorted(set(active_skills) - set(active_stage_skills))
    if extra:
        errors.append(f"Active skills not tied to active stages: {extra}")
    missing = sorted(set(active_stage_skills) - set(active_skills))
    if missing:
        errors.append(f"Active stages missing active skill load: {missing}")

    normalized["workflow_stages"] = normalized_stages
    return {"errors": errors, "warnings": warnings, "handoff": normalized}


def _block(text: str, name: str) -> str:
    match = re.search(rf"\[{name}\]\s*(.*?)\s*\[/{name}\]", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _parse_yaml_block(text: str, name: str, default: Any) -> Any:
    content = _block(text, name)
    if not content:
        return default
    try:
        parsed = yaml.safe_load(content)
    except Exception:
        return default
    return parsed if parsed is not None else default


def _parse_skill_block(text: str, name: str) -> List[str]:
    content = _block(text, name)
    if not content:
        return []
    skills: List[str] = []
    for item in re.split(r"[,\n;，；]+", content):
        cleaned = re.sub(r"^\s*[-*+]\s*", "", item).strip().strip("`'\"")
        if cleaned and cleaned.lower() not in {"none", "n/a", "null"} and cleaned not in skills:
            skills.append(cleaned)
    return skills


def _parse_candidate_block(text: str) -> List[str]:
    content = _block(text, "CANDIDATE_SKILLS")
    if not content:
        return []
    skills: List[str] = []
    for line in content.splitlines():
        cleaned = re.sub(r"^\s*[-*+]\s*", "", line).strip()
        if not cleaned:
            continue
        skill = re.split(r"\s*[:：]\s*", cleaned, maxsplit=1)[0].strip().strip("`'\"")
        if skill and skill.lower() not in {"none", "n/a", "null"} and skill not in skills:
            skills.append(skill)
    return skills


def _normalize_skills(skill_manager, skills: List[str], keep_unknown: bool = False) -> List[str]:
    normalized: List[str] = []
    for skill in skills or []:
        canonical = skill_manager.normalize_selected_skills([skill], max_results=1)[0:1] if skill_manager else []
        value = canonical[0] if canonical else (str(skill).strip() if keep_unknown else "")
        if value and value not in normalized:
            normalized.append(value)
    return normalized
