"""Generic plan-step and final completion validation gates.

This module deliberately avoids benchmark-specific expectations. It validates
whether the current plan step appears to have fulfilled its own contract before
BioMind advances, and whether the completed plan produced user-facing artifacts
before finalizing the task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ARTIFACT_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_.+\-/]+"
    r"\.(?:tsv|csv|txt|md|html?|json|ya?ml|vcf(?:\.gz)?|bed(?:graph)?|rds|"
    r"png|pdf|svg|fa|fasta|fq|fastq|gfa|cool|mcool|sh|R|r|py))\b"
)

PATH_TOKEN_RE = re.compile(r"(?<![\w.-])((?:/|[A-Za-z0-9_.+\-]+/)[^\s`'\"<>|]+)")

MAX_OUTPUT_ROOTS = 64
MAX_RELATIVE_OUTPUT_PATHS = 128
MAX_SEMANTIC_ARTIFACT_SCAN_DEPTH = 3
MAX_SEMANTIC_ARTIFACT_SCAN_ENTRIES = 512
MAX_DECLARATION_SCAN_FILES = 256
MAX_DECLARATION_FILE_BYTES = 2_000_000

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


def _steps(plan_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [step for step in plan_state.get("steps", []) or [] if isinstance(step, dict)]


def current_running_step(plan_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for step in _steps(plan_state):
        if str(step.get("status") or "").strip().lower() == "running":
            return step
    return None


def _step_title(step: Optional[Dict[str, Any]]) -> str:
    if not step:
        return ""
    return str(step.get("title") or "").strip()


def _last_execution_evidence(step: Dict[str, Any]) -> Tuple[str, str]:
    """Return (script_path, stdout_excerpt) from the latest execution evidence."""
    evidence = step.get("evidence")
    if not isinstance(evidence, list):
        return "", ""
    for item in reversed(evidence):
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "")
        if event not in {"execute_success", "execute_failed", "runtime_missing_after_execute"}:
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
        existing = [artifact for artifact in group if _artifact_exists_in_roots(roots, artifact)]
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
        if not _artifact_exists_in_roots(roots, artifact):
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
        if not re.search(r"\b(output\s+dir(?:ectory)?|输出目录)\b", line, re.IGNORECASE):
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
    terminal_statuses = {"done", "blocked", "skipped", "cancelled"}
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
    for step in _steps(plan_state):
        evidence = step.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            detail = str(item.get("detail") or "").upper()
            event = str(item.get("event") or "").strip().lower()
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


def _artifact_exists_in_roots(output_roots: Iterable[Path], artifact: str) -> bool:
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
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except OSError:
            continue

    if not path.is_absolute() and len(path.parts) == 1 and _semantic_artifact_exists_in_roots(output_roots, artifact):
        return True

    return False


def _artifact_exists(output_root: Path, artifact: str) -> bool:
    return _artifact_exists_in_roots([output_root], artifact)


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


def _semantic_artifact_exists_in_roots(output_roots: Iterable[Path], artifact: str) -> bool:
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
                    if not candidate.is_file() or candidate.stat().st_size <= 0:
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
    for step in _steps(plan_state):
        title = _step_title(step)
        if _looks_like_artifact_step(title):
            snippets.append(title)

    text = str(current_plan or "")
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
    for snippet in snippets:
        for artifact in _artifact_candidates(snippet):
            key = artifact.lower()
            if key not in seen:
                seen.add(key)
                artifacts.append(artifact)
        for artifact in _implicit_artifacts_from_output_text(snippet):
            key = artifact.lower()
            if key not in seen:
                seen.add(key)
                artifacts.append(artifact)
    return artifacts


def _text_for_output_root_discovery(plan_state: Dict[str, Any], current_plan: str) -> str:
    chunks = [str(current_plan or "")]
    for step in _steps(plan_state):
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


class CompletionGate:
    """Stateless validators for plan-step and final task completion."""

    @staticmethod
    def validate_code_boundary(code: str, task_context: str) -> GateResult:
        return GateResult(True)

    @staticmethod
    def validate_current_step(data_path: str | Path, task_id: str, plan_state: Dict[str, Any]) -> GateResult:
        step = current_running_step(plan_state)
        if not step:
            return GateResult(True)
        title = _step_title(step)
        output_root = Path(data_path)
        script_path, stdout = _last_execution_evidence(step)
        code = _read_script(script_path)
        discovery_text = "\n".join([title, script_path, code, stdout])
        output_roots = _candidate_output_roots_from_text(output_root, discovery_text)
        checked: List[str] = []

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

        if _looks_like_artifact_step(title):
            artifacts = _artifact_candidates(title)
            for artifact in _implicit_artifacts_from_output_text(title):
                if artifact not in artifacts:
                    artifacts.append(artifact)
            checked.extend(artifacts)
            missing, mention_artifacts = _missing_artifacts_considering_alternatives(
                artifacts,
                _artifact_alternative_groups(title),
                output_roots,
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

        declared = _declared_completed_artifacts(output_roots)
        if declared:
            checked.extend(artifact for artifact in declared if artifact not in checked)
            missing_declared = [
                artifact for artifact in declared
                if not _artifact_exists_in_roots(output_roots, artifact)
            ]
            if missing_declared:
                roots_text = ", ".join(str(root) for root in output_roots)
                return GateResult(
                    passed=False,
                    message=f"Current step status/report declares completed artifacts that are missing: {', '.join(missing_declared)}",
                    feedback=(
                        "Step completion gate failed: a status/report file declares these artifacts as generated or complete, "
                        f"but they are missing or empty under searched roots ({roots_text}): {', '.join(missing_declared)}. "
                        "Create the missing artifacts or correct the status file before advancing."
                    ),
                    missing_artifacts=missing_declared,
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
        expected = _expected_artifacts_from_final_plan(plan_state, current_plan)
        output_roots = _trusted_final_output_roots(output_root, current_plan)
        expected = [
            artifact for artifact in expected
            if _artifact_allowed_for_final(artifact, current_plan, output_roots)
        ]
        declared = _declared_completed_artifacts(output_roots)
        for artifact in declared:
            if not _artifact_allowed_for_final(artifact, current_plan, output_roots):
                continue
            if artifact not in expected:
                expected.append(artifact)
        alternative_context = "\n".join([current_plan] + [_step_title(step) for step in _steps(plan_state)])
        missing, _ = _missing_artifacts_considering_alternatives(
            expected,
            _artifact_alternative_groups(alternative_context),
            output_roots,
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
