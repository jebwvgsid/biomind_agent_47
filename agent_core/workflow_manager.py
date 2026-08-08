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
        normalized = str(value or "").replace("\\", "/").strip().strip("/")
        if normalized.lower() in {"none", "null", "nil", "n/a", "na", "-"}:
            return ""
        return normalized

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

    def candidate_workflow_ids_for_prompt(self, text: str, max_results: int = 3) -> List[str]:
        """Return metadata matches for a compact Consultant context.

        This is retrieval only. It does not decide that a workflow applies; the
        Consultant must still load and semantically validate the candidate.
        """
        query = str(text or "").lower()
        if not query or not self.workflows:
            return []

        def phrase_present(value: Any) -> bool:
            phrase = str(value or "").strip().lower()
            if not phrase:
                return False
            if re.search(r"[^\x00-\x7f]", phrase):
                return phrase in query
            tokens = [token for token in re.split(r"[^a-z0-9]+", phrase) if token]
            if not tokens:
                return False
            pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, tokens)) + r"(?![a-z0-9])"
            return bool(re.search(pattern, query))

        # Explicit canonical IDs and aliases are authoritative retrieval hints.
        explicit: List[str] = []
        for workflow_id, workflow in self.workflows.items():
            identifiers = [workflow_id, *self._ensure_list(workflow.get("aliases"))]
            if any(phrase_present(identifier) for identifier in identifiers):
                explicit.append(workflow_id)
        if explicit:
            return explicit[: max(1, int(max_results or 1))]

        generic_tokens = {
            "analysis", "workflow", "standard", "complete", "data", "input",
            "output", "report", "summary", "stage", "task", "result", "run",
            "annotation", "genome", "sample", "file", "project", "custom",
        }
        query_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", query)
            if len(token) >= 3 and token not in generic_tokens
        }
        scored: List[tuple[int, str]] = []
        for workflow_id, workflow in self.workflows.items():
            exclude_when = workflow.get("exclude_when")
            if isinstance(exclude_when, dict):
                excluded = self._ensure_list(exclude_when.get("any"))
            else:
                excluded = self._ensure_list(exclude_when)
            if any(phrase_present(value) for value in excluded):
                continue
            triggers = self._ensure_list(workflow.get("triggers"))
            task_types = self._ensure_list(workflow.get("task_types"))
            exact_matches = sum(1 for value in [*triggers, *task_types] if phrase_present(value))
            primary_text = " ".join([
                workflow_id,
                str(workflow.get("name") or ""),
                *triggers,
                *task_types,
            ]).lower()
            field_tokens = {
                token
                for token in re.split(r"[^a-z0-9]+", primary_text)
                if len(token) >= 3 and token not in generic_tokens
            }
            overlap = query_tokens & field_tokens
            # One generic substring is not enough.  Accept an exact structured
            # trigger/task type or at least two distinctive tokens.  A single
            # token such as "filtered" from a VCF filename is not sufficient.
            if exact_matches or len(overlap) >= 2:
                scored.append((exact_matches * 10 + len(overlap), workflow_id))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [workflow_id for _, workflow_id in scored[: max(1, int(max_results or 1))]]

    def get_workflow_catalog_prompt(
        self,
        workflow_ids: Optional[List[str]] = None,
        max_entries: Optional[int] = None,
        description_chars: int = 240,
    ) -> str:
        if not self.workflows:
            return "<workflow_catalog>\nNo workflow templates available.\n</workflow_catalog>"
        compact = bool(workflow_ids) or max_entries is not None or description_chars != 240
        chosen_ids = [
            self.resolve_workflow_id(workflow_id)
            for workflow_id in (workflow_ids or [])
            if self.resolve_workflow_id(workflow_id)
        ]
        if chosen_ids:
            selected = [self.workflows[workflow_id] for workflow_id in dict.fromkeys(chosen_ids)]
        else:
            selected = [self.workflows[workflow_id] for workflow_id in sorted(self.workflows)]
        if max_entries is not None:
            selected = selected[: max(0, int(max_entries))]
        lines = [
            "<workflow_catalog>",
            "Use a workflow only when it clearly matches the requested analysis; compare task type, inputs, outputs, and stages before selecting it.",
            "Otherwise, select local skill(s) directly and skip workflow handoff.",
        ]
        for workflow in selected:
            workflow_id = workflow.get("workflow_id", "")
            task_types = ", ".join(self._ensure_list(workflow.get("task_types"))) or "none"
            triggers = ", ".join(self._ensure_list(workflow.get("triggers"))) or "none"
            stages = ", ".join(stage.get("stage", "") for stage in workflow.get("stages", []) if stage.get("stage")) or "none"
            if compact:
                description = str(workflow.get("description", "No description") or "No description").strip()
                description = description[: max(80, int(description_chars or 240))]
                lines.extend([
                    f"- workflow_id: {workflow_id}",
                    f"  task_types: {task_types}",
                    f"  triggers: {triggers}",
                    f"  stages: {stages}",
                    f"  description: {description}",
                ])
            else:
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
        if not selected:
            lines.append("- no metadata match; do not invent a workflow_id")
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
        workflow_id = WorkflowManager._normalize_workflow_id(decision.get("workflow_id"))
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


