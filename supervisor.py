"""Explicit domain assessments and parsers retained from the former Supervisor.

These helpers return observations/advice only. They own no model client, session,
retry policy, Step gate, final audit, workflow routing or terminal lifecycle.
RuleEngine and its result vocabulary remain available for explicit domain checks;
no automatic scientific-success verdict is installed in the Pi request loop.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Sequence

from agent_core.recovery import ArtifactValidator, ExecutionRecord
from agent_core.semantic_completion import SemanticCompletionError, validate_reviewer_payload
from agent_core.task_spec import effective_task_contract, evaluate_requirements
from agent_core.plan_tracker import canonical_task_spec_for_state

logger = logging.getLogger("BioMind.DomainAssessment")

def _effective_declared_requirements(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    task_spec = canonical_task_spec_for_state(state)
    if task_spec:
        return list(effective_task_contract(task_spec).get("requirements") or [])
    return list(state.get("requirements") or []) if isinstance(state.get("requirements"), list) else []


class Verdict(str, Enum):
    """Legacy report vocabulary retained only for dormant compatibility code."""
    PASS      = "PASS"       # 结果正确，继续下一步
    WARN      = "WARN"       # 有小问题但可接受，附警告继续
    RETRY     = "RETRY"      # 结果有错，让 generate 重试（最多 max_retries 次）
    ESCALATE  = "ESCALATE"   # 多次重试仍失败，上报给用户决策
    BLOCKED   = "BLOCKED"     # 不能继续且已经写出真实阻塞证据
    NO_PROGRESS = "NO_PROGRESS"  # 重试没有产生新证据或状态变化
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"  # 修复引入了更严重问题
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"  # 恢复预算耗尽


@dataclass
class CheckResult:
    rule: str
    passed: bool
    severity: str          # "error" | "warning" | "info"
    message: str
    suggestion: str = ""


class RuleEngine:
    """基于正则 + 启发式规则的静态检查器"""

    # 幻觉信号：AI 宣告成功但没有执行证据
    HALLUCINATION_PHRASES = [
        r"installation\s+(?:is\s+)?(?:complete|successful|done)",
        r"(?:successfully|correctly)\s+installed",
        r"tool\s+is\s+(?:now\s+)?(?:ready|available|working)",
        r"all\s+steps\s+(?:have\s+been\s+)?completed",
        r"pipeline\s+(?:is\s+)?finished",
    ]

    # 致命错误信号（即使 returncode=0 也要警告）
    SILENT_FAILURE_PATTERNS = [
        (r"warning:\s*no\s+reads\s+mapped",            "Mapping rate is 0% – check reference genome"),
        (r"0\s+variants\s+called",                     "Zero variants called – possible parameter issue"),
        (r"terminated\s+(?:with\s+)?signal\s+\d+",     "Process terminated by signal – likely OOM or timeout"),
        (r"output\s+file\s+is\s+empty",                "Output file is empty – check input data"),
        (r"deprecat(?:ed|ion)",                        "Using deprecated API – may break in future"),
        (r"java\.lang\.outofmemoryerror",              "Java OOM – increase -Xmx heap or reduce threads"),
        (r"\bnan\b|\binf\b",                           "NaN/Inf detected in output – possible division error"),
    ]

    HARD_FAILURE_PATTERNS = [
        (r"\bA USER ERROR has occurred\b",              "Tool reported A USER ERROR"),
        (r"\bERROR\s+conda\.cli\.main_run:execute\b",  "conda run wrapped command failed"),
        (r"\bTraceback \(most recent call last\):",     "Python traceback detected"),
        (r"\b[Ff]atal error\b",                         "Fatal error detected"),
        (r"\bException in thread\b",                    "JVM exception detected"),
        (r"biomind shim error",                         "BioMind shim is stale or bound to the wrong runtime"),
        (r"rscript:\s*command not found",                "Rscript is missing from the selected runtime"),
        (r"calder2?\s+not\s+available",                 "CALDER R package is not available"),
        (r"there is no package called ['\"]?calder",     "CALDER R package is not installed"),
        (r"gene_density(?:\.bed)?[^\n]*(?:sum|总和)[^\n]*(?:0|为 0)", "gene_density has zero signal"),
    ]

    # 生信科学性规则（正向模式 → 预期存在）
    BIO_POSITIVE_CHECKS = {
        "samtools_sort":    r"bam|cram",
        "bwa_mem":          r"\.sam\b|\.bam\b|mapped\s+reads",
        "fastqc":           r"html|fastqc_report|per\s+base\s+sequence",
        "gatk":             r"\.vcf\b|variant|genotype",
        "trim_galore":      r"trimmed|adapter|quality",
        "featurecounts":    r"assigned|unassigned|feature",
        "deseq2":           r"log2.*fold|padj|basemean",
    }

    NON_PROGRESS_PATTERNS = [
        r"^\s*(?:conda\s+run\s+-n\s+\S+\s+)?(?:python(?:3)?\s+)?\S+\.py\s+(?:--help|-h)\s*$",
        r"^\s*(?:conda\s+run\s+-n\s+\S+\s+)?\S+\s+(?:--help|-h)\s*$",
        r"^\s*(?:ls|dir)(?:\s+[-\w]+)*(?:\s+\S+)?\s*$",
        r"^\s*(?:which|where|type)\s+\S+\s*$",
    ]

    EXPECTED_SILENT_COMMAND_PATTERNS = [
        r"^\s*(?:cd|pushd|popd)\b",
        r"^\s*(?:mkdir|chmod|touch|sync)\b",
        r"^\s*(?:rm|rmdir)\s+",
        r"^\s*(?:cp|mv)\s+",
        r"^\s*(?:echo|printf|cat|awk|sed|perl)\b.*(?:>>?|tee\s+-a?\b)",
    ]

    ARTIFACT_INSPECTION_RE = re.compile(
        r"\.(?:bed|bedgraph|gff|gtf|tsv|csv|json|jsonl|md|png|pdf|html|h5ad|h5|rds|rda|cool|mcool|bm)\b",
        re.IGNORECASE,
    )
    EMPTY_ALLOWED_ARTIFACT_RE = re.compile(
        r"(?:^|[_\-/])(?:no[_-]?hits?|missing|unmatched|skipped|absent|empty[_-]?allowed)(?:[_\-.]|$)",
        re.IGNORECASE,
    )
    EMPTY_STATUS_DECISION_RE = re.compile(
        r"(?:stage[_-]?)?(?:blocked|skipped|unresolved|not[_ -]?run|not[_ -]?attempted|unavailable)",
        re.IGNORECASE,
    )
    EMPTY_STATUS_REASON_RE = re.compile(
        r"(?:missing|not[_ -]?(?:provided|available|applicable|requested|run|attempted)|unavailable|"
        r"skipped|optional|conditional|branch|requires?|dependency|runtime|environment|tool|package|"
        r"network|database|reference|metadata|parameter|permission|quota|timeout|service|external|"
        r"input|upstream|user[_ -]?decision|cannot[_ -]?confirm|uncertain|no[_ -]?valid)",
        re.IGNORECASE,
    )
    EMPTY_STATUS_FALSE_RESULT_RE = re.compile(
        r"(?:biological\s+)?no[_ -]?(?:hit|difference)|no\s+significant|no\s+differential|"
        r"\b0\s+(?:hits|results|rows|features|variants|domains|loops)\b",
        re.IGNORECASE,
    )
    ZERO_SIZE_ARTIFACT_EXT_RE = re.compile(
        r"\.(?:tsv|csv|txt|json|jsonl|md|html?|bed|bedgraph|gff|gtf|vcf(?:\.gz)?|fa|fasta|fq|fastq|png|pdf|svg|rds|h5ad|h5)\b",
        re.IGNORECASE,
    )

    @classmethod
    def is_non_progress_command(cls, code: str) -> bool:
        lines = [
            line.strip()
            for line in str(code or "").splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("set ")
            and not line.strip().startswith("export ")
        ]
        if not lines:
            return False
        inspection_commands = {"head", "grep", "rg", "sed", "cat", "tail", "awk", "less", "more"}

        def inspection_segment(segment: str) -> bool:
            segment = re.sub(r"^\s*(?:cd|pushd|popd)\b.*?&&\s*", "", segment.strip(), flags=re.IGNORECASE)
            if re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", segment, re.IGNORECASE):
                return True
            pipeline = [part.strip() for part in re.split(r"(?<!\|)\|(?!\|)", segment) if part.strip()]
            if pipeline and all(
                (re.match(r"^(?:\S+=\S+\s+)*(?P<command>[A-Za-z0-9_.+-]+)", part) is not None)
                and re.match(r"^(?:\S+=\S+\s+)*(?P<command>[A-Za-z0-9_.+-]+)", part).group("command").lower()
                in inspection_commands
                for part in pipeline
            ):
                return True
            return any(re.search(pattern, segment, re.IGNORECASE) for pattern in cls.NON_PROGRESS_PATTERNS)

        return all(inspection_segment(line) for line in lines)

    @classmethod
    def is_artifact_verification_command(cls, code: str, stdout: str = "", stderr: str = "") -> bool:
        """Return True when an inspection-only command shows concrete output artifacts.

        A plain parent-directory listing such as `ls TADs/HiCExplorer` should not
        count; it only becomes useful completion evidence when either the command
        or its output names real files with analysis-artifact extensions.
        """
        if not cls.is_non_progress_command(code):
            return False
        evidence = "\n".join([str(code or ""), str(stdout or ""), str(stderr or "")])
        return bool(cls.ARTIFACT_INSPECTION_RE.search(evidence))

    @classmethod
    def is_expected_silent_command(cls, code: str) -> bool:
        lines = [
            line.strip()
            for line in str(code or "").splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("set ")
            and not line.strip().startswith("export ")
        ]
        if not lines:
            return False
        command_like = []
        for line in lines:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:", line):
                continue
            if not re.search(r"\b(?:cd|pushd|popd|mkdir|chmod|touch|sync|rm|rmdir|cp|mv|echo|printf|cat|awk|sed|perl)\b|>>?|tee\s+-a?\b", line):
                continue
            command_like.append(line)
        if not command_like:
            return False
        return all(
            any(re.search(pattern, line, re.IGNORECASE) for pattern in cls.EXPECTED_SILENT_COMMAND_PATTERNS)
            for line in command_like
        )

    @classmethod
    def _zero_size_artifact_names(cls, text: str) -> List[str]:
        names: List[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            cleaned = str(name or "").strip().strip("`'\".,;:()[]{}")
            if not cleaned or cls.EMPTY_ALLOWED_ARTIFACT_RE.search(cleaned):
                return
            if not cls.ZERO_SIZE_ARTIFACT_EXT_RE.search(cleaned):
                return
            key = cleaned.lower()
            if key in seen:
                return
            seen.add(key)
            names.append(cleaned)

        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 9 and parts[0].startswith("-"):
                try:
                    if int(parts[4]) == 0:
                        add(parts[-1])
                except (TypeError, ValueError):
                    pass
            for match in re.finditer(
                r"(?P<name>[A-Za-z0-9_.+\-/]+\.(?:tsv|csv|txt|json|jsonl|md|html?|bed|bedgraph|gff|gtf|vcf(?:\.gz)?|fa|fasta|fq|fastq|png|pdf|svg|rds|h5ad|h5))"
                r"\s*(?:\(|:)?\s*0\s*(?:bytes?|b|lines?)\b",
                line,
                re.IGNORECASE,
            ):
                add(match.group("name"))
            for match in re.finditer(
                r"^\s*0\s+(?P<name>[A-Za-z0-9_.+\-/]+\.(?:tsv|csv|txt|json|jsonl|md|html?|bed|bedgraph|gff|gtf|vcf(?:\.gz)?|fa|fasta|fq|fastq|png|pdf|svg|rds|h5ad|h5))\s*$",
                line,
                re.IGNORECASE,
            ):
                add(match.group("name"))
        return names

    @classmethod
    def _empty_artifact_has_status_explanation(cls, artifact_name: str, text: str) -> bool:
        artifact = Path(str(artifact_name or "").strip()).name
        if not artifact:
            return False

        artifact_stem = re.sub(r"\.[^.]+(?:\.gz)?$", "", artifact, flags=re.IGNORECASE)
        status_line_terms = [artifact.lower(), artifact_stem.lower()]

        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lower = line.lower()
            if not any(term and term in lower for term in status_line_terms):
                continue
            if not cls.EMPTY_STATUS_DECISION_RE.search(line):
                continue
            if not cls.EMPTY_STATUS_REASON_RE.search(line):
                continue
            if cls.EMPTY_STATUS_FALSE_RESULT_RE.search(line):
                continue
            return True
        return False

    @classmethod
    def run(
        cls,
        code: str,
        stdout: str,
        stderr: str,
        returncode: int,
        *,
        command_role: str = "",
        expect_stdout: bool = False,
        ai_claim_text: str = "",
        approved_explicit_result_plan: bool = False,
        approved_basic_plan: bool = False,
    ) -> List[CheckResult]:
        results: List[CheckResult] = []
        combined_text = stdout + "\n" + stderr
        combined = combined_text.lower()
        code_lower = code.lower()
        role = str(command_role or "").strip().lower()
        explicit_probe = role in {
            "probe", "diagnostic", "optional_probe", "capability_probe",
        }
        inspection_command = cls.is_non_progress_command(code)

        # A shell script is not an AI completion claim. Hallucination checks
        # only compare an explicit claim against execution/artifact evidence.
        ai_text = str(ai_claim_text or "").lower()
        for pattern in cls.HALLUCINATION_PHRASES:
            if ai_text and re.search(pattern, ai_text, re.IGNORECASE):
                results.append(CheckResult(
                    rule="HallucinationDetector",
                    passed=False,
                    severity="error",
                    message=f"AI may be reporting false success: '{pattern}'",
                    suggestion="Verify actual command output before concluding success.",
                ))
                break

        # ── Rule 2: 退出码检查 ────────────────────────────────
        if returncode != 0:
            results.append(CheckResult(
                rule="ExitCodeCheck",
                passed=False,
                severity="error",
                message=f"Process exited with code {returncode}",
                suggestion="Check STDERR and STDOUT for root cause; some scripts print errors to STDOUT before exiting.",
            ))
        else:
            results.append(CheckResult(
                rule="ExitCodeCheck",
                passed=True,
                severity="info",
                message="Exit code 0 ✓",
            ))

        if approved_basic_plan:
            # The Plan's artifact/predicate validators run independently. Log
            # keywords and tool names cannot add scientific obligations.
            return results

        # ── Rule 2b: 硬失败输出检测，即使退出码为 0 ────────────
        for pattern, hint in cls.HARD_FAILURE_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                results.append(CheckResult(
                    rule="HardFailureOutputDetector",
                    passed=False,
                    severity="error",
                    message=f"Hard failure signal detected: {hint}",
                    suggestion=hint,
                ))

        # ── Rule 3: 静默失败检测 ──────────────────────────────
        for pattern, hint in cls.SILENT_FAILURE_PATTERNS:
            if explicit_probe or inspection_command:
                continue
            if re.search(pattern, combined, re.IGNORECASE):
                results.append(CheckResult(
                    rule="SilentFailureDetector",
                    passed=False,
                    severity="warning",
                    message=f"Silent failure signal detected: {hint}",
                    suggestion=hint,
                ))

        # ── Rule 4: 空输出检测 ────────────────────────────────
        if returncode == 0 and expect_stdout and not stdout.strip() and not stderr.strip():
            results.append(CheckResult(
                rule="EmptyOutputCheck",
                passed=False,
                severity="warning",
                message="Command succeeded but produced no output",
                suggestion="Add verbose flag (-v) or check output file directly.",
            ))

        # ── Rule 4a: 空 artifact 检测 ─────────────────────────
        for artifact_name in cls._zero_size_artifact_names(combined_text):
            if approved_explicit_result_plan:
                results.append(CheckResult(
                    rule="EmptyArtifactObservation",
                    passed=False,
                    severity="warning",
                    message=f"Terminal evidence mentions a zero-byte file: {artifact_name}",
                    suggestion="Apply the approved Plan artifact validator to the actual result; terminal filenames do not create required results.",
                ))
                continue
            if cls._empty_artifact_has_status_explanation(artifact_name, combined_text):
                results.append(CheckResult(
                    rule="EmptyArtifactStatusCheck",
                    passed=True,
                    severity="info",
                    message=f"Empty optional/attempt artifact has explicit blocked/skipped/unresolved status: {artifact_name}",
                ))
                continue
            results.append(CheckResult(
                rule="EmptyArtifactCheck",
                passed=False,
                severity="error",
                message=f"Required-looking artifact is empty: {artifact_name}",
                suggestion=(
                    "Do not treat an empty result artifact as a biological no-hit/no-difference conclusion. "
                    "Retry only if the next action can change the outcome; otherwise record the stage as unresolved/blocked "
                    "with the empty artifact size and relevant stderr/log evidence."
                ),
            ))

        # ── Rule 4b: 非进展命令检测 ───────────────────────────
        if returncode == 0 and inspection_command and not explicit_probe:
            results.append(CheckResult(
                rule="NonProgressCommandCheck",
                passed=False,
                severity="warning",
                message="Command only inspected/help-listed resources and did not run an analysis step",
                suggestion="Use this only for explicit preflight/checklist steps; otherwise run the planned analysis command.",
            ))

        # ── Rule 5: 工具特异性科学验证 ───────────────────────
        # Legacy compatibility only. New tasks should supply validation
        # metadata through the step/skill/renderer contract.
        for tool, expected_pattern in cls.BIO_POSITIVE_CHECKS.items():
            # Match an executable token, not a substring of an environment or
            # path name.  For example `bio_gatk` must not trigger a GATK
            # output check for a snpEff command running in that environment.
            tool_invoked = (
                bool(re.search(r"(?<![\w.-])gatk(?:4)?(?=\s|$)", code_lower))
                if tool == "gatk"
                else tool in code_lower
            )
            if tool_invoked and not explicit_probe:
                if not re.search(expected_pattern, combined, re.IGNORECASE):
                    results.append(CheckResult(
                        rule=f"BioCheck:{tool}",
                        passed=False,
                        severity="warning",
                        message=f"Expected output pattern for '{tool}' not found in stdout/stderr",
                        suggestion=f"Verify {tool} ran correctly and produced expected files/metrics.",
                    ))
                else:
                    results.append(CheckResult(
                        rule=f"BioCheck:{tool}",
                        passed=True,
                        severity="info",
                        message=f"Expected {tool} output pattern detected ✓",
                    ))

        # ── Rule 6: 内存杀手检测 ─────────────────────────────
        if re.search(r"\bkilled\b", combined) or re.search(r"exit code 137", combined):
            results.append(CheckResult(
                rule="OOMKillDetector",
                passed=False,
                severity="error",
                message="Process was killed (likely OOM – exit 137)",
                suggestion="Reduce memory usage: lower threads (-t), use streaming mode, or request more RAM.",
            ))

        return results


def bound_supervisor_contract(
    review_decision: Dict[str, Any],
    declared_requirements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply requirement policy without granting the reviewer contract authority."""
    evaluation = evaluate_requirements(declared_requirements or [])
    proposed: List[Dict[str, Any]] = []
    for item in review_decision.get("proposed_requirements", []) or []:
        proposal = dict(item) if isinstance(item, dict) else {"value": str(item)}
        proposal.update({
            "status": "advisory_not_adopted",
            "accepted": False,
            "effective_level": None,
            "source": "supervisor_llm_proposal",
        })
        proposed.append(proposal)
    return {
        "requirements": evaluation["requirements"],
        "missing_required_inputs": evaluation["missing_required"] + evaluation["missing_conditional"],
        "skipped_optional_checks": evaluation["skipped_optional"] + evaluation["skipped_known_absent"],
        "missing_recommended": evaluation["missing_recommended"],
        "unknown_requirements": evaluation["unknown"],
        "contract_status": (
            "missing_required" if evaluation["blocked"] else
            "needs_reconciliation" if evaluation["needs_reconciliation"] else
            "valid"
        ),
        "proposed_requirements": proposed,
    }


