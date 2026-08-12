"""Generic plan-step and final completion validation gates.

This module deliberately avoids benchmark-specific expectations. It validates
whether the current plan step appears to have fulfilled its own contract before
BioMind advances, and whether the completed plan produced user-facing artifacts
before finalizing the task.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .plan_tracker import (
    CanonicalTaskStateError,
    STEP_RECOVERABLE_STATUSES,
    STEP_SUCCESS_STATUSES,
    STEP_TERMINAL_STATUSES,
    artifact_contract_match_score,
    normalize_artifact_contract,
    normalize_renderer_manifest,
    reconcile_step_artifacts,
    renderer_manifest_complete,
    step_artifact_contracts,
    validate_renderer_action_artifacts,
    ensure_provenance_state,
    approval_applicability_v2,
    canonical_task_spec_for_state,
)
from .task_spec import effective_output_contracts, effective_task_contract, evaluate_requirements


ARTIFACT_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_.+\-/]+"
    r"\.(?:tsv|csv|txt|md|html?|json|ya?ml|vcf(?:\.gz)?|bed(?:graph)?|rds|"
    r"png|pdf|svg|fa|fasta|fq|fastq|gfa|cool|mcool|bam|cram|bai|crai|"
    r"tbi|csi|bin|idx|dict|log|sh|R|r|py))\b"
)

PATH_TOKEN_RE = re.compile(r"(?<![\w.-])((?:/|[A-Za-z0-9_.+\-]+/)[^\s`'\"<>|]+)")

MAX_OUTPUT_ROOTS = 64
MAX_RELATIVE_OUTPUT_PATHS = 128
MAX_SEMANTIC_ARTIFACT_SCAN_DEPTH = 3
MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES = 512
MAX_DECLARATION_SCAN_FILES = 256
MAX_DECLARATION_FILE_BYTES = 2_000_000
ARTIFACT_MTIME_TOLERANCE_SECONDS = 5.0

ARTIFACT_STEP_TERMS = (
    "artifact",
    "create",
    "final",
    "generate",
    "output",
    "report",
    "result",
    "write",
    "产物",
    "报告",
    "输出",
    "生成",
    "结果",
)

DIRECTORY_PREP_TERMS = (
    "create output directory",
    "prepare output directory",
    "prepare write location",
    "directory exists",
    "mkdir",
    "创建输出目录",
    "准备输出目录",
)

NON_PROGRESS_PREFIXES = (
    "cd ",
    "du ",
    "echo ",
    "find ",
    "ls ",
    "mkdir ",
    "pwd",
    "realpath ",
    "test ",
    "tree ",
    "which ",
)

WRITE_HINTS = (
    ">",
    ">>",
    "cat >",
    "tee ",
    ".to_csv",
    ".to_excel",
    ".write",
    "write_text",
    "write.csv",
    "write.table",
    "write_tsv",
    "writeLines",
)

TEXT_STATUS_SUFFIXES = {".csv", ".json", ".md", ".txt", ".tsv", ".yaml", ".yml"}

COMPLETED_DECLARATION_TERMS = (
    "completed",
    "complete",
    "done",
    "finished",
    "success",
    "passed",
    "完成",
    "已完成",
    "成功",
)

GENERATED_DECLARATION_TERMS = (
    "generated",
    "created",
    "written",
    "saved",
    "produced",
    "exists",
    "生成",
    "创建",
    "写入",
    "保存",
    "产出",
)

NON_COMPLETED_DECLARATION_TERMS = (
    "blocked",
    "no_skill_needed",
    "not requested",
    "not provided",
    "missing",
    "failed",
    "skipped",
    "阻塞",
    "缺少",
    "未提供",
    "跳过",
    "失败",
)

ABSOLUTE_ROOT_ARTIFACT_DIRS = {
    "objects",
    "tables",
    "plots",
    "figures",
    "results",
    "reports",
}

KNOWN_ABSENT_RE = re.compile(
    r"KNOWN_ABSENT_FOR_THIS_TASK\s*:\s*([^\n（(]+)",
    re.IGNORECASE,
)

@dataclass
class GateResult:
    passed: bool
    message: str = ""
    feedback: str = ""
    missing_artifacts: List[str] = field(default_factory=list)
    checked_artifacts: List[str] = field(default_factory=list)
    classification: str = ""
    reconciliation: Dict[str, Any] = field(default_factory=dict)
    stale_artifacts: List[str] = field(default_factory=list)
    invalid_artifacts: List[str] = field(default_factory=list)
    attempt_ids: List[str] = field(default_factory=list)
    artifact_version_ids: List[str] = field(default_factory=list)
    validation_ids: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    plan_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "verdict": "PASS" if self.passed else "FAIL",
            "message": self.message,
            "feedback": self.feedback,
            "missing_artifacts": list(self.missing_artifacts),
            "checked_artifacts": list(self.checked_artifacts),
            "classification": self.classification or ("passed" if self.passed else "gate_failed"),
            "reconciliation": dict(self.reconciliation or {}),
            "stale_artifacts": list(self.stale_artifacts),
            "invalid_artifacts": list(self.invalid_artifacts),
            "attempt_ids": list(self.attempt_ids),
            "artifact_version_ids": list(self.artifact_version_ids),
            "validation_ids": list(self.validation_ids),
            "evidence_refs": list(self.evidence_refs),
            "plan_version": self.plan_version,
        }


@dataclass
class DependencyEvidence:
    satisfied: bool
    step_id: str
    stage_id: str = ""
    missing_dependencies: List[str] = field(default_factory=list)
    invalid_dependencies: List[str] = field(default_factory=list)
    stale_artifacts: List[str] = field(default_factory=list)
    invalid_artifacts: List[str] = field(default_factory=list)
    unknown_producers: List[str] = field(default_factory=list)
    missing_producer_artifacts: List[str] = field(default_factory=list)
    rollback_candidates: List[Dict[str, str]] = field(default_factory=list)
    stage_rewind_candidates: List[Dict[str, str]] = field(default_factory=list)
    plan_id: str = ""
    plan_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.__dict__,
            "rollback_candidates": [dict(item) for item in self.rollback_candidates],
            "stage_rewind_candidates": [dict(item) for item in self.stage_rewind_candidates],
        }


@dataclass
class StepCompletionEvidence:
    completion_satisfied: bool
    step_id: str
    missing_outputs: List[str] = field(default_factory=list)
    invalid_outputs: List[str] = field(default_factory=list)
    stale_outputs: List[str] = field(default_factory=list)
    failed_criteria: List[str] = field(default_factory=list)
    attempt_ids: List[str] = field(default_factory=list)
    artifact_version_ids: List[str] = field(default_factory=list)
    validation_ids: List[str] = field(default_factory=list)
    plan_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TaskCompletionEvidence:
    task_complete: bool
    unfinished_steps: List[str] = field(default_factory=list)
    invalidated_steps: List[str] = field(default_factory=list)
    waiting_upstream_steps: List[str] = field(default_factory=list)
    stale_artifacts: List[str] = field(default_factory=list)
    invalid_artifacts: List[str] = field(default_factory=list)
    plan_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _steps(plan_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [step for step in plan_state.get("steps", []) or [] if isinstance(step, dict)]


def current_running_step(plan_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cursor = str(plan_state.get("current_step_id") or "")
    if cursor:
        for step in _steps(plan_state):
            if (
                str(step.get("step_id") or "") == cursor
                and str(step.get("status") or "").strip().lower() in STEP_RECOVERABLE_STATUSES
            ):
                return step
    for step in _steps(plan_state):
        if str(step.get("status") or "").strip().lower() in STEP_RECOVERABLE_STATUSES:
            return step
    return None


def _step_title(step: Optional[Dict[str, Any]]) -> str:
    if not step:
        return ""
    return str(step.get("title") or "").strip()


def _step_contract_text(step: Optional[Dict[str, Any]]) -> str:
    """Project a structured step's goal and completion contract for gating."""
    if not step:
        return ""
    chunks = [_step_title(step), str(step.get("goal") or "")]
    for key in ("outputs", "output_contracts", "success_criteria"):
        value = step.get(key)
        if isinstance(value, list):
            chunks.extend(str(item) for item in value)
        elif value:
            chunks.append(str(value))
    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


def _last_execution_evidence(step: Dict[str, Any]) -> Tuple[str, str]:
    """Return (script_path, stdout_excerpt) from the latest execution evidence."""
    evidence = step.get("evidence")
    if not isinstance(evidence, list):
        return "", ""
    for item in reversed(evidence):
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "")
        if event not in {
            "execute_success",
            "execute_failed",
            "runtime_missing_after_execute",
            "execution_evidence_relation",
        }:
            continue
        detail = str(item.get("detail") or "")
        first, _, rest = detail.partition("\n")
        _, _, stdout = rest.partition("STDOUT:\n")
        return first.strip(), stdout
    return "", ""


def _read_script(script_path: str) -> str:
    if not script_path:
        return ""
    try:
        path = Path(script_path)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


_GENERIC_CRITERION_WORDS = {
    "all", "check", "checked", "complete", "completed", "correct", "done",
    "pass", "passed", "success", "successful", "validate", "validated",
    "verification", "verify",
}
_GENERIC_CRITERION_CJK = {
    "所有", "检查", "完成", "成功", "正确", "验证", "通过", "文件",
}


def _criterion_terms(text: str) -> List[str]:
    """Extract small language-agnostic anchors for evidence convergence."""
    source = str(text or "").lower()
    terms = {
        word
        for word in re.findall(r"[a-z0-9_.+-]{3,}", source)
        if word not in _GENERIC_CRITERION_WORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]{2,}", source):
        for index in range(len(run) - 1):
            token = run[index:index + 2]
            if token not in _GENERIC_CRITERION_CJK:
                terms.add(token)
    return sorted(terms)


def _criterion_supported(criterion: str, evidence_text: str) -> bool:
    normalized_criterion = re.sub(r"\W+", "", str(criterion or "").lower())
    normalized_evidence = re.sub(r"\W+", "", str(evidence_text or "").lower())
    if normalized_criterion and normalized_criterion in normalized_evidence:
        return True
    terms = _criterion_terms(criterion)
    if not terms:
        return False
    evidence_terms = set(_criterion_terms(evidence_text))
    matched = [term for term in terms if term in evidence_terms or term in normalized_evidence]
    required = 1 if len(terms) == 1 else 2
    return len(matched) >= required


def _successful_execution_record(record: Dict[str, Any]) -> bool:
    status = str(record.get("execution_status") or "").strip().lower()
    exit_code = record.get("exit_code", record.get("returncode"))
    return status == "succeeded" and exit_code in (None, "", 0, "0")


def _execution_record_text(record: Dict[str, Any]) -> str:
    return "\n".join(
        str(record.get(key) or "")
        for key in (
            "code", "generated_code", "raw_stdout", "stdout", "stdout_summary",
            "raw_stderr", "stderr", "stderr_summary",
        )
    )