def parse_workflow_state(text: str) -> Dict[str, Any]:
    """Parse workflow state using the legacy handoff wire format."""
    return parse_workflow_handoff(text)


def strip_workflow_activation(workflow_state: Dict[str, Any]) -> Dict[str, Any]:
    """Return coarse workflow metadata without node-owned execution state.

    Legacy sessions may still contain activation fields. They remain parseable,
    but new Consultant-to-Planner handoffs must not carry scheduling decisions.
    """
    normalized = dict(workflow_state or {})
    normalized["active_batch_id"] = ""
    normalized["active_skills"] = []
    normalized["future_skills"] = []
    decision = dict(normalized.get("workflow_decision") or {})
    decision.pop("active_batch_id", None)
    decision.pop("current_batch", None)
    normalized["workflow_decision"] = decision
    stages: List[Dict[str, Any]] = []
    for raw_stage in normalized.get("workflow_stages") or []:
        if not isinstance(raw_stage, dict):
            continue
        stage = dict(raw_stage)
        stage.pop("active_now", None)
        stages.append(stage)
    normalized["workflow_stages"] = stages
    return normalized


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


def validate_workflow_state(
    workflow_state: Dict[str, Any],
    skill_manager,
    workflow_manager: Optional[WorkflowManager] = None,
    max_active_skills: int = 3,
) -> Dict[str, Any]:
    """Validate workflow state while preserving legacy validation behavior."""
    result = validate_workflow_handoff(
        workflow_state,
        skill_manager,
        workflow_manager,
        max_active_skills=max_active_skills,
    )
    if "handoff" in result:
        result["workflow_state"] = result["handoff"]
    return result