def parse_review_decision(
    text: str,
    *,
    expected_report_id: str = "",
    allowed_issue_ids: Sequence[str] = (),
    required_issue_ids: Sequence[str] = (),
    include_error: bool = False,
) -> Dict[str, Any]:
    raw = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(raw)
    error = {"code":"SEMANTIC_REVIEW_JSON_INVALID", "field_path":"$", "expected":"one complete JSON object"}
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc,json.JSONDecodeError):
                error.update(line=exc.lineno,column=exc.colno,detail=exc.msg)
            continue
        if not isinstance(value, dict):
            continue
        try:
            normalized = validate_reviewer_payload(
                value,
                expected_report_id=expected_report_id,
                allowed_issue_ids=allowed_issue_ids,
                required_issue_ids=required_issue_ids,
            )
        except SemanticCompletionError as exc:
            error=exc.to_dict()
            continue
        normalized["concerns"] = normalized["concerns"][:20]
        normalized["suggestions"] = normalized["suggestions"][:20]
        return normalized
    return {"_validation_error":error} if include_error else {}


def extract_stream(content: str, label: str) -> str:
    pattern = rf"{re.escape(label)}:[ \t]*\r?\n?(.*?)(?=\r?\nSTDOUT:|\r?\nSTDERR:|(?:\r?\n)?<validation_result|(?:\r?\n)?<runtime_status>|</observation>|\r?\n</observation>|$)"
    match = re.search(pattern, content, re.DOTALL)
    return match.group(1).strip() if match else ""


