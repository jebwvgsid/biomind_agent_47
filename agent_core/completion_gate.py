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


def _artifact_candidates(text: str) -> List[str]:
    seen: set[str] = set()
    artifacts: List[str] = []
    for match in ARTIFACT_RE.finditer(str(text or "")):
        artifact = match.group(1).strip("`'\".,;:()[]{}")
        if not artifact or artifact.startswith(("http://", "https://")):
            continue
        # Input files should not become final-output requirements unless a
        # plan/output step explicitly asks to generate them.
        if artifact.endswith((".mcool", ".cool", ".fa", ".fasta", ".fq", ".fastq")):
            context = str(text or "")[max(0, match.start() - 60): match.end() + 60].lower()
            if not any(term in context for term in ARTIFACT_STEP_TERMS):
                continue
        key = artifact.lower()
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(artifact)
    return artifacts


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

    return False


def _artifact_exists(output_root: Path, artifact: str) -> bool:
    return _artifact_exists_in_roots([output_root], artifact)


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

    # Resolve relative output paths against every explicitly discovered output
    # root. This preserves the no-global-recursive-search rule while allowing a
    # plan step such as "Done: gene_density.bed" to be satisfied by stdout or a
    # status file that points to "Compartments/PC1_AB/gene_density.bed" after
    # the script has cd'ed into the benchmark run directory.
    changed = True
    while changed:
        changed = False
        known_roots = list(roots)
        for token in tokens:
            path = Path(token)
            if path.is_absolute():
                continue
            for root in known_roots:
                candidate = root / path
                before = len(roots)
                if path.suffix:
                    for parent in (candidate.parent, candidate.parent.parent):
                        if str(parent) not in {"", "."}:
                            add(parent)
                else:
                    add(candidate)
                if len(roots) != before:
                    changed = True
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
            checked.extend(artifacts)
            missing = [artifact for artifact in artifacts if not _artifact_exists_in_roots(output_roots, artifact)]
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

        return GateResult(True, checked_artifacts=checked)

    @staticmethod
    def validate_final(data_path: str | Path, task_id: str, plan_state: Dict[str, Any], current_plan: str = "") -> GateResult:
        output_root = Path(data_path)
        steps = _steps(plan_state)
        if steps and not all(str(step.get("status") or "").lower() == "done" for step in steps):
            return GateResult(True)

        expected = _expected_artifacts_from_final_plan(plan_state, current_plan)
        output_roots = _candidate_output_roots(output_root, plan_state, current_plan)
        missing = [artifact for artifact in expected if not _artifact_exists_in_roots(output_roots, artifact)]
        if expected and missing:
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