def _relation_detail(record: Dict[str, Any], target_step_id: str) -> str:
    script_path = str(
        record.get("script_path")
        or record.get("code_path")
        or record.get("generated_script_path")
        or ""
    )
    stdout = str(
        record.get("raw_stdout")
        or record.get("stdout")
        or record.get("stdout_summary")
        or ""
    )
    relation = {
        "execution_id": str(record.get("execution_id") or record.get("action_id") or ""),
        "source_step_id": str(record.get("step_id") or ""),
        "target_step_id": str(target_step_id or ""),
    }
    return (
        f"{script_path}\nSTDOUT:\n{stdout[:5000]}\n"
        f"EVIDENCE_RELATION: {json.dumps(relation, ensure_ascii=False, sort_keys=True)}"
    )


def _strip_code_noise(code: str) -> List[str]:
    lines: List[str] = []
    for raw in str(code or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("set ") or line.startswith("export "):
            continue
        lines.append(line)
    return lines


def _is_metadata_or_prep_only(code: str) -> bool:
    lines = _strip_code_noise(code)
    if not lines:
        return True
    for line in lines:
        lowered = line.lower()
        if lowered.startswith("cooler ls "):
            continue
        if any(lowered.startswith(prefix) for prefix in NON_PROGRESS_PREFIXES):
            continue
        return False
    return True


def _has_write_intent(code: str) -> bool:
    lowered = str(code or "").lower()
    return any(hint.lower() in lowered for hint in WRITE_HINTS)


def _looks_like_artifact_step(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in ARTIFACT_STEP_TERMS)


def _looks_like_directory_prep_step(text: str) -> bool:
    lowered = str(text or "").lower()
    if not any(term in lowered for term in DIRECTORY_PREP_TERMS):
        return False
    hard_artifacts = _artifact_candidates(text)
    return not hard_artifacts


def _requires_balance_weight_check(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        ("balance" in lowered or "balanced" in lowered or "平衡" in lowered)
        and ("status" in lowered or "check" in lowered or "determine" in lowered or "验证" in lowered or "检查" in lowered)
        and not ("list" in lowered and "resolution" in lowered)
    )


def _has_balance_weight_evidence(code: str, stdout: str) -> bool:
    combined = f"{code}\n{stdout}".lower()
    return (
        ("weight" in combined or "bins/weight" in combined)
        and (
            "finite" in combined
            or "notnull" in combined
            or "non-null" in combined
            or "valid_weight" in combined
            or "has_weight" in combined
            or "balanced=" in combined
        )
    )


NEGATIVE_ARTIFACT_TERMS = (
    "must not exist",
    "must not be generated",
    "should not exist",
    "should not be generated",
    "do not generate",
    "do not create",
    "cannot be generated",
    "cannot be produced",
    "can't be generated",
    "can't be produced",
    "forbidden",
    "禁止",
    "不得生成",
    "不要生成",
    "不能生成",
    "无法生成",
    "不应存在",
    "不应该存在",
)


def _negative_artifact_context(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in NEGATIVE_ARTIFACT_TERMS)


def _drop_negative_artifact_sections(text: str) -> str:
    kept: List[str] = []
    skipping = False
    skip_level = 0
    for line in str(text or "").splitlines():
        heading = re.match(r"^\s*(#{2,6})\s+(?P<title>.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group("title")
            if _negative_artifact_context(title):
                skipping = True
                skip_level = level
                continue
            if skipping and level <= skip_level:
                skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _artifact_candidates(text: str) -> List[str]:
    seen: set[str] = set()
    artifacts: List[str] = []
    searchable = _drop_negative_artifact_sections(str(text or ""))
    for match in ARTIFACT_RE.finditer(searchable):
        artifact = match.group(1).strip("`'\".,;:()[]{}")
        if not artifact or artifact.startswith(("http://", "https://")):
            continue
        line_start = searchable.rfind("\n", 0, match.start()) + 1
        line_end = searchable.find("\n", match.end())
        if line_end == -1:
            line_end = len(searchable)
        line_context = searchable[line_start:line_end]
        nearby_context = searchable[max(0, match.start() - 100): match.end() + 100]
        if _negative_artifact_context(line_context) or _negative_artifact_context(nearby_context):
            continue
        # Input files should not become final-output requirements unless a
        # plan/output step explicitly asks to generate them.
        if artifact.endswith((".mcool", ".cool", ".fa", ".fasta", ".fq", ".fastq")):
            context = searchable[max(0, match.start() - 60): match.end() + 60].lower()
            if not any(term in context for term in ARTIFACT_STEP_TERMS):
                continue
        key = artifact.lower()
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(artifact)
    return artifacts


def _artifact_alternative_groups(text: str) -> List[List[str]]:
    """Return artifact groups where the plan promises one of several outputs.

    Planner often expresses conditional stages as "A or B" (or Chinese "A 或 B").
    Those alternatives must be validated as an OR condition; otherwise the gate
    keeps asking Generate to create intentionally skipped artifacts.
    """
    groups: List[List[str]] = []
    seen_groups: set[Tuple[str, ...]] = set()
    searchable = _drop_negative_artifact_sections(str(text or ""))

    def add_group(group: List[str]) -> None:
        cleaned: List[str] = []
        seen: set[str] = set()
        for artifact in group:
            key = artifact.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(artifact)
        if len(cleaned) < 2:
            return
        key = tuple(sorted(item.lower() for item in cleaned))
        if key in seen_groups:
            return
        seen_groups.add(key)
        groups.append(cleaned)

    for line in searchable.splitlines():
        allowed = {artifact.lower() for artifact in _artifact_candidates(line)}
        if len(allowed) < 2:
            continue
        matches: List[Tuple[str, int, int]] = []
        for match in ARTIFACT_RE.finditer(line):
            artifact = match.group(1).strip("`'\".,;:()[]{}")
            if artifact.lower() in allowed:
                matches.append((artifact, match.start(), match.end()))
        if len(matches) < 2:
            continue
        current = [matches[0][0]]
        for index in range(1, len(matches)):
            previous = matches[index - 1]
            artifact, start, end = matches[index]
            separator = line[previous[2]:start].lower()
            is_alternative = bool(
                re.search(r"\b(?:or|either|and/or)\b", separator)
                or re.search(r"或|或者|二选一|任选其一", separator)
                or re.search(r"(^|\s)/(?:\s|$)", separator)
            )
            if is_alternative:
                current.append(artifact)
            else:
                add_group(current)
                current = [artifact]
        add_group(current)
    return groups


def _missing_artifacts_considering_alternatives(
    artifacts: Iterable[str],
    alternative_groups: Iterable[Iterable[str]],
    output_roots: Iterable[Path],
    *,
    allow_semantic: bool = True,
    not_before: Optional[float] = None,
) -> Tuple[List[str], List[str]]:
    artifact_list = list(artifacts)
    artifact_keys = {artifact.lower() for artifact in artifact_list}
    handled: set[str] = set()
    missing: List[str] = []
    mention_required: List[str] = []
    roots = list(output_roots)

    def add_once(target: List[str], artifact: str) -> None:
        if artifact not in target:
            target.append(artifact)

    for raw_group in alternative_groups:
        group = [artifact for artifact in raw_group if artifact.lower() in artifact_keys]
        if len(group) < 2:
            continue
        for artifact in group:
            handled.add(artifact.lower())
        existing = [
            artifact for artifact in group
            if _artifact_exists_in_roots(
                roots, artifact, allow_semantic=allow_semantic, not_before=not_before
            )
        ]
        if existing:
            for artifact in existing:
                add_once(mention_required, artifact)
            continue
        for artifact in group:
            add_once(missing, artifact)
            add_once(mention_required, artifact)

    for artifact in artifact_list:
        if artifact.lower() in handled:
            continue
        add_once(mention_required, artifact)
        if not _artifact_exists_in_roots(
            roots, artifact, allow_semantic=allow_semantic, not_before=not_before
        ):
            add_once(missing, artifact)
    return missing, mention_required


def _known_absent_artifacts(current_plan: str) -> set[str]:
    absent: set[str] = set()
    for match in KNOWN_ABSENT_RE.finditer(str(current_plan or "")):
        token = match.group(1).strip("`'\".,;:()[]{} ")
        if token:
            absent.add(token.lower())
            absent.add(Path(token).name.lower())
    return absent


def _declared_output_dirs(current_plan: str) -> List[str]:
    dirs: List[str] = []
    seen: set[str] = set()
    for line in str(current_plan or "").splitlines():
        if not re.search(r"\b(output[\s_-]+dir(?:ectory)?|output_root|declared_output_dir|输出目录)\b", line, re.IGNORECASE):
            continue
        for match in PATH_TOKEN_RE.finditer(line):
            token = match.group(1).strip("`'\".,;:()[]{}")
            if not token or token in seen:
                continue
            seen.add(token)
            dirs.append(token)
    return dirs


def _is_under_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _artifact_allowed_for_final(artifact: str, current_plan: str, output_roots: Iterable[Path]) -> bool:
    text = str(artifact or "").strip()
    if not text:
        return False
    path = Path(text)
    absent = _known_absent_artifacts(current_plan)
    if text.lower() in absent or path.name.lower() in absent:
        return False
    if path.is_absolute():
        # Reject root-level fragments such as /objects/foo.rds that are not
        # inside any discovered output root. These are almost always markdown
        # shorthand, not user-writable deliverables.
        if len(path.parts) >= 2 and path.parts[1] in ABSOLUTE_ROOT_ARTIFACT_DIRS:
            return any(str(root) != "/" and _is_under_path(path, root) for root in output_roots)
        return any(str(root) != "/" and _is_under_path(path, root) for root in output_roots)

    parts = path.parts
    if not parts:
        return False
    if any(part in {"..", ".", ""} for part in parts):
        return False
    if parts[0] == "benchmarks" or (len(parts) >= 3 and parts[0] == "runs"):
        # Full benchmark paths in prompts usually describe inputs, siblings, or
        # other ambiguity branches. Final outputs should be relative to the
        # current declared output directory instead.
        declared_dirs = _declared_output_dirs(current_plan)
        if not any(text.startswith(prefix.rstrip("/") + "/") for prefix in declared_dirs):
            return False
    if "fixtures_simulated_truth" in parts or "expected_answers" in parts:
        return False
    return True


def _trusted_final_output_roots(data_path: str | Path, current_plan: str) -> List[Path]:
    base_root = Path(data_path)
    roots: List[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        root = _normalise_existing_dir(path, base_root)
        if not root or str(root) == "/":
            return
        key = str(root)
        if key in seen:
            return
        seen.add(key)
        roots.append(root)

    for token in _declared_output_dirs(current_plan):
        add(Path(token))
    add(base_root)
    return roots or [base_root]


def _is_task_output_root(path: Path) -> bool:
    parts = set(path.parts)
    return "task_outputs" in parts


def _merge_final_output_roots(*root_groups: Iterable[Path]) -> List[Path]:
    roots: List[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            root = path.resolve()
        except OSError:
            root = path
        if str(root) == "/" or _is_task_output_root(root):
            return
        key = str(root)
        if key in seen:
            return
        seen.add(key)
        roots.append(root)

    for group in root_groups:
        for path in group:
            add(path)
    return roots


def _has_final_status_marker(path: Path) -> bool:
    for name in ("stage_status.tsv", "report.md", "reproduce.sh"):
        try:
            candidate = path / name
            if candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _final_output_roots(data_path: str | Path, plan_state: Dict[str, Any], current_plan: str) -> List[Path]:
    base_root = Path(data_path)
    try:
        base_resolved = base_root.resolve()
    except OSError:
        base_resolved = base_root

    trusted_roots = _trusted_final_output_roots(base_root, current_plan)
    declared_roots = [
        root for root in trusted_roots
        if root != base_resolved and not _is_task_output_root(root)
    ]
    candidate_roots = _candidate_output_roots(base_root, plan_state, current_plan)
    accepted_candidates: List[Path] = []

    for root in candidate_roots:
        if _is_task_output_root(root):
            continue
        if _is_under_path(root, base_resolved) and (root == base_resolved or _has_final_status_marker(root)):
            accepted_candidates.append(root)
            continue
        if any(_is_under_path(root, declared) or _is_under_path(declared, root) for declared in declared_roots):
            accepted_candidates.append(root)

    roots = _merge_final_output_roots(trusted_roots, accepted_candidates)
    return roots or [base_root]


def _implicit_artifacts_from_output_text(text: str) -> List[str]:
    lowered = str(text or "").lower()
    artifacts: List[str] = []
    if "report" in lowered and not re.search(r"\breport\.(?:md|html?|pdf)\b", lowered):
        artifacts.append("report.md")
    if "reproduce" in lowered and "reproduce.sh" not in lowered:
        artifacts.append("reproduce.sh")
    return artifacts


def _line_declares_completed_output(line: str) -> bool:
    lowered = str(line or "").lower()
    if not lowered.strip():
        return False
    if any(term in lowered for term in NON_COMPLETED_DECLARATION_TERMS):
        return False
    has_completion = any(term in lowered for term in COMPLETED_DECLARATION_TERMS)
    has_generation = any(term in lowered for term in GENERATED_DECLARATION_TERMS)
    has_output_hint = bool(_artifact_candidates(line)) or "report" in lowered or "reproduce" in lowered
    return has_completion and (has_generation or has_output_hint)


def _read_small_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_DECLARATION_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _declared_completed_artifacts(output_roots: Iterable[Path]) -> List[str]:
    """Infer artifacts that status/report files explicitly claim are complete.

    This catches cases where a stage status table says "report/reproduce
    generated" even though the final files were never written. It is deliberately
    generic and only trusts completed/generated contexts, not blocked/no-skill rows.
    """
    declared: List[str] = []
    seen: set[str] = set()
    scanned = 0

    def add(artifact: str) -> None:
        cleaned = str(artifact or "").strip()
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        declared.append(cleaned)

    for output_root in output_roots:
        try:
            iterator = output_root.iterdir()
        except OSError:
            continue
        for path in iterator:
            if scanned >= MAX_DECLARATION_SCAN_FILES:
                return declared
            if "task_outputs" in path.parts or path.suffix.lower() not in TEXT_STATUS_SUFFIXES:
                continue
            scanned += 1
            text = _read_small_text(path)
            if not text:
                continue
            base_dir = path.parent
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not _line_declares_completed_output(line):
                    continue
                lowered = line.lower()
                for artifact in _artifact_candidates(line):
                    add(str((base_dir / artifact).resolve()) if not Path(artifact).is_absolute() else artifact)
                if "report" in lowered and not re.search(r"\breport\.(?:md|html?|pdf)\b", lowered):
                    add(str((base_dir / "report.md").resolve()))
                if "reproduce" in lowered and "reproduce.sh" not in lowered:
                    add(str((base_dir / "reproduce.sh").resolve()))
    return declared


def _plan_declares_blocked(plan_state: Dict[str, Any], current_plan: str) -> bool:
    chunks = [str(current_plan or "")]
    for step in _steps(plan_state):
        chunks.append(_step_title(step))
        chunks.append(str(step.get("status") or ""))
        evidence = step.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    chunks.append(str(item.get("event") or ""))
                    chunks.append(str(item.get("detail") or ""))
    combined = "\n".join(chunks).lower()
    return bool(re.search(r"\bblocked\b|blocked_dependency_missing|阻塞", combined))


def _unfinished_plan_steps(plan_state: Dict[str, Any]) -> List[str]:
    unfinished: List[str] = []
    terminal_statuses = set(STEP_TERMINAL_STATUSES)
    for step in _steps(plan_state):
        status = str(step.get("status") or "").strip().lower()
        if status in terminal_statuses:
            continue
        title = _step_title(step)
        unfinished.append(title or str(step.get("step_id") or "unnamed step"))
    return unfinished


def _successful_execution_evidence(plan_state: Dict[str, Any], data_path: str | Path, task_id: str) -> bool:
    for step in _steps(plan_state):
        evidence = step.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            if str(item.get("event") or "").strip() == "execute_success":
                return True

    task_output = Path(data_path) / "task_outputs" / str(task_id or "default")
    try:
        return any(path.is_file() and path.stat().st_size > 0 for path in task_output.glob("step_*.sh"))
    except OSError:
        return False


def _supervisor_acceptance_evidence(plan_state: Dict[str, Any]) -> bool:
    """Recognize controller-accepted validation, with legacy evidence fallback."""
    for step in _steps(plan_state):
        evidence = step.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            detail = str(item.get("detail") or "").upper()
            event = str(item.get("event") or "").strip().lower()
            if event == "recovery_decision" and detail.startswith("COMPLETE_STEP"):
                return True
            if event == "validation_evidence":
                status = str(item.get("status") or item.get("detail") or "").strip().upper()
                if status in {"VALID", "VALID_WITH_WARNINGS"}:
                    return True
            # Old checkpoints recorded the Supervisor's compatibility verdict
            # directly in step evidence. Keep this read-only migration path.
            if event in {"done", "supervisor_verdict"} and ("PASS" in detail or "WARN" in detail):
                return True
    return False


def _blocked_status_artifacts_present(output_roots: Iterable[Path]) -> bool:
    wanted = ("report.md", "stage_status.tsv", "reproduce.sh")
    found = {name: None for name in wanted}
    for output_root in output_roots:
        for name in wanted:
            if found[name] is not None:
                continue
            for candidate in (output_root / name, output_root / Path(name).name):
                try:
                    if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                        found[name] = candidate
                        break
                except OSError:
                    continue
    if not all(found.values()):
        return False
    try:
        status_text = "\n".join(
            Path(path).read_text(encoding="utf-8", errors="replace")
            for path in found.values()
            if path
        ).lower()
    except OSError:
        return False
    return bool(re.search(r"\bblocked\b|blocked_dependency_missing|阻塞", status_text))


def _normalise_existing_dir(path: Path, base_root: Path) -> Optional[Path]:
    candidates = [path] if path.is_absolute() else [base_root / path, Path.cwd() / path]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _artifact_exists_in_roots(
    output_roots: Iterable[Path],
    artifact: str,
    *,
    allow_semantic: bool = True,
    not_before: Optional[float] = None,
) -> bool:
    path = Path(artifact)
    candidates: List[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        for output_root in output_roots:
            candidates.append(output_root / artifact)
            candidates.append(output_root / Path(artifact).name)

    for candidate in candidates:
        try:
            stat = candidate.stat()
            if (
                candidate.exists()
                and candidate.is_file()
                and stat.st_size > 0
                and (
                    not_before is None
                    or stat.st_mtime + ARTIFACT_MTIME_TOLERANCE_SECONDS >= not_before
                )
            ):
                return True
        except OSError:
            continue

    if not path.is_absolute() and len(path.parts) == 1 and len(path.suffixes) >= 2:
        suffix_name = path.name
        scanned_entries = 0
        for output_root in output_roots:
            stack: List[Tuple[Path, int]] = [(output_root, 0)]
            while stack and scanned_entries < MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
                current, depth = stack.pop()
                try:
                    entries = list(current.iterdir())
                except OSError:
                    continue
                for candidate in entries:
                    if scanned_entries >= MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
                        return False
                    scanned_entries += 1
                    if "task_outputs" in candidate.parts:
                        continue
                    try:
                        if candidate.is_dir():
                            if depth < MAX_SEMANTIC_ARTIFACT_SCAN_DEPTH and candidate.name not in {".git", "__pycache__"}:
                                stack.append((candidate, depth + 1))
                            continue
                        if (
                            candidate.is_file()
                            and candidate.stat().st_size > 0
                            and (
                                not_before is None
                                or candidate.stat().st_mtime + ARTIFACT_MTIME_TOLERANCE_SECONDS >= not_before
                            )
                            and candidate.name.endswith(suffix_name)
                        ):
                            return True
                    except OSError:
                        continue

    if (
        allow_semantic
        and not path.is_absolute()
        and len(path.parts) == 1
        and _semantic_artifact_exists_in_roots(
            output_roots,
            artifact,
            not_before=not_before,
        )
    ):
        return True

    return False


def _artifact_exists(output_root: Path, artifact: str) -> bool:
    return _artifact_exists_in_roots([output_root], artifact)


def _bounded_manifest_paths(output_roots: Iterable[Path], discovery_text: str) -> List[Path]:
    """Locate renderer manifests only in roots already tied to this step."""
    candidates: List[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen or "task_outputs" in resolved.parts and resolved.name != "manifest.json":
            return
        try:
            if resolved.is_file() and resolved.name == "manifest.json" and resolved.stat().st_size <= 2_000_000:
                seen.add(key)
                candidates.append(resolved)
        except OSError:
            return

    for match in re.finditer(r'"outdir"\s*:\s*("(?:[^"\\]|\\.)*")', str(discovery_text or "")):
        try:
            add(Path(json.loads(match.group(1))) / "manifest.json")
        except (json.JSONDecodeError, TypeError):
            continue
    for root in list(output_roots)[:MAX_OUTPUT_ROOTS]:
        add(root / "manifest.json")
        scanned = 0
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if scanned >= 64:
                break
            scanned += 1
            try:
                if child.is_dir():
                    add(child / "manifest.json")
            except OSError:
                continue
    return candidates[:16]


def _read_renderer_manifest(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("manifest_type") == "renderer_artifact_contract" or isinstance(value.get("artifacts"), list):
        return normalize_renderer_manifest(value, path)
    if isinstance(value.get("scripts"), list) and value.get("outdir"):
        return normalize_renderer_manifest(value, path)
    return {}


def _plan_start_epoch(plan_state: Dict[str, Any]) -> Optional[float]:
    """Return the durable task/plan provenance boundary for artifact checks."""
    value = str(
        (plan_state.get("task_state") or {}).get("created_at")
        or plan_state.get("created_at")
        or ""
    ).strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _preview_manifest_reconciliation(
    step: Dict[str, Any],
    output_roots: Iterable[Path],
    discovery_text: str,
    *,
    task_id: str = "",
    not_before: Optional[float] = None,
) -> Dict[str, Any]:
    for manifest_path in _bounded_manifest_paths(output_roots, discovery_text):
        manifest = _read_renderer_manifest(manifest_path)
        if not manifest:
            continue
        manifest_task_id = str(manifest.get("task_id") or "")
        if manifest_task_id and task_id and manifest_task_id != task_id:
            continue
        generated_at = str(manifest.get("generated_at") or "").strip()
        if generated_at and not_before is not None:
            try:
                if (
                    datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
                    + ARTIFACT_MTIME_TOLERANCE_SECONDS
                    < not_before
                ):
                    continue
            except (TypeError, ValueError):
                continue
        candidate = deepcopy(step)
        events = reconcile_step_artifacts(candidate, manifest, manifest_path=manifest_path)
        if events:
            return {
                "step_id": str(step.get("step_id") or ""),
                "manifest_path": str(manifest_path),
                "manifest": manifest,
                "events": events,
            }
    return {}


def _contract_roots(contract: Dict[str, Any], output_roots: Iterable[Path]) -> List[Path]:
    roots = list(output_roots)
    base_dir = str(contract.get("base_dir") or "").strip()
    if base_dir:
        base = Path(base_dir)
        try:
            if base.exists() and base.is_dir() and base not in roots:
                roots.insert(0, base)
        except OSError:
            pass
    return roots


def _contract_exists(
    contract: Dict[str, Any],
    output_roots: Iterable[Path],
    *,
    not_before: Optional[float] = None,
) -> bool:
    path_text = str(contract.get("path") or "").strip()
    if not path_text:
        return not bool(contract.get("required", True))
    roots = _contract_roots(contract, output_roots)
    candidates = [Path(path_text)] if Path(path_text).is_absolute() else [root / path_text for root in roots]
    validator = str(contract.get("validator") or "nonempty_file")
    for candidate in candidates:
        try:
            stat = candidate.stat()
            if validator == "nonempty_directory":
                if not candidate.is_dir() or not any(candidate.iterdir()):
                    continue
            elif not candidate.is_file() or stat.st_size <= 0:
                continue
            if (
                not_before is not None
                and stat.st_mtime + ARTIFACT_MTIME_TOLERANCE_SECONDS < not_before
            ):
                continue
            if validator == "nonempty_executable_script" and not bool(candidate.stat().st_mode & 0o111):
                continue
            if validator == "nonempty_vcf_with_ann_header":
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    if not any(line.startswith("##INFO=<ID=ANN") for _, line in zip(range(5000), handle)):
                        continue
            return True
        except OSError:
            continue
    return False


def _validate_renderer_execution_state(plan_state: Dict[str, Any]) -> GateResult:
    """Require registered manifests to finish every enabled required action."""
    checked_artifacts: List[str] = []
    for step in _steps(plan_state):
        execution = step.get("renderer_execution")
        if not isinstance(execution, dict) or not str(execution.get("manifest_path") or "").strip():
            continue
        manifest_path = Path(str(execution["manifest_path"]))
        manifest = _read_renderer_manifest(manifest_path)
        if not manifest:
            return GateResult(
                False,
                message="Registered renderer manifest is missing or unreadable.",
                feedback=f"Restore or reconcile the registered manifest before completion: {manifest_path}",
                missing_artifacts=[str(manifest_path)],
                classification="renderer_manifest_unreadable",
            )
        fingerprint = str(execution.get("manifest_fingerprint") or "")
        if fingerprint and fingerprint != str(manifest.get("manifest_fingerprint") or ""):
            return GateResult(
                False,
                message="Registered renderer manifest fingerprint changed.",
                feedback="Reconcile manifest provenance before executing or completing this step.",
                checked_artifacts=[str(manifest_path)],
                classification="renderer_manifest_fingerprint_mismatch",
            )
        requirements = evaluate_requirements(manifest.get("requirements", []) or [])
        if requirements["blocked"] or requirements["needs_reconciliation"]:
            unresolved = (
                requirements["missing_required"]
                + requirements["missing_conditional"]
                + requirements["unknown"]
            )
            return GateResult(
                False,
                message="Renderer requirements are unresolved.",
                feedback="Resolve the declared requirement contract: " + ", ".join(unresolved),
                missing_artifacts=unresolved,
                classification=(
                    "missing_required_input" if requirements["blocked"]
                    else "contract_reconciliation_required"
                ),
            )
        if not renderer_manifest_complete(manifest, execution):
            completed = {str(item) for item in execution.get("completed_action_ids", []) or []}
            remaining = [
                str(action.get("id") or "")
                for action in manifest.get("actions", []) or []
                if action.get("required", True)
                and action.get("enabled", True)
                and str(action.get("id") or "") not in completed
            ]
            return GateResult(
                False,
                message="Renderer composite step still has required actions.",
                feedback="Continue the Manifest Runner with required action IDs: " + ", ".join(remaining),
                checked_artifacts=[str(manifest_path)],
                classification="renderer_actions_incomplete",
                reconciliation={"remaining_action_ids": remaining},
            )
        required_actions = [
            action
            for action in manifest.get("actions", []) or []
            if action.get("required", True) and action.get("enabled", True)
        ]
        evidence_by_action = {
            str(item.get("manifest_action_id") or ""): item
            for item in execution.get("action_evidence", []) or []
            if isinstance(item, dict) and str(item.get("manifest_action_id") or "")
        }
        missing_evidence = [
            str(action.get("id") or "")
            for action in required_actions
            if str(action.get("id") or "") not in evidence_by_action
        ]
        if missing_evidence:
            return GateResult(
                False,
                message="Renderer actions are marked complete without execution evidence.",
                feedback="Restore action evidence before completing: " + ", ".join(missing_evidence),
                classification="renderer_action_evidence_missing",
            )
        artifact_by_id = {
            str(item.get("artifact_id") or ""): item
            for item in manifest.get("artifacts", []) or []
            if isinstance(item, dict) and str(item.get("artifact_id") or "")
        }
        for action in required_actions:
            action_id = str(action.get("id") or "")
            recorded_validation = evidence_by_action[action_id].get("artifact_validation") or {}
            if not bool(recorded_validation.get("passed")):
                return GateResult(
                    False,
                    message=f"Renderer action {action_id} lacks passing artifact evidence.",
                    feedback="Revalidate the manifest action artifacts before step completion.",
                    classification="renderer_action_artifact_evidence_failed",
                )
            current_validation = validate_renderer_action_artifacts(manifest, action)
            if not current_validation.get("passed"):
                failed = [
                    str(item.get("path") or item.get("artifact_id") or "")
                    for item in current_validation.get("failed_artifacts", []) or []
                ]
                missing = [str(item) for item in current_validation.get("missing_contracts", []) or []]
                return GateResult(
                    False,
                    message=f"Renderer action {action_id} artifacts no longer satisfy the manifest.",
                    feedback="Repair or reconcile renderer artifacts: " + ", ".join([*missing, *failed]),
                    missing_artifacts=[*missing, *failed],
                    classification="renderer_action_artifact_revalidation_failed",
                )
            for artifact_id in action.get("expected_artifact_ids", []) or []:
                contract = artifact_by_id.get(str(artifact_id)) or {}
                path = str(contract.get("path") or "")
                if path:
                    candidate = Path(path)
                    if not candidate.is_absolute():
                        candidate = manifest_path.parent / candidate
                    checked_artifacts.append(str(candidate))
    return GateResult(True, checked_artifacts=sorted(set(checked_artifacts)))


def _bounded_contract_candidates(
    contracts: Iterable[Dict[str, Any]],
    output_roots: Iterable[Path],
) -> Dict[str, List[str]]:
    """Find possible replacements without treating filename similarity as proof."""
    candidates: Dict[str, List[str]] = {}
    scanned = 0
    roots = list(output_roots)[:MAX_OUTPUT_ROOTS]
    for root in roots:
        stack: List[Tuple[Path, int]] = [(root, 0)]
        while stack and scanned < MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
            current, depth = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for path in entries:
                if scanned >= MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
                    break
                scanned += 1
                if "task_outputs" in path.parts:
                    continue
                try:
                    if path.is_dir():
                        if depth < MAX_SEMANTIC_ARTIFACT_SCAN_DEPTH and path.name not in {".git", "__pycache__"}:
                            stack.append((path, depth + 1))
                        continue
                    if not path.is_file() or path.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                observed = normalize_artifact_contract(
                    {"path": path.name, "artifact_type": "script" if path.suffix.lower() in {".sh", ".py", ".r"} else "file"},
                    source="bounded_probe",
                )
                for contract in contracts:
                    if path.name == Path(str(contract.get("path") or "")).name:
                        continue
                    if artifact_contract_match_score(contract, observed) <= 0:
                        continue
                    artifact_id = str(contract.get("artifact_id") or contract.get("path") or "artifact")
                    candidates.setdefault(artifact_id, []).append(str(path.resolve()))
    return {key: sorted(set(paths)) for key, paths in candidates.items() if paths}


def _validate_degraded_manifest_disclosures(output_roots: Iterable[Path]) -> GateResult:
    roots = list(output_roots)
    for manifest_path in _bounded_manifest_paths(roots, ""):
        manifest = _read_renderer_manifest(manifest_path)
        requirement_result = evaluate_requirements(manifest.get("requirements", []) or [])
        if requirement_result["blocked"]:
            missing_requirements = requirement_result["missing_required"] + requirement_result["missing_conditional"]
            return GateResult(
                False,
                message="Renderer manifest has unsatisfied required requirements.",
                feedback="Final completion gate found missing required contract items: " + ", ".join(missing_requirements),
                missing_artifacts=missing_requirements,
                classification="missing_required_input",
            )
        if requirement_result["needs_reconciliation"]:
            return GateResult(
                False,
                message="Renderer requirement contract remains unresolved.",
                feedback="Resolve requirement levels before final completion: " + ", ".join(requirement_result["unknown"]),
                classification="contract_reconciliation_required",
            )
        validation = manifest.get("validation") if isinstance(manifest, dict) else {}
        if not isinstance(validation, dict) or str(validation.get("validation_mode") or "strict") == "strict":
            continue
        required = [str(item) for item in validation.get("required_disclosures") or [] if str(item).strip()]
        missing = [item for item in required if not _artifact_exists_in_roots([manifest_path.parent, *roots], item)]
        if missing:
            return GateResult(
                False,
                message="Degraded validation mode is missing required disclosure artifacts.",
                feedback=(
                    "Final completion gate failed: a renderer manifest records degraded scientific validation, "
                    "but these required disclosure artifacts are missing or empty: " + ", ".join(missing)
                ),
                missing_artifacts=missing,
                checked_artifacts=required,
                classification="missing_degraded_validation_disclosure",
            )
        combined = ""
        for name in required:
            path = manifest_path.parent / name
            try:
                if path.is_file() and path.stat().st_size <= MAX_DECLARATION_FILE_BYTES:
                    combined += "\n" + path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        markers = [str(item) for item in validation.get("reproduction_markers") or [] if str(item).strip()]
        skipped = [str(item).lower() for item in validation.get("skipped_checks") or [] if str(item).strip()]
        lower = combined.lower()
        if not all(marker in combined for marker in markers) or not all(item in lower for item in skipped):
            return GateResult(
                False,
                message="Degraded validation mode was not fully disclosed in final artifacts.",
                feedback=(
                    "Final completion gate failed: report/status/reproduction artifacts do not preserve the renderer's "
                    "degraded validation mode, skipped checks, and reproduction parameters."
                ),
                checked_artifacts=required,
                classification="incomplete_degraded_validation_disclosure",
            )
    return GateResult(True)


def _latest_command_mentions_artifacts(code: str, stdout: str, artifacts: Iterable[str]) -> bool:
    text = f"{code}\n{stdout}".lower()
    for artifact in artifacts:
        artifact_text = str(artifact or "").strip().lower()
        if not artifact_text:
            continue
        artifact_name = Path(artifact_text).name
        if artifact_text in text or artifact_name in text:
            return True
    return False


SEMANTIC_FILENAME_STOPWORDS = {
    "artifact",
    "artifacts",
    "file",
    "files",
    "filtered",
    "final",
    "object",
    "objects",
    "output",
    "outputs",
    "result",
    "results",
}


def _semantic_filename_tokens(path: Path) -> set[str]:
    stem = path.name
    for suffix in path.suffixes:
        if stem.lower().endswith(suffix.lower()):
            stem = stem[: -len(suffix)]
    tokens = {
        token.lower()
        for token in re.split(r"[^A-Za-z0-9]+", stem)
        if token and token.lower() not in SEMANTIC_FILENAME_STOPWORDS
    }
    return tokens


def _semantic_artifact_exists_in_roots(
    output_roots: Iterable[Path],
    artifact: str,
    *,
    not_before: Optional[float] = None,
) -> bool:
    requested = Path(artifact)
    requested_suffixes = tuple(suffix.lower() for suffix in requested.suffixes)
    requested_tokens = _semantic_filename_tokens(requested)
    if not requested_suffixes or len(requested_tokens) < 2:
        return False

    scanned_entries = 0
    for output_root in output_roots:
        stack: List[Tuple[Path, int]] = [(output_root, 0)]
        while stack and scanned_entries < MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
            current, depth = stack.pop()
            try:
                if not current.exists() or not current.is_dir():
                    continue
                entries = list(current.iterdir())
            except OSError:
                continue

            for candidate in entries:
                if scanned_entries >= MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES:
                    return False
                scanned_entries += 1
                if "task_outputs" in candidate.parts:
                    continue
                try:
                    if candidate.is_dir():
                        if depth < MAX_SEMANTIC_ARTIFACT_SCAN_DEPTH and candidate.name not in {".git", "__pycache__"}:
                            stack.append((candidate, depth + 1))
                        continue
                    if (
                        not candidate.is_file()
                        or candidate.stat().st_size <= 0
                        or (
                            not_before is not None
                            and candidate.stat().st_mtime + ARTIFACT_MTIME_TOLERANCE_SECONDS < not_before
                        )
                    ):
                        continue
                except OSError:
                    continue

                if tuple(suffix.lower() for suffix in candidate.suffixes) != requested_suffixes:
                    continue
                candidate_tokens = _semantic_filename_tokens(candidate)
                if requested_tokens == candidate_tokens:
                    return True
    return False


def _non_task_output_files(output_root: Path) -> List[Path]:
    files: List[Path] = []
    try:
        for path in output_root.rglob("*"):
            if "task_outputs" in path.parts:
                continue
            if path.is_file() and path.stat().st_size > 0:
                files.append(path)
    except OSError:
        return []
    return files


def _expected_artifacts_from_final_plan(plan_state: Dict[str, Any], current_plan: str) -> List[str]:
    snippets: List[str] = []
    canonical_artifacts: List[str] = []
    superseded_artifacts: set[str] = set()
    task_spec = canonical_task_spec_for_state(plan_state)
    if task_spec:
        for output in effective_task_contract(task_spec).get("outputs", []) or []:
            if not isinstance(output, dict) or not bool(output.get("required", True)):
                continue
            if str(output.get("role") or "") == "output_dir":
                continue
            artifact = str(output.get("canonical_path") or output.get("path") or output.get("filename") or "").strip()
            if artifact:
                canonical_artifacts.append(artifact)
    for step in _steps(plan_state):
        title = _step_title(step)
        contracts = step_artifact_contracts(step)
        renderer_contracts = [
            contract for contract in contracts
            if str(contract.get("source") or "") == "renderer_manifest"
        ]
        for contract in renderer_contracts:
            path = str(contract.get("path") or "").strip()
            if path and bool(contract.get("required", True)):
                canonical_artifacts.append(path)
            for item in contract.get("history", []) or []:
                if isinstance(item, dict) and str(item.get("path") or "").strip():
                    superseded_artifacts.add(str(item.get("path")).strip().lower())
            declared = str(contract.get("declared_path") or "").strip()
            if declared and declared != path:
                superseded_artifacts.add(declared.lower())
        if _looks_like_artifact_step(title):
            snippets.append(title)

    # V2 completion is contract driven.  Human plan prose, compatibility
    # aliases and status/report text are not authoritative output declarations.
    # Keep the text parser only as an explicit legacy-session read adapter.
    if _uses_canonical_completion_contract(plan_state):
        artifacts: List[str] = []
        seen: set[str] = set()
        superseded_names = {Path(item).name.lower() for item in superseded_artifacts}
        for artifact in canonical_artifacts:
            key = artifact.lower()
            if key in superseded_artifacts or Path(artifact).name.lower() in superseded_names:
                continue
            if key not in seen:
                seen.add(key)
                artifacts.append(artifact)
        return artifacts

    text = str(current_plan or "")
    for key in ("expected_artifacts", "final_artifacts", "declared_artifacts"):
        values = plan_state.get(key)
        if isinstance(values, list):
            snippets.append("\n".join(str(value) for value in values))
    for heading in (
        "final outputs",
        "outputs",
        "output",
        "expected outputs",
        "final outputs to verify",
        "最终输出",
        "预期产物",
        "输出",
    ):
        pattern = re.compile(
            rf"{re.escape(heading)}\s*:?(.*?)(?=\n\s*##|\n\s*Step\s+\d+|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(text):
            snippets.append(match.group(1))

    artifacts: List[str] = []
    seen: set[str] = set()
    superseded_names = {Path(item).name.lower() for item in superseded_artifacts}
    for artifact in canonical_artifacts:
        key = artifact.lower()
        if key not in seen:
            seen.add(key)
            artifacts.append(artifact)
    for snippet in snippets:
        for artifact in _artifact_candidates(snippet):
            key = artifact.lower()
            if key in superseded_artifacts or Path(artifact).name.lower() in superseded_names:
                continue
            if key not in seen:
                seen.add(key)
                artifacts.append(artifact)
        for artifact in _implicit_artifacts_from_output_text(snippet):
            key = artifact.lower()
            if key not in seen:
                seen.add(key)
                artifacts.append(artifact)
    return artifacts


def _canonical_output_alternative_groups(plan_state: Mapping[str, Any]) -> List[List[str]]:
    """Return one-of output groups declared by canonical TaskSpec contracts.

    V2 completion must not reinterpret the natural-language plan to decide
    whether an output is optional.  The grouping is therefore carried by the
    output bindings themselves via ``alternative_group``.
    """
    task_spec = canonical_task_spec_for_state(plan_state)
    grouped: Dict[str, List[str]] = {}
    for output in effective_output_contracts(task_spec):
        if not isinstance(output, Mapping) or not bool(output.get("required", True)):
            continue
        group = str(output.get("alternative_group") or "").strip()
        artifact = str(
            output.get("canonical_path")
            or output.get("required_basename")
            or ""
        ).strip()
        if group and artifact:
            grouped.setdefault(group, []).append(artifact)
    return [members for members in grouped.values() if len(members) >= 2]


def _uses_canonical_completion_contract(plan_state: Mapping[str, Any]) -> bool:
    """Distinguish live V2 state from an explicitly migrated legacy session."""
    compatibility = plan_state.get("compatibility")
    source_version = (
        compatibility.get("source_schema_version")
        if isinstance(compatibility, Mapping)
        else plan_state.get("schema_version")
    )
    try:
        return int(source_version or 1) >= 8
    except (TypeError, ValueError):
        return False


def _text_for_output_root_discovery(plan_state: Dict[str, Any], current_plan: str) -> str:
    is_v2 = _uses_canonical_completion_contract(plan_state)
    chunks = [] if is_v2 else [str(current_plan or "")]
    task_spec = canonical_task_spec_for_state(plan_state)
    if task_spec:
        for output in effective_task_contract(task_spec).get("outputs", []) or []:
            if isinstance(output, dict) and str(output.get("role") or "") == "output_dir":
                path = str(output.get("canonical_path") or output.get("path") or "").strip()
                if path:
                    chunks.append(f"declared_output_dir: {path}")
    if not is_v2:
        for key in ("output_dir", "output_root", "declared_output_dir", "output_roots", "output_paths"):
            value = plan_state.get(key)
            if isinstance(value, (list, tuple)):
                chunks.append(f"{key}: " + " ".join(str(item) for item in value))
            elif value:
                chunks.append(f"{key}: {value}")
    for step in _steps(plan_state):
        for contract in step_artifact_contracts(step):
            path = str(contract.get("path") or "").strip()
            if path:
                chunks.append(f"contract_artifact: {path}")
        if not is_v2:
            chunks.append(_step_title(step))
            evidence = step.get("evidence")
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict):
                        chunks.append(str(item.get("detail") or ""))
    return "\n".join(chunks)


def _candidate_output_roots_from_text(data_path: str | Path, text: str) -> List[Path]:
    base_root = Path(data_path)
    roots: List[Path] = []
    seen: set[str] = set()
    tokens: List[str] = []

    def add(path: Path) -> None:
        existing = _normalise_existing_dir(path, base_root)
        if not existing:
            return
        key = str(existing)
        if key in seen:
            return
        if len(roots) >= MAX_OUTPUT_ROOTS:
            return
        seen.add(key)
        roots.append(existing)

    add(base_root)
    for match in PATH_TOKEN_RE.finditer(str(text or "")):
        token = match.group(1).strip("`'\".,;:()[]{}")
        if not token:
            continue
        tokens.append(token)
        path = Path(token)
        if not path.is_absolute():
            continue
        if path.suffix:
            for parent in (path.parent, path.parent.parent):
                if str(parent) not in {"", "."}:
                    add(parent)
        else:
            add(path)

    # Resolve relative output paths once against explicitly discovered roots.
    # Do not feed newly derived roots back into the expansion loop: logs and
    # scripts often contain many relative paths, and recursively combining them
    # can grow exponentially before the supervisor can route to the next node.
    explicit_roots = list(roots)
    expanded = 0
    for token in tokens:
        path = Path(token)
        if path.is_absolute():
            continue
        if str(path).startswith(("-", "$")):
            continue
        for root in explicit_roots:
            if expanded >= MAX_RELATIVE_OUTPUT_PATHS:
                return roots
            expanded += 1
            candidate = root / path
            if path.suffix:
                for parent in (candidate.parent, candidate.parent.parent):
                    if str(parent) not in {"", "."}:
                        add(parent)
            else:
                add(candidate)
    return roots


def _candidate_output_roots(data_path: str | Path, plan_state: Dict[str, Any], current_plan: str) -> List[Path]:
    return _candidate_output_roots_from_text(
        data_path,
        _text_for_output_root_discovery(plan_state, current_plan),
    )


BIOMIND_CODE_BENCHMARK_RE = re.compile(
    r"(?P<repo>/[^\s`'\"<>|;&]*?/Biomind_v1)/benchmarks(?P<rest>/[^\s`'\"<>|;&]*)?"
)

FIND_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)find\s+(?P<root>'[^']+'|\"[^\"]+\"|[^\s;&|]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _is_broad_discovery_root(raw_root: str) -> bool:
    """Return whether a find root is broad enough to require an explicit bound."""
    root = str(raw_root or "").strip().strip("'\"")
    if root in {"/", "~", "$HOME", "${HOME}"}:
        return True
    if not root.startswith("/"):
        return False
    # /home/user and /public/home/user span a whole account. Deeper project or
    # source directories remain valid places for targeted scans.
    return len(Path(root).parts) <= 4


def _detect_unbounded_discovery_scan(code: str) -> str:
    """Reject broad recursive scans before they consume a cluster session."""
    text = str(code or "")
    for match in FIND_COMMAND_RE.finditer(text):
        root = match.group("root")
        command_tail = text[match.start() :].splitlines()[0]
        if not _is_broad_discovery_root(root) or "-maxdepth" in command_tail or "-quit" in command_tail:
            continue
        return (
            "Code boundary gate failed: this command recursively scans the broad path "
            f"`{root}` without a depth or early-exit bound. Restrict discovery to a verified project, "
            "environment, or source-tools directory and add `-maxdepth`; do not scan a whole home or root "
            "tree while looking for a runtime file."
        )
    return ""


def _detect_wrong_biomind_benchmark_root(code: str, task_context: str) -> str:
    """Catch benchmark paths accidentally rooted inside the BioMind code repo.

    The benchmark data root is commonly a sibling of ``Biomind_v1``. This guard
    stays structural: it only fires when the command references
    ``.../Biomind_v1/benchmarks/...`` and either the sibling benchmark root is
    visible on disk or the task context explicitly points there.
    """
    text = str(code or "")
    context = str(task_context or "")
    for match in BIOMIND_CODE_BENCHMARK_RE.finditer(text):
        repo_root = Path(match.group("repo"))
        sibling_root = repo_root.parent / "benchmarks"
        expected = str(sibling_root)
        context_points_to_sibling = expected in context
        context_marks_parent_as_project = str(repo_root.parent) in context and "benchmark" in context.lower()
        if sibling_root.exists() or context_points_to_sibling or context_marks_parent_as_project:
            bad_path = match.group(0)
            return (
                "Code boundary gate failed: the command resolves benchmark data under the BioMind code "
                f"repository path `{bad_path}`. Benchmark inputs should be resolved from the sibling "
                f"benchmark root `{expected}` when that root is available or declared in the task context. "
                "Regenerate the command with the verified benchmark root; do not execute commands using "
                "`.../Biomind_v1/benchmarks/...` as a data path."
            )
    return ""


class CompletionGate:
    """Stateless validators for plan-step and final task completion."""

    @staticmethod
    def validate_dependencies(
        plan_state: Dict[str, Any],
        step_id: str,
        stage_id: str = "",
    ) -> DependencyEvidence:
        """Return read-only dependency evidence for one Plan step or manifest stage."""
        provenance = ensure_provenance_state(plan_state)
        step = next(
            (item for item in _steps(plan_state) if str(item.get("step_id") or "") == str(step_id)),
            None,
        )
        if not step:
            return DependencyEvidence(
                False, str(step_id), str(stage_id), missing_dependencies=[str(step_id)],
                plan_id=str(plan_state.get("plan_id") or ""),
                plan_version=str(plan_state.get("plan_version") or ""),
            )
        step_by_id = {str(item.get("step_id") or ""): item for item in _steps(plan_state)}
        missing_dependencies: List[str] = []
        invalid_dependencies: List[str] = []
        for dependency in step.get("depends_on", step.get("dependencies", [])) or []:
            dependency_id = str(dependency)
            upstream = step_by_id.get(dependency_id)
            if not upstream:
                missing_dependencies.append(dependency_id)
            elif str(upstream.get("status") or "").lower() not in STEP_SUCCESS_STATUSES:
                invalid_dependencies.append(dependency_id)

        stale_artifacts: List[str] = []
        invalid_artifacts: List[str] = []
        unknown_producers: List[str] = []
        missing_producers: List[str] = []
        rollback_candidates: List[Dict[str, str]] = []
        for artifact_id in step.get("input_artifact_ids", []) or []:
            artifact_id = str(artifact_id)
            head = provenance.get("artifact_heads", {}).get(artifact_id)
            artifact = provenance.get("artifacts", {}).get(head, {})
            if not artifact:
                missing_producers.append(artifact_id)
                continue
            status = str(artifact.get("status") or "expected").lower()
            if status == "stale":
                stale_artifacts.append(artifact_id)
            elif status in {"invalid", "missing", "expected"}:
                invalid_artifacts.append(artifact_id)
            producer_step = str(artifact.get("producer_step_id") or "")
            producer_attempt = str(artifact.get("producer_attempt_id") or "")
            if not producer_step and not producer_attempt:
                unknown_producers.append(artifact_id)
            attempt = provenance.get("attempts", {}).get(producer_attempt, {}) if producer_attempt else {}
            if producer_attempt and str(attempt.get("status") or "").lower() not in {
                "succeeded", "passed", "valid", "completed",
            }:
                invalid_artifacts.append(artifact_id)
            if status in {"stale", "invalid"} and producer_step and producer_step != str(step_id):
                rollback_candidates.append({
                    "artifact_id": artifact_id,
                    "producer_step_id": producer_step,
                    "producer_attempt_id": producer_attempt,
                })

        stage_rewind_candidates: List[Dict[str, str]] = []
        if stage_id:
            stage = provenance.get("stages", {}).get(str(stage_id), {})
            for dependency_id in stage.get("depends_on_stage_ids", []) or []:
                dependency = provenance.get("stages", {}).get(str(dependency_id), {})
                if str(dependency.get("status") or "").lower() not in {"passed", "done", "valid"}:
                    invalid_dependencies.append(str(dependency_id))
                    stage_rewind_candidates.append({
                        "manifest_id": str(stage.get("manifest_id") or ""),
                        "failed_stage_id": str(stage_id),
                        "rewind_to_stage_id": str(dependency_id),
                    })
        return DependencyEvidence(
            satisfied=not any((missing_dependencies, invalid_dependencies, stale_artifacts, invalid_artifacts, unknown_producers, missing_producers)),
            step_id=str(step_id),
            stage_id=str(stage_id),
            missing_dependencies=list(dict.fromkeys(missing_dependencies)),
            invalid_dependencies=list(dict.fromkeys(invalid_dependencies)),
            stale_artifacts=list(dict.fromkeys(stale_artifacts)),
            invalid_artifacts=list(dict.fromkeys(invalid_artifacts)),
            unknown_producers=list(dict.fromkeys(unknown_producers)),
            missing_producer_artifacts=list(dict.fromkeys(missing_producers)),
            rollback_candidates=rollback_candidates,
            stage_rewind_candidates=stage_rewind_candidates,
            plan_id=str(plan_state.get("plan_id") or ""),
            plan_version=str(plan_state.get("plan_version") or ""),
        )

    @staticmethod
    def validate_step_evidence(plan_state: Dict[str, Any], step_id: str) -> StepCompletionEvidence:
        provenance = ensure_provenance_state(plan_state)
        step = next(
            (item for item in _steps(plan_state) if str(item.get("step_id") or "") == str(step_id)),
            None,
        )
        if not step:
            return StepCompletionEvidence(False, str(step_id), missing_outputs=[str(step_id)])
        missing: List[str] = []
        invalid: List[str] = []
        stale: List[str] = []
        attempts: List[str] = []
        versions: List[str] = []
        validations: List[str] = []
        for artifact_id in step.get("output_artifact_ids", []) or []:
            artifact_id = str(artifact_id)
            version_id = str(provenance.get("artifact_heads", {}).get(artifact_id) or "")
            artifact = provenance.get("artifacts", {}).get(version_id, {})
            if not artifact:
                missing.append(artifact_id)
                continue
            status = str(artifact.get("status") or "expected").lower()
            if status == "stale":
                stale.append(artifact_id)
            elif status != "valid":
                invalid.append(artifact_id)
            if str(artifact.get("producer_attempt_id") or ""):
                attempts.append(str(artifact["producer_attempt_id"]))
            if version_id:
                versions.append(version_id)
            if str(artifact.get("validation_id") or ""):
                validations.append(str(artifact["validation_id"]))
        renderer = dict(step.get("renderer_execution") or {})
        failed_criteria: List[str] = []
        manifest_id = str(renderer.get("manifest_id") or "")
        if manifest_id:
            stage_ids = list(provenance.get("manifests", {}).get(manifest_id, {}).get("stage_ids") or [])
            incomplete = [
                str(item) for item in stage_ids
                if str(provenance.get("stages", {}).get(str(item), {}).get("status") or "").lower()
                not in {"passed", "done", "valid", "skipped"}
            ]
            if incomplete:
                failed_criteria.append("incomplete_manifest_stages:" + ",".join(incomplete))
        return StepCompletionEvidence(
            completion_satisfied=not any((missing, invalid, stale, failed_criteria)),
            step_id=str(step_id),
            missing_outputs=missing,
            invalid_outputs=invalid,
            stale_outputs=stale,
            failed_criteria=failed_criteria,
            attempt_ids=list(dict.fromkeys(attempts)),
            artifact_version_ids=list(dict.fromkeys(versions)),
            validation_ids=list(dict.fromkeys(validations)),
            plan_version=str(plan_state.get("plan_version") or ""),
        )

    @staticmethod
    def validate_task_evidence(plan_state: Dict[str, Any]) -> TaskCompletionEvidence:
        provenance = ensure_provenance_state(plan_state)
        unfinished: List[str] = []
        invalidated: List[str] = []
        waiting: List[str] = []
        for step in _steps(plan_state):
            step_id = str(step.get("step_id") or "")
            status = str(step.get("status") or "").lower()
            if status == "invalidated":
                invalidated.append(step_id)
            if status == "waiting_upstream":
                waiting.append(step_id)
            if status not in STEP_SUCCESS_STATUSES:
                unfinished.append(step_id)
        stale = []
        invalid = []
        for artifact_id, version_id in provenance.get("artifact_heads", {}).items():
            status = str(provenance.get("artifacts", {}).get(version_id, {}).get("status") or "").lower()
            if status == "stale":
                stale.append(str(artifact_id))
            elif status == "invalid":
                invalid.append(str(artifact_id))
        return TaskCompletionEvidence(
            task_complete=not any((unfinished, stale, invalid)),
            unfinished_steps=unfinished,
            invalidated_steps=invalidated,
            waiting_upstream_steps=waiting,
            stale_artifacts=stale,
            invalid_artifacts=invalid,
            plan_version=str(plan_state.get("plan_version") or ""),
        )

    @staticmethod
    def validate_code_boundary(code: str, task_context: str) -> GateResult:
        wrong_benchmark_root = _detect_wrong_biomind_benchmark_root(code, task_context)
        if wrong_benchmark_root:
            return GateResult(
                passed=False,
                message="Command uses a benchmark path under the BioMind code repository.",
                feedback=wrong_benchmark_root,
            )
        unbounded_discovery = _detect_unbounded_discovery_scan(code)
        if unbounded_discovery:
            return GateResult(
                passed=False,
                message="Command performs an unbounded broad file-system scan.",
                feedback=unbounded_discovery,
            )
        return GateResult(True)

    @staticmethod
    def validate_step_completion_candidate(
        data_path: str | Path,
        task_id: str,
        plan_state: Dict[str, Any],
        candidate: Dict[str, Any],
        execution_records: Iterable[Dict[str, Any]],
    ) -> GateResult:
        """Validate a step-level completion claim against persisted execution evidence."""
        step = current_running_step(plan_state)
        if not step:
            return GateResult(
                False,
                message="No running step is available for STEP_COMPLETION_CANDIDATE.",
                feedback="Return an action for the controller's current running step.",
                classification="step_candidate_without_current_step",
            )

        step_id = str(step.get("step_id") or "")
        candidate_step_id = str(candidate.get("step_id") or "")
        if not candidate_step_id or candidate_step_id != step_id:
            return GateResult(
                False,
                message=f"Step completion candidate targets {candidate_step_id or '<missing>'}, current step is {step_id}.",
                feedback=f"Return STEP_COMPLETION_CANDIDATE for current step {step_id} only.",
                classification="step_candidate_stale",
            )

        evidence_ids = [
            str(item).strip()
            for item in candidate.get("evidence_ids", []) or []
            if str(item).strip()
        ]
        if not evidence_ids:
            return GateResult(
                False,
                message="Step completion candidate did not cite execution evidence.",
                feedback="Cite one or more eligible execution_id values from the current action contract.",
                classification="step_candidate_missing_evidence",
            )

        records_by_id: Dict[str, Dict[str, Any]] = {}
        for record in execution_records:
            if not isinstance(record, dict):
                continue
            for key in ("execution_id", "action_id", "attempt_id"):
                record_id = str(record.get(key) or "").strip()
                if record_id:
                    records_by_id[record_id] = record
        missing_ids = [item for item in evidence_ids if item not in records_by_id]
        if missing_ids:
            return GateResult(
                False,
                message="Step completion candidate cites unknown execution evidence.",
                feedback="Unknown evidence_ids: " + ", ".join(missing_ids),
                classification="step_candidate_unknown_evidence",
            )

        records = [records_by_id[item] for item in evidence_ids]
        failed_ids = [
            evidence_id
            for evidence_id, record in zip(evidence_ids, records)
            if not _successful_execution_record(record)
        ]
        if failed_ids:
            return GateResult(
                False,
                message="Step completion candidate cites unsuccessful execution evidence.",
                feedback="Unsuccessful evidence_ids: " + ", ".join(failed_ids),
                classification="step_candidate_unsuccessful_evidence",
            )

        purpose = str(step.get("purpose") or "").strip().lower()
        same_step_record = any(str(record.get("step_id") or "") == step_id for record in records)
        if not same_step_record:
            if purpose != "validation" or any(
                str(record.get("purpose") or "").strip().lower() != "validation"
                for record in records
            ):
                return GateResult(
                    False,
                    message="Cross-step evidence may only satisfy a validation step.",
                    feedback="Run the current analysis step or cite evidence produced for this exact step.",
                    classification="step_candidate_cross_step_scope",
                )

        renderer_execution = step.get("renderer_execution") if isinstance(step.get("renderer_execution"), dict) else {}
        renderer_manifest = _read_renderer_manifest(Path(str(renderer_execution.get("manifest_path") or "")))
        renderer_complete = bool(
            renderer_manifest
            and renderer_manifest_complete(renderer_manifest, renderer_execution)
        )
        evidence_text = "\n".join(_execution_record_text(record) for record in records)
        unsupported = [] if renderer_complete else [
            str(criterion)
            for criterion in step.get("success_criteria", []) or []
            if str(criterion).strip() and not _criterion_supported(str(criterion), evidence_text)
        ]
        if unsupported:
            return GateResult(
                False,
                message="Execution evidence does not support every current-step success criterion.",
                feedback="Missing criterion evidence: " + "; ".join(unsupported),
                classification="step_candidate_evidence_insufficient",
                reconciliation={"evidence_ids": evidence_ids, "unsupported_criteria": unsupported},
            )

        preview = deepcopy(plan_state)
        preview_step = current_running_step(preview)
        if not preview_step:
            return GateResult(False, message="Current step disappeared during candidate validation.")
        preview_evidence = list(preview_step.get("evidence") or [])
        preview_evidence.append({
            "event": "execution_evidence_relation",
            "detail": _relation_detail(records[-1], step_id),
        })
        preview_step["evidence"] = preview_evidence[-20:]
        step_gate = CompletionGate.validate_current_step(data_path, task_id, preview)
        if not step_gate.passed:
            step_gate.classification = step_gate.classification or "step_candidate_gate_failed"
            step_gate.reconciliation = {
                **dict(step_gate.reconciliation or {}),
                "evidence_ids": evidence_ids,
            }
            return step_gate
        return GateResult(
            True,
            message="Execution evidence satisfies the current step.",
            checked_artifacts=list(step_gate.checked_artifacts),
            classification="step_completion_candidate_valid",
            reconciliation={"evidence_ids": evidence_ids, "record": records[-1]},
        )

    @staticmethod
    def validate_current_step(data_path: str | Path, task_id: str, plan_state: Dict[str, Any]) -> GateResult:
        step = current_running_step(plan_state)
        if not step:
            return GateResult(True)
        step_id = str(step.get("step_id") or "")
        step_status = str(step.get("status") or "").strip().lower()
        if step_status in {"waiting_upstream", "invalidated"}:
            return GateResult(
                False,
                message=f"Current step {step_id} is {step_status}.",
                feedback="The current step cannot complete until its upstream recovery has been revalidated.",
                classification="step_provenance_invalidated",
                plan_version=str(plan_state.get("plan_version") or ""),
            )
        provenance_evidence = CompletionGate.validate_step_evidence(plan_state, step_id)
        provenance = ensure_provenance_state(plan_state)
        explicitly_invalid = [
            artifact_id
            for artifact_id in step.get("output_artifact_ids", []) or []
            if str(
                provenance.get("artifacts", {}).get(
                    provenance.get("artifact_heads", {}).get(str(artifact_id), ""), {}
                ).get("status") or ""
            ).lower() == "invalid"
        ]
        if provenance_evidence.stale_outputs or explicitly_invalid:
            return GateResult(
                False,
                message="Current-step provenance contains stale or invalid outputs.",
                feedback="Re-execute or revalidate the current step against its active producer attempt.",
                classification="step_provenance_not_valid",
                stale_artifacts=list(provenance_evidence.stale_outputs),
                invalid_artifacts=list(explicitly_invalid),
                attempt_ids=list(provenance_evidence.attempt_ids),
                artifact_version_ids=list(provenance_evidence.artifact_version_ids),
                validation_ids=list(provenance_evidence.validation_ids),
                plan_version=provenance_evidence.plan_version,
            )
        renderer_gate = _validate_renderer_execution_state(plan_state)
        if not renderer_gate.passed:
            return renderer_gate
        renderer_execution = step.get("renderer_execution") if isinstance(step.get("renderer_execution"), dict) else {}
        renderer_manifest = _read_renderer_manifest(Path(str(renderer_execution.get("manifest_path") or "")))
        if renderer_manifest and renderer_manifest_complete(renderer_manifest, renderer_execution):
            return GateResult(
                True,
                message="Renderer manifest actions and artifacts satisfy the composite step.",
                checked_artifacts=list(renderer_gate.checked_artifacts),
                classification="renderer_composite_step_valid",
            )
        title = _step_contract_text(step)
        output_root = Path(data_path)
        script_path, stdout = _last_execution_evidence(step)
        code = _read_script(script_path)
        discovery_text = "\n".join([title, script_path, code, stdout])
        output_roots = _candidate_output_roots_from_text(output_root, discovery_text)
        not_before = _plan_start_epoch(plan_state)
        checked: List[str] = []

        reconciliation = _preview_manifest_reconciliation(
            step,
            output_roots,
            discovery_text,
            task_id=task_id,
            not_before=not_before,
        )
        if reconciliation:
            events = reconciliation.get("events") or []
            summary = "; ".join(
                f"{item.get('old_path')} -> {item.get('new_path')}"
                for item in events
                if isinstance(item, dict)
            )
            return GateResult(
                passed=False,
                message="Planner and renderer artifact contracts require deterministic reconciliation.",
                feedback=(
                    "The latest successful renderer manifest provides canonical physical artifact paths. "
                    f"Reconcile the plan state without rerunning execution: {summary}"
                ),
                checked_artifacts=[str(item.get("new_path") or "") for item in events if isinstance(item, dict)],
                classification="contract_reconciliation_required",
                reconciliation=reconciliation,
            )

        if _looks_like_directory_prep_step(title):
            return GateResult(True)

        if _requires_balance_weight_check(title) and not _has_balance_weight_evidence(code, stdout):
            return GateResult(
                passed=False,
                message="Current plan step requires balance/weight validation, but the latest command did not inspect bins/weight or finite weights.",
                feedback=(
                    "Step completion gate failed: this step asks for balance/weight status, "
                    "but the last command only produced metadata or did not show bins/weight evidence. "
                    "Continue this same step by checking each relevant cooler URI's bins/weight column, "
                    "finite/non-null weight count, total bins, and balanced status before moving on."
                ),
            )

        contracts = step_artifact_contracts(step)
        artifacts = [str(item.get("path") or "") for item in contracts if str(item.get("path") or "")]
        if not artifacts:
            artifacts = _artifact_candidates(title)
            for artifact in _implicit_artifacts_from_output_text(title):
                if artifact not in artifacts:
                    artifacts.append(artifact)
        if _looks_like_artifact_step(title) or artifacts:
            checked.extend(artifacts)
            canonical_contracts = bool(contracts) and any(
                str(contract.get("source") or "") in {"renderer_manifest", "structured_plan", "skill_contract"}
                for contract in contracts
            )
            if canonical_contracts:
                stale = [
                    str(contract.get("path") or "")
                    for contract in contracts
                    if bool(contract.get("required", True))
                    and _contract_exists(contract, output_roots)
                    and not _contract_exists(contract, output_roots, not_before=not_before)
                ]
                missing = [
                    str(contract.get("path") or "")
                    for contract in contracts
                    if bool(contract.get("required", True))
                    and not _contract_exists(contract, output_roots, not_before=not_before)
                ]
                mention_artifacts = list(artifacts)
            else:
                stale = [
                    artifact for artifact in artifacts
                    if _artifact_exists_in_roots(output_roots, artifact, allow_semantic=False)
                    and not _artifact_exists_in_roots(
                        output_roots, artifact, allow_semantic=False, not_before=not_before
                    )
                ]
                missing, mention_artifacts = _missing_artifacts_considering_alternatives(
                    artifacts,
                    _artifact_alternative_groups(title),
                    output_roots,
                    allow_semantic=False,
                    not_before=not_before,
                )
                if missing:
                    missing_contracts = [
                        contract for contract in contracts
                        if str(contract.get("path") or "") in set(missing)
                    ]
                    possible = _bounded_contract_candidates(missing_contracts, output_roots)
                    if possible:
                        return GateResult(
                            passed=False,
                            message="Promised artifact paths are missing, but bounded discovery found unverified alternatives.",
                            feedback=(
                                "Completion Gate found possible replacement artifacts without a trusted renderer manifest. "
                                "Do not accept them by filename similarity and do not rerun the successful analysis yet. "
                                "Perform a bounded read-only diagnostic or an explicit plan patch. Candidates: "
                                + json.dumps(possible, ensure_ascii=False, sort_keys=True)
                            ),
                            missing_artifacts=missing,
                            checked_artifacts=checked,
                            classification="diagnostic_probe_required",
                            reconciliation={"candidate_artifacts": possible},
                        )
            if stale:
                return GateResult(
                    passed=False,
                    message="Current plan step is satisfied only by pre-existing artifacts.",
                    feedback=(
                        "Completion Gate found non-empty files, but they predate the current task/plan provenance "
                        "boundary and have no current producer evidence: " + ", ".join(stale)
                    ),
                    missing_artifacts=stale,
                    checked_artifacts=checked,
                    classification="stale_preexisting_artifact",
                )
            if missing and _is_metadata_or_prep_only(code) and not _has_write_intent(code):
                return GateResult(
                    passed=False,
                    message="Current plan step is an output/artifact step, but the latest command only prepared or inspected resources.",
                    feedback=(
                        "Step completion gate failed: the current plan step promises output/report/result artifacts, "
                        "but the last command only prepared or inspected resources. Generate the promised files now, "
                        "then verify they exist and are non-empty before marking the step done."
                    ),
                    missing_artifacts=missing,
                    checked_artifacts=checked,
                    classification="artifact_missing",
                )
            if missing:
                roots_text = ", ".join(str(root) for root in output_roots)
                return GateResult(
                    passed=False,
                    message=f"Current plan step is missing promised artifacts: {', '.join(missing)}",
                    feedback=(
                        "Step completion gate failed: the current step promised these artifacts but they are missing "
                        f"or empty under searched roots ({roots_text}): {', '.join(missing)}. "
                        "Continue the step and create/validate them."
                    ),
                    missing_artifacts=missing,
                    checked_artifacts=checked,
                    classification="artifact_missing",
                )
            if mention_artifacts and not _latest_command_mentions_artifacts(code, stdout, mention_artifacts):
                return GateResult(
                    passed=False,
                    message="Current plan step artifacts already exist, but the latest command did not create or verify the promised artifacts.",
                    feedback=(
                        "Step completion gate failed: the current step promised output/report/result artifacts, "
                        "but those artifacts appear to be pre-existing or from another step because the latest command "
                        "did not mention them. Continue this same step by creating or directly verifying the promised "
                        f"artifacts: {', '.join(mention_artifacts)}."
                    ),
                    checked_artifacts=checked,
                )

        return GateResult(True, checked_artifacts=checked)

    @staticmethod
    def validate_final(
        data_path: str | Path,
        task_id: str,
        plan_state: Dict[str, Any],
        current_plan: str = "",
        *,
        require_execution_evidence: bool = False,
        require_supervisor_evidence: bool = False,
    ) -> GateResult:
        output_root = Path(data_path)
        if isinstance(plan_state.get("approval_snapshot"), dict) and plan_state.get("approval_snapshot"):
            approval_gate = approval_applicability_v2(plan_state)
            if not approval_gate.get("applicable"):
                return GateResult(
                    False,
                    message="Final validation requires an applicable immutable approval snapshot.",
                    feedback=(
                        "Approval Gate rejected completion: "
                        + str(approval_gate.get("status") or "unverifiable")
                    ),
                    classification="approval_" + str(approval_gate.get("status") or "unverifiable"),
                )
        task_evidence = CompletionGate.validate_task_evidence(plan_state)
        if task_evidence.invalidated_steps or task_evidence.waiting_upstream_steps:
            return GateResult(
                False,
                message="Final validation found recoverable plan steps that are not validly complete.",
                feedback=(
                    "Re-run or revalidate the affected dependency branch before finalizing. "
                    f"Invalidated: {', '.join(task_evidence.invalidated_steps) or '-'}; "
                    f"waiting upstream: {', '.join(task_evidence.waiting_upstream_steps) or '-'} ."
                ),
                classification="task_provenance_incomplete",
                stale_artifacts=list(task_evidence.stale_artifacts),
                invalid_artifacts=list(task_evidence.invalid_artifacts),
                plan_version=task_evidence.plan_version,
            )
        if task_evidence.stale_artifacts or task_evidence.invalid_artifacts:
            return GateResult(
                False,
                message="Final validation found stale or invalid artifact provenance.",
                feedback="Existing files from invalidated producer attempts cannot satisfy final completion.",
                classification="task_artifact_provenance_invalid",
                stale_artifacts=list(task_evidence.stale_artifacts),
                invalid_artifacts=list(task_evidence.invalid_artifacts),
                plan_version=task_evidence.plan_version,
            )
        renderer_gate = _validate_renderer_execution_state(plan_state)
        if not renderer_gate.passed:
            return renderer_gate
        try:
            task_spec = canonical_task_spec_for_state(plan_state)
        except CanonicalTaskStateError as exc:
            return GateResult(
                False,
                message="Final validation cannot read the canonical TaskSpec.",
                feedback=str(exc),
                classification="canonical_task_state_corrupt",
            )
        is_v2 = _uses_canonical_completion_contract(plan_state)
        if is_v2 and not task_spec:
            return GateResult(
                False,
                message="Final validation requires an initialized canonical TaskSpec.",
                feedback="V2 current_task is uninitialized; route through Consultant before completion.",
                classification="canonical_task_uninitialized",
            )
        expected = _expected_artifacts_from_final_plan(plan_state, current_plan)
        # A V2 final gate derives roots and exclusions from canonical contracts;
        # plan prose is only a one-version legacy read adapter.
        gate_plan_text = "" if is_v2 else current_plan
        output_roots = _final_output_roots(output_root, plan_state, gate_plan_text)
        not_before = _plan_start_epoch(plan_state)
        degraded_disclosures = _validate_degraded_manifest_disclosures(output_roots)
        if not degraded_disclosures.passed:
            return degraded_disclosures
        expected = [
            artifact for artifact in expected
            if _artifact_allowed_for_final(artifact, gate_plan_text, output_roots)
        ]
        alternative_groups = (
            _canonical_output_alternative_groups(plan_state)
            if is_v2
            else _artifact_alternative_groups(
                "\n".join([current_plan] + [_step_title(step) for step in _steps(plan_state)])
            )
        )
        missing, _ = _missing_artifacts_considering_alternatives(
            expected,
            alternative_groups,
            output_roots,
            not_before=not_before,
        )
        stale = [
            artifact for artifact in expected
            if _artifact_exists_in_roots(output_roots, artifact)
            and not _artifact_exists_in_roots(output_roots, artifact, not_before=not_before)
        ]
        if stale:
            return GateResult(
                passed=False,
                message="Final validation found only pre-existing artifacts for this task.",
                feedback=(
                    "Final completion gate rejected files that predate the current task/plan provenance boundary: "
                    + ", ".join(stale)
                ),
                missing_artifacts=stale,
                checked_artifacts=expected,
                classification="stale_preexisting_artifact",
            )
        if expected and missing:
            if _plan_declares_blocked(plan_state, current_plan) and _blocked_status_artifacts_present(output_roots):
                return GateResult(True, checked_artifacts=expected)
            roots_text = ", ".join(str(root) for root in output_roots)
            return GateResult(
                passed=False,
                message=f"Final validation missing expected artifacts: {', '.join(missing)}",
                feedback=(
                    "Final completion gate failed: the plan is marked complete, but these promised artifacts are missing "
                    f"or empty under searched roots ({roots_text}): {', '.join(missing)}. Reopen the final output step, generate the files, "
                    "and verify them before finishing."
                ),
                missing_artifacts=missing,
                checked_artifacts=expected,
            )

        blocked_complete = _plan_declares_blocked(plan_state, current_plan) and _blocked_status_artifacts_present(output_roots)
        if require_execution_evidence and not blocked_complete and not _successful_execution_evidence(plan_state, data_path, task_id):
            roots_text = ", ".join(str(root) for root in output_roots)
            return GateResult(
                passed=False,
                message="Final validation found no successful Execute evidence.",
                feedback=(
                    "Final completion gate failed: this managed analysis is being finished without any recorded "
                    "`execute_success` evidence or non-empty `step_*.sh` script under task_outputs. "
                    f"Searched output roots: {roots_text}. Route back to Generate and run the planned analysis command, "
                    "or write truthful blocked/partial-success status artifacts if execution cannot proceed."
                ),
                checked_artifacts=expected,
            )

        if require_supervisor_evidence and not blocked_complete and not _supervisor_acceptance_evidence(plan_state):
            return GateResult(
                passed=False,
                message="Final validation found no Supervisor PASS/WARN evidence.",
                feedback=(
                    "Final completion gate failed: no plan evidence shows a Supervisor PASS/WARN for the executed analysis. "
                    "Route through Supervisor final audit or execute the current step and let Supervisor review it before finishing."
                ),
                checked_artifacts=expected,
            )

        unfinished = _unfinished_plan_steps(plan_state)
        if unfinished:
            preview = "; ".join(unfinished[:5])
            if len(unfinished) > 5:
                preview += f"; ... (+{len(unfinished) - 5} more)"
            return GateResult(
                passed=False,
                message=f"Final validation found unfinished plan steps: {preview}",
                feedback=(
                    "Final completion gate failed: the plan still has unfinished required steps. "
                    f"Finish, explicitly skip, or block these steps before producing the final answer: {preview}."
                ),
                checked_artifacts=expected,
            )

        if not expected and not _non_task_output_files(output_root):
            return GateResult(
                passed=False,
                message="Final validation found no user-facing artifacts outside task_outputs.",
                feedback=(
                    "Final completion gate failed: all plan steps are marked done, but no user-facing output files were found "
                    f"under {output_root} outside task_outputs. Generate a report/reproducibility artifact or explicitly explain "
                    "why the task is blocked before finishing."
                ),
            )

        return GateResult(True, checked_artifacts=expected)