def returncode_from_observation(content: str) -> int:
    """Infer executor status from the observation header only.

    Do not scan full stdout/stderr for symbols such as "❌"; reports may use
    those symbols as data values (for example "Not balanced") even when the
    shell command itself exited successfully.
    """
    match = re.search(r"^\[[^\]\n]+\]\s*(?P<header>[^\n]*)", str(content or ""), re.MULTILINE)
    if not match:
        return 0
    header = match.group("header").strip().lower()
    if "failed" in header or "runtime missing" in header:
        return 1
    return 0


def validation_checks_from_observation(observation: str) -> List[CheckResult]:
    match = re.search(
        r"<validation_result\b(?P<attrs>[^>]*)>(?P<body>.*?)</validation_result>",
        str(observation or ""),
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    attrs = match.group("attrs") or ""
    body = match.group("body").strip()
    status_match = re.search(r"status=['\"](?P<status>[^'\"]+)['\"]", attrs, re.IGNORECASE)
    status = status_match.group("status").lower() if status_match else ""
    normalized = body.lower()
    summary = re.sub(r"\s+", " ", body).strip()[:500]

    if status == "failed" or "status: failed" in normalized or "critical failures" in normalized:
        return [
            CheckResult(
                rule="ExecutionValidator",
                passed=False,
                severity="error",
                message=f"Post-run validation failed: {summary}",
                suggestion="Fix validation failures before treating this step as complete.",
            )
        ]
    if status == "warning" or "with warnings" in normalized or "warnings:" in normalized:
        return [
            CheckResult(
                rule="ExecutionValidator",
                passed=False,
                severity="warning",
                message=f"Post-run validation warning: {summary}",
                suggestion="Review validation warnings and continue only if they are expected.",
            )
        ]
    return []


def validation_level_from_observation(observation: str) -> str:
    match = re.search(
        r"<validation_result\b(?P<attrs>[^>]*)>",
        str(observation or ""),
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    level_match = re.search(r"level=['\"](?P<level>[^'\"]+)['\"]", match.group("attrs") or "", re.IGNORECASE)
    return level_match.group("level").lower() if level_match else ""


def validation_status_from_observation(observation: str) -> str:
    match = re.search(
        r"<validation_result\b(?P<attrs>[^>]*)>",
        str(observation or ""),
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    status_match = re.search(r"status=['\"](?P<status>[^'\"]+)['\"]", match.group("attrs") or "", re.IGNORECASE)
    return status_match.group("status").lower() if status_match else ""


def extract_review_verdict(llm_text: str) -> str:
    try:
        value = json.loads(str(llm_text or ""))
    except (TypeError, json.JSONDecodeError):
        return ""
    verdict = str(value.get("verdict_recommendation") or "").upper() if isinstance(value, dict) else ""
    return verdict if verdict in {"PASS", "WARN", "RETRY", "ESCALATE"} else ""


def review_decision(llm_text: str) -> Dict[str, Any]:
    try:
        value = json.loads(str(llm_text or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def verification_checks_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = state.get("verification_checks") if isinstance(state, dict) else None
    if not isinstance(checks, list) or not checks:
        decision = state.get("recovery_decision") if isinstance(state, dict) else None
        checks = decision.get("verification_checks") if isinstance(decision, dict) else None
    if not isinstance(checks, list):
        checks = []
    evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
    current_step_id = str((evidence or {}).get("step_id") or state.get("current_step_id") or "")
    normalized = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        scoped_step = str(item.get("_recovery_step_id") or "")
        if scoped_step and current_step_id and scoped_step != current_step_id:
            continue
        if str(item.get("check_type") or "") == "content_pattern":
            pattern = str(item.get("pattern") or item.get("expected") or "").strip()
            if not pattern or len(pattern) > 500 or re.search(
                r"\b(?:must|should|needs?\s+to)\s+be\b|\bmust\s+be\s+identified\b",
                pattern,
                re.IGNORECASE,
            ):
                logger.warning("Ignoring non-machine-checkable recovery content_pattern: %r", pattern[:200])
                continue
        normalized.append(dict(item))
    explicit_artifacts = state.get("expected_artifacts") if isinstance(state, dict) else None
    if isinstance(explicit_artifacts, list):
        normalized.extend(
            {
                "check_type": "file_nonempty",
                "target": str(path),
                "required": True,
            }
            for path in explicit_artifacts
            if str(path).strip()
        )
    return normalized


def artifact_check_results(state: Dict[str, Any], *, output_dir: str) -> List[CheckResult]:
    checks = verification_checks_from_state(state)
    if not checks:
        return []
    evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
    cwd = str((evidence or {}).get("cwd") or output_dir)
    result = ArtifactValidator.validate(checks, cwd=cwd)
    rows: List[CheckResult] = []
    for row in result.get("failed_checks", []):
        check = row.get("check") or {}
        required = bool(check.get("required", True))
        rows.append(
            CheckResult(
                rule="RequiredArtifactCheck" if required else "OptionalArtifactCheck",
                passed=False,
                severity="error" if required else "warning",
                message=f"Artifact verification failed: {row.get('detail') or check.get('target')}",
                suggestion=(
                    "Do not accept the stage from exit code alone; create or repair the artifact and rerun verification."
                    if required
                    else "Record the optional artifact as skipped/unresolved if it is not applicable; do not fabricate it."
                ),
            )
        )
    return rows


def root_cause_check_results(state: Dict[str, Any]) -> List[CheckResult]:
    """Treat an evidenced inner failure as a failure even when a wrapper exits 0."""
    evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
    analysis = evidence.get("root_cause_analysis") if isinstance(evidence, dict) else None
    if not isinstance(analysis, dict):
        return []
    failed_command = str(analysis.get("failed_command") or "").strip()
    primary_error = str(analysis.get("primary_error_message") or "").strip()
    if re.match(r"^trap\b", failed_command):
        return [
            CheckResult(
                "RootCauseEvidenceProvenance",
                False,
                "warning",
                "A legacy root-cause record selected a shell trap as the failed inner command; script-source error handlers are not runtime failure evidence.",
                "Discard the trap attribution and run one bounded probe or re-analyze stderr/tool-log/shell-trace evidence.",
            )
        ]
    masked = bool(analysis.get("masked_failure")) or analysis.get("root_cause_category") == "masked_pipeline_failure"
    raw_returncode = evidence.get("returncode") if isinstance(evidence, dict) else None
    try:
        returncode = int(raw_returncode) if raw_returncode is not None else None
    except (TypeError, ValueError):
        returncode = None
    if masked:
        primary = analysis.get("primary_failure") or {}
        inner_rc = primary.get("exit_code") if isinstance(primary, dict) else None
        strong_inner_failure = bool(
            failed_command and str(analysis.get("status") or "") == "confirmed"
            and type(inner_rc) is int and inner_rc != 0
        )
        if not strong_inner_failure:
            return [CheckResult(
                "RootCauseEvidenceUncertain", False, "warning",
                "A masked-failure hypothesis lacks a confirmed inner command and nonzero exit receipt. " + primary_error,
                "Inspect the approved action, raw results and observation parser receipt; do not infer data absence from this hypothesis.",
            )]
        validation = evidence.get("validation") if isinstance(evidence, dict) else {}
        validation_passed = isinstance(validation, dict) and bool(validation.get("passed"))
        declared = {str(item) for item in evidence.get("declared_outputs", []) or [] if str(item).strip()}
        observed = {str(item) for item in evidence.get("observed_outputs", []) or [] if str(item).strip()}
        observed_names = {Path(item).name for item in observed}
        declared_satisfied = bool(declared) and all(
            item in observed or Path(item).name in observed_names for item in declared
        )
        if returncode == 0 and validation_passed and declared_satisfied:
            return [
                CheckResult(
                    "OptionalFallbackEvidence",
                    False,
                    "warning",
                    (
                        "An inner command failed, but the outer action completed, post-run validation passed, "
                        "and every declared output was observed. Treat the inner failure as bounded auxiliary/fallback evidence."
                    ),
                    "Preserve the failed command and fallback evidence in the audit trail; do not rerun the completed primary analysis.",
                )
            ]
        return [
            CheckResult(
                "RootCauseAnalysis",
                False,
                "error",
                f"Inner command failed while the outer command returned {returncode}: {failed_command or 'unresolved pipeline segment'}. {primary_error}",
                "Repair or verify the failed inner command; do not accept the wrapper's zero exit code as success.",
            )
        ]
    if failed_command and returncode not in (None, 0) and str(analysis.get("status")) in {"confirmed", "probable"}:
        return [
            CheckResult(
                "RootCauseAnalysis",
                False,
                "error",
                f"Root cause analyzer identified the failed inner command: {failed_command}. {primary_error}",
                "Fix the primary inner command before retrying downstream stages; wrapper errors are only cascade evidence.",
            )
        ]
    if str(analysis.get("status")) in {"ambiguous", "insufficient_evidence"} and returncode not in (None, 0):
        return [
            CheckResult(
                "RootCauseAnalysis",
                False,
                "warning",
                "The command returned non-zero but the failed inner process could not be identified deterministically.",
                "Run one bounded trace/probe with explicit argv rather than repeating the same wrapper command.",
            )
        ]
    return []


def structured_execution_context(state: Dict[str, Any]) -> Dict[str, Any]:
    evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    reconciliation = (
        dict(state.get("reconciliation_evidence") or {})
        if isinstance(state, dict)
        and isinstance(state.get("reconciliation_evidence"), dict)
        else {}
    )
    structured = bool(
        evidence
        and (evidence.get("returncode") is not None or evidence.get("exit_code") is not None)
        and (
            evidence.get("execution_id")
            or evidence.get("attempt_id")
            or evidence.get("action_id")
            or evidence.get("command")
            or evidence.get("generated_code")
            or evidence.get("script_file")
        )
    )
    if structured:
        record = ExecutionRecord.from_mapping(evidence)
        normalized = {
            **evidence,
            "returncode": record.exit_code,
            "execution_id": record.execution_id,
            "attempt_id": record.attempt_id,
            "action_id": record.action_id,
            "step_id": record.step_id,
            "execution_status": record.execution_status,
            "command": record.command,
            "command_role": str(evidence.get("command_role") or record.command_role),
            "stdout_summary": str(
                evidence.get("raw_stdout") if evidence.get("raw_stdout") is not None
                else evidence.get("stdout_summary") if evidence.get("stdout_summary") is not None
                else record.raw_stdout
            ),
            "stderr_summary": str(
                evidence.get("raw_stderr") if evidence.get("raw_stderr") is not None
                else evidence.get("stderr_summary") if evidence.get("stderr_summary") is not None
                else record.raw_stderr
            ),
        }
        return {
            "execution": normalized,
            "code": str(record.code or record.command),
            "stdout": normalized["stdout_summary"],
            "stderr": normalized["stderr_summary"],
            "returncode": int(record.exit_code),
            "evidence_source": "execution_evidence",
            "evidence_complete": True,
            "reconciliation_evidence": reconciliation,
            "migration_warnings": [],
        }

    last_observation = ""
    last_code = ""
    for message in reversed(list(state.get("messages") or [])):
        content = str(getattr(message, "content", message) or "")
        if not last_observation and "<observation>" in content:
            last_observation = content
        if not last_code and "<execute>" in content:
            match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
            if match:
                last_code = match.group(1).strip()
    return {
        "execution": evidence,
        "code": last_code,
        "stdout": extract_stream(last_observation, "STDOUT"),
        "stderr": extract_stream(last_observation, "STDERR"),
        "returncode": returncode_from_observation(last_observation),
        "evidence_source": "legacy_observation",
        "evidence_complete": False,
        "reconciliation_evidence": reconciliation,
        "migration_warnings": [
            "Legacy observation was parsed because no complete ExecutionEvidence was available."
        ],
    }


def structured_artifact_results(
    state: Dict[str, Any],
    *,
    output_dir: str,
    command_role: str,
    formal_execution: bool = False,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if command_role in {"probe", "diagnostic", "optional_probe", "capability_probe"} and not formal_execution:
        return [], []
    checks = verification_checks_from_state(state)
    if not checks:
        return [], []
    execution = state.get("execution_evidence") if isinstance(state, dict) else {}
    cwd = str((execution or {}).get("cwd") or output_dir)
    result = ArtifactValidator.validate(checks, cwd=cwd)
    rows: List[Dict[str, Any]] = []
    criteria: List[Dict[str, Any]] = []
    for group, satisfied in ((result.get("passed_checks", []), True), (result.get("failed_checks", []), False)):
        for item in group:
            check = dict(item.get("check") or {})
            required = bool(check.get("required", True))
            row = {
                "path": str(check.get("target") or ""),
                "check_type": str(check.get("check_type") or "file_exists"),
                "required": required,
                "satisfied": satisfied,
                "evidence": str(item.get("detail") or ""),
                "severity": "error" if required and not satisfied else ("warning" if not satisfied else "info"),
            }
            rows.append(row)
            criteria.append({
                "criterion_id": str(check.get("criterion_id") or check.get("id") or f"artifact-{len(criteria) + 1}"),
                "satisfied": satisfied,
                "evidence": row["evidence"],
                "severity": row["severity"],
            })
    return rows, criteria


def manifest_validation_results(execution: Dict[str, Any], command_role: str) -> List[Dict[str, Any]]:
    if command_role != "renderer_invocation":
        return []
    manifest_path = str(execution.get("manifest_path") or "").strip()
    row: Dict[str, Any] = {
        "manifest_path": manifest_path,
        "renderer_id": str(execution.get("renderer_id") or ""),
        "satisfied": False,
        "evidence": "",
    }
    if not manifest_path:
        row["evidence"] = "Renderer invocation did not declare a manifest path."
        return [row]
    path = Path(manifest_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        actual_renderer = str((value.get("renderer") or {}).get("id") or value.get("renderer_id") or "")
        expected_renderer = row["renderer_id"]
        if expected_renderer and actual_renderer and expected_renderer != actual_renderer:
            row["evidence"] = f"renderer mismatch: expected={expected_renderer} actual={actual_renderer}"
        else:
            row["satisfied"] = path.stat().st_size > 0
            row["evidence"] = f"manifest={path} renderer={actual_renderer or expected_renderer}"
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        row["evidence"] = f"manifest validation failed: {exc}"
    return [row]


def knowledge_conflict_check_results(state: Dict[str, Any]) -> List[CheckResult]:
    """Assess persisted conflict facts without routing or changing request state."""
    rows: List[CheckResult] = []
    conflicts = state.get("knowledge_conflicts") if isinstance(state, dict) else []
    conflicts = conflicts if isinstance(conflicts, list) else []
    blocking_ids = set()
    summary = state.get("knowledge_conflict_summary") if isinstance(state, dict) else {}
    if isinstance(summary, dict):
        blocking_ids.update(str(item) for item in summary.get("blocking_conflicts", []) if str(item).strip())
    for item in conflicts:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "detected")
        severity = str(item.get("severity") or "warning")
        if status in {"resolved", "accepted_exception", "superseded"}:
            continue
        if severity not in {"blocking", "critical"} and str(item.get("conflict_id")) not in blocking_ids:
            continue
        conflict_id = str(item.get("conflict_id") or "unknown")
        rows.append(
            CheckResult(
                "KnowledgeConflictCheck",
                False,
                "error",
                f"Unresolved knowledge conflict {conflict_id}: {str(item.get('description') or item.get('conflict_type') or 'unknown conflict')[:1200]}",
                "Run the required probe or create an auditable plan revision before PASS.",
            )
        )
    if blocking_ids and not rows:
        rows.append(
            CheckResult(
                "KnowledgeConflictCheck",
                False,
                "error",
                "The conflict summary contains unresolved blocking conflicts.",
                "Reload the conflict audit and resolve/revalidate it before PASS.",
            )
        )
    stale = state.get("plan_stale_steps") or state.get("plan_revalidation_steps") or []
    if stale:
        rows.append(
            CheckResult(
                "StalePlanCheck",
                False,
                "error",
                "Plan steps require revalidation after a knowledge-state change: " + ", ".join(str(item) for item in stale[:12]),
                "Do not execute stale steps; run a probe or revise the affected plan scope.",
            )
        )
    return rows