def repair_workflow_handoff(
    handoff: Dict[str, Any],
    skill_manager,
    workflow_manager: Optional[WorkflowManager] = None,
    max_active_skills: int = 3,
) -> Dict[str, Any]:
    """Repair deterministic workflow handoff omissions using workflow/stage metadata.

    This intentionally stays generic: a repair is allowed only when a selected
    active skill maps to exactly one workflow stage via either template
    default_skill or skill stage_tags. Ambiguous cases remain validation errors.
    """
    validation = validate_workflow_handoff(
        handoff,
        skill_manager,
        workflow_manager,
        max_active_skills=max_active_skills,
    )
    if not validation.get("errors"):
        return {"repaired": False, "notes": [], **validation}

    normalized = dict(validation.get("handoff") or handoff or {})
    workflow_id = str(normalized.get("workflow_id") or "").strip()
    active_skills = _normalize_skills(skill_manager, normalized.get("active_skills") or [])
    stages = [dict(stage or {}) for stage in normalized.get("workflow_stages") or [] if isinstance(stage, dict)]
    notes: List[str] = []

    template_stages: List[Dict[str, Any]] = []
    if workflow_id and workflow_manager:
        template = workflow_manager.get_workflow(workflow_id)
        template_stages = [dict(stage or {}) for stage in template.get("stages", []) if isinstance(stage, dict)]

    by_stage: Dict[str, Dict[str, Any]] = {}
    for stage in template_stages + stages:
        stage_name = str(stage.get("stage") or stage.get("stage_id") or "").strip()
        if not stage_name:
            continue
        current = by_stage.setdefault(stage_name, {})
        for key, value in stage.items():
            if key in {"stage", "stage_id"}:
                continue
            current_value = current.get(key)
            value_is_empty = value is None or value == "" or value == []
            current_has_value = current_value is not None and current_value != "" and current_value != []
            if value_is_empty and current_has_value:
                continue
            current[key] = value
        current["stage"] = stage_name

    if not active_skills:
        active_stage_skills = []
        for stage in stages:
            if stage.get("status") != "selected_skill":
                continue
            if stage.get("active_now") is not True and not (
                normalized.get("active_batch_id")
                and str(stage.get("batch_id") or "").strip() == str(normalized.get("active_batch_id") or "").strip()
            ):
                continue
            canonical = _normalize_skills(skill_manager, [str(stage.get("skill") or "")])
            if canonical:
                active_stage_skills.append(canonical[0])
        if active_stage_skills:
            active_skills = list(dict.fromkeys(active_stage_skills))
            normalized["active_skills"] = active_skills
            notes.append("Derived ACTIVE_SKILLS from selected active stages.")

    active_batch_id = str(normalized.get("active_batch_id") or "").strip()
    matched_batches: List[str] = []
    repaired_stage_names: List[str] = []

    for skill in active_skills:
        meta = skill_manager.get_skill_meta(skill) if skill_manager else {}
        stage_tags = skill_manager.metadata_list(meta, "stage_tags") if hasattr(skill_manager, "metadata_list") else []
        matches: List[Dict[str, Any]] = []
        for stage in by_stage.values():
            stage_name = str(stage.get("stage") or stage.get("stage_id") or "").strip()
            default_skill = str(stage.get("default_skill") or "").strip()
            default_canonical = _normalize_skills(skill_manager, [default_skill]) if default_skill else []
            if stage_name in stage_tags or (default_canonical and default_canonical[0] == skill):
                matches.append(stage)

        if active_batch_id:
            batch_matches = [
                stage for stage in matches
                if str(stage.get("batch_id") or "").strip() == active_batch_id
            ]
            if len(batch_matches) == 1:
                matches = batch_matches
        if len(matches) != 1:
            continue

        stage = dict(matches[0])
        stage_name = str(stage.get("stage") or stage.get("stage_id") or "").strip()
        if not stage_name:
            continue
        stage["stage"] = stage_name
        stage["status"] = "selected_skill"
        stage["skill"] = skill
        stage["active_now"] = True
        stage["batch_id"] = str(stage.get("batch_id") or active_batch_id or "1").strip()
        by_stage[stage_name] = stage
        matched_batches.append(stage["batch_id"])
        if stage_name not in repaired_stage_names:
            repaired_stage_names.append(stage_name)
        notes.append(f"Tied active skill {skill} to workflow stage {stage_name}.")

    for stage_name, stage in list(by_stage.items()):
        if str(stage.get("status") or "").strip() != "selected_skill":
            continue
        skill = _normalize_skills(skill_manager, [str(stage.get("skill") or "")])
        skill_id = skill[0] if skill else ""
        meta = skill_manager.get_skill_meta(skill_id) if skill_id and skill_manager else {}
        stage_tags = skill_manager.metadata_list(meta, "stage_tags") if hasattr(skill_manager, "metadata_list") else []
        default_skill = str(stage.get("default_skill") or "").strip()
        default_canonical = _normalize_skills(skill_manager, [default_skill]) if default_skill else []
        supports_stage = bool(
            skill_id
            and (
                stage_name in stage_tags
                or (default_canonical and default_canonical[0] == skill_id)
            )
        )
        if supports_stage:
            continue
        status_hint = str(stage.get("status_hint") or "").strip()
        if status_hint != "no_skill_needed":
            continue
        repaired = dict(stage)
        repaired["status"] = "no_skill_needed"
        repaired.pop("skill", None)
        repaired.pop("skill_role", None)
        if not str(repaired.get("reason") or "").strip():
            repaired["reason"] = "Workflow template marks this stage as a direct verification step; no separate skill is required."
        if not str(repaired.get("direct_method") or "").strip():
            repaired["direct_method"] = (
                "Verify the selected workflow skill's expected output files and record readiness in the final report/status table."
            )
        by_stage[stage_name] = repaired
        notes.append(f"Converted workflow stage {stage_name} to no_skill_needed from template status_hint.")

    if not active_batch_id and matched_batches and len(set(matched_batches)) == 1:
        active_batch_id = matched_batches[0]
        normalized["active_batch_id"] = active_batch_id
        decision = dict(normalized.get("workflow_decision") or {})
        if decision:
            decision["active_batch_id"] = active_batch_id
            normalized["workflow_decision"] = decision
        notes.append(f"Inferred active_batch_id={active_batch_id} from repaired active stage.")

    selected_stage_skills_by_batch: Dict[str, List[str]] = {}
    for stage in by_stage.values():
        if str(stage.get("status") or "").strip() != "selected_skill":
            continue
        skill = _normalize_skills(skill_manager, [str(stage.get("skill") or "")])
        if not skill:
            continue
        batch_id = str(stage.get("batch_id") or "").strip()
        selected_stage_skills_by_batch.setdefault(batch_id, [])
        if skill[0] not in selected_stage_skills_by_batch[batch_id]:
            selected_stage_skills_by_batch[batch_id].append(skill[0])

    if selected_stage_skills_by_batch:
        if not active_batch_id:
            active_batch_id = sorted(selected_stage_skills_by_batch, key=_batch_sort_key)[0]
            normalized["active_batch_id"] = active_batch_id
            decision = dict(normalized.get("workflow_decision") or {})
            decision["workflow_id"] = workflow_id or decision.get("workflow_id", "")
            decision["active_batch_id"] = active_batch_id
            normalized["workflow_decision"] = decision
            notes.append(f"Inferred active_batch_id={active_batch_id} from selected workflow stages.")

        batch_active_skills = selected_stage_skills_by_batch.get(active_batch_id, [])
        if batch_active_skills and (
            len(active_skills) > max_active_skills
            or set(active_skills) != set(batch_active_skills)
        ):
            future = list(normalized.get("future_skills") or [])
            for skills in selected_stage_skills_by_batch.values():
                for skill in skills:
                    if skill not in batch_active_skills and skill not in future:
                        future.append(skill)
            normalized["active_skills"] = batch_active_skills[:max_active_skills]
            normalized["future_skills"] = future
            required_all = list(normalized.get("required_skills_all") or [])
            for skill in [*batch_active_skills, *future]:
                if skill not in required_all:
                    required_all.append(skill)
            normalized["required_skills_all"] = required_all
            active_skills = list(normalized["active_skills"])
            notes.append(
                f"Pruned ACTIVE_SKILLS to active_batch_id={active_batch_id}; moved later batch skills to FUTURE_SKILLS."
            )

        for stage in by_stage.values():
            stage_batch = str(stage.get("batch_id") or "").strip()
            if stage_batch:
                stage["active_now"] = stage_batch == active_batch_id

    original_order = [
        str(stage.get("stage") or stage.get("stage_id") or "").strip()
        for stage in stages
        if str(stage.get("stage") or stage.get("stage_id") or "").strip()
    ]
    ordered_names = list(dict.fromkeys(original_order + repaired_stage_names))
    normalized["workflow_stages"] = [by_stage[name] for name in ordered_names if name in by_stage]

    repaired_validation = validate_workflow_handoff(
        normalized,
        skill_manager,
        workflow_manager,
        max_active_skills=max_active_skills,
    )
    return {
        "repaired": bool(notes and not repaired_validation.get("errors")),
        "notes": notes,
        **repaired_validation,
    }


def repair_workflow_state(
    workflow_state: Dict[str, Any],
    skill_manager,
    workflow_manager: Optional[WorkflowManager] = None,
    max_active_skills: int = 3,
) -> Dict[str, Any]:
    """Repair workflow state using the legacy handoff repair rules."""
    result = repair_workflow_handoff(
        workflow_state,
        skill_manager,
        workflow_manager,
        max_active_skills=max_active_skills,
    )
    if "handoff" in result:
        result["workflow_state"] = result["handoff"]
    return result


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


def _batch_sort_key(batch_id: str) -> tuple[int, str]:
    text = str(batch_id or "").strip()
    if text.isdigit():
        return (int(text), text)
    return (10**9, text)
