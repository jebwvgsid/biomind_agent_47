"""
supervisor.py  ―  Scientific Supervisor & QA Node for BioAnalyst Agent
========================================================================
功能：
  1. 验证结构化 ExecutionEvidence、manifest、产物和当前步骤标准。
  2. 输出幂等的 ValidationEvidence；LLM 仅提供科学 advisory。
  3. 将事实交给 RecoveryPolicy，由后者独占恢复和路由决策。

设计：Supervisor 是 execute 后的事实验证节点，不拥有状态迁移权。
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from colorama import Fore, Style
except ModuleNotFoundError:  # pragma: no cover - cosmetic fallback for minimal test envs
    class _NoColor:
        BLACK = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET_ALL = ""

    Fore = Style = _NoColor()
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from execution.executor import detect_script_language
from agent_core.recovery import (
    ArtifactValidator,
    ExecutionRecord,
    RecoveryFailureOwner,
    ValidationEvidence,
    ValidationStatus,
    failure_log_projection,
)
from agent_core.context_lineage import ainvoke_with_snapshot
from agent_core.task_spec import effective_task_contract, evaluate_requirements
from agent_core.plan_tracker import canonical_task_spec_for_state, load_plan_state
from agent_core.node_context import (
    InvocationRegistry,
    build_node_context,
    evaluate_node_staleness,
)

logger = logging.getLogger("BioMind.Supervisor")


def _effective_declared_requirements(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    task_spec = canonical_task_spec_for_state(state)
    if task_spec:
        return list(effective_task_contract(task_spec).get("requirements") or [])
    return list(state.get("requirements") or []) if isinstance(state.get("requirements"), list) else []


# ─────────────────────────────────────────────────────────────
# 1.  判决枚举
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# 2.  规则引擎（无需 LLM，快速静态检查）
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# 3.  LLM 科学审查（深度，按需调用）
# ─────────────────────────────────────────────────────────────

class LLMReviewer:
    def __init__(self, llm, *, data_path: str = ""):
        self.llm = llm
        self.data_path = str(data_path or "")
        self._invocations = InvocationRegistry(max_entries=128, max_stale_signatures=128)

    @staticmethod
    def _parse_decision(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        candidates = [fenced.group(1).strip()] if fenced else []
        candidates.append(raw)
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            candidates.append(brace.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            assessment = str(value.get("assessment") or "").upper()
            if assessment not in {"ACCEPTABLE", "CONCERN", "UNKNOWN"}:
                continue
            concerns = value.get("concerns") or []
            suggestions = value.get("suggestions") or []
            if not isinstance(concerns, list) or not isinstance(suggestions, list):
                continue
            confidence = value.get("confidence")
            if confidence is not None:
                try:
                    confidence = max(0.0, min(1.0, float(confidence)))
                except (TypeError, ValueError):
                    confidence = None
            return {
                "assessment": assessment,
                "concerns": [str(item)[:1000] for item in concerns[:20]],
                "suggestions": [str(item)[:1000] for item in suggestions[:20]],
                "confidence": confidence,
                "parse_status": "valid",
            }
        return {}

    async def review(
        self,
        code: str,
        stdout: str,
        stderr: str,
        task_context: str,
        returncode: int = 0,
        *,
        task_id: str = "",
        trace_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        plan_state = load_plan_state(self.data_path, task_id) if self.data_path and task_id else {}
        task_spec = (
            canonical_task_spec_for_state(plan_state)
            if plan_state
            else {"goal": str(task_context or "")}
        )
        metadata = dict(trace_metadata or {})
        reviewer_context = build_node_context(
            node_type="supervisor",
            task_spec=task_spec,
            plan_state=plan_state,
            state_generation=int(plan_state.get("state_generation") or 0),
            current_step_id=str(metadata.get("step_id") or ""),
            runtime={
                "return_code": int(returncode),
                "executed_action": str(code or "")[:4000],
                "scientific_review_context": str(task_context or "")[:2000],
            },
            evidence=[
                {
                    "evidence_id": "supervisor-stdout",
                    "kind": "stdout",
                    "excerpt": str(stdout or "")[:6000],
                    "truncated": len(str(stdout or "")) > 6000,
                },
                {
                    "evidence_id": "supervisor-stderr",
                    "kind": "stderr",
                    "excerpt": str(stderr or "")[-4000:],
                    "truncated": len(str(stderr or "")) > 4000,
                },
            ],
            allowed_operations=["scientific_advisory"],
            attempt_id=str(metadata.get("attempt_id") or ""),
            action_identity=str(metadata.get("action_id") or ""),
            evidence_identity=hashlib.sha256(
                (str(stdout or "") + "\n" + str(stderr or "")).encode("utf-8", errors="replace")
            ).hexdigest(),
            trace_enabled=bool(os.getenv("NODE_CONTEXT_V2_TRACE", "")),
        )
        payload = reviewer_context.llm_payload.to_json(indent=2)
        prompt = f"""You are a senior bioinformatics scientist reviewing an agent's execution step.

The Controller supplied a non-authoritative semantic payload. Provide advisory
scientific assessment only. Do not return state identity, approval, storage,
concurrency, routing, retry, blocking, plan mutation, or completion decisions.

<llm_payload>
{payload}
</llm_payload>

Evaluate only as a scientific advisory:
1. Is the scientific approach correct for this bioinformatics task?
2. Are the parameters reasonable (threads, memory, genome version)?
3. Are there any red flags in the output (e.g., low mapping rate, 0 variants)?
4. If this step generated or summarized artifacts such as reports, tables, status files,
   manifests, or metrics, are the conclusions consistent with the visible evidence?
5. Are there evidence-bound concerns the deterministic validators may not express?

Review rules:
- Do not choose routing, retry, escalation, blocking, plan repair, or task completion.
- Do not output PASS, WARN, RETRY, ESCALATE, BLOCKED, or next_step.
- For artifact/report steps, compare conclusions against any visible tables, status rows,
  metrics, logs, and file paths. If the required evidence is not visible, ask for a targeted
  inspection of the relevant artifact instead of accepting the conclusion.
- Do not use command success alone as evidence that a scientific or reporting conclusion is true.
- Do not infer missing inputs unless the exact error line proves the missing input.
- Every root-cause statement must cite visible runtime evidence. Script source text,
  a trap containing the word ERROR, and validator summaries are not runtime failures.
- Only stderr, a tool log, or a shell trace can directly confirm a failed inner command.
  stdout from a read-only diagnostic may describe a prior failure but does not prove
  that the diagnostic command failed.
- A directory, config file, or build log existing is only partial-artifact evidence.
  Accept a built database only when declared core files are non-empty, the final log
  has no fatal build result, and a minimal load check succeeds when the skill requires it.
- If evidence is incomplete, describe the missing evidence without selecting a probe or repair action.
- If the traceback/log is truncated, ask to inspect the relevant log tail or run a minimal availability check instead of guessing.
- Do not guess a tool's CLI from memory when the executed code already shows its arguments.
- Never promote a proposed input to required, modify the Plan, or choose a global route.
- If a Python script exits non-zero with empty STDERR, inspect STDOUT first; many scripts print errors there.
- If a failure points to a `.err`, `.log`, `.out`, or traceback file, inspect only the tail first (`tail -n 80` or targeted `grep ... | tail -n 40`); never request or paste an entire long log.
- For `plot_methylation.py`, `--sample` intentionally takes five values:
  LABEL, CG_BW, CHG_BW, CHH_BW, COMP_BED. A dummy compartment BED path is allowed by the skill docs.
- For CALDER2 official-r mode, a subprocess `No such file or directory` at `Popen`
  usually means the executable in the command, commonly `Rscript`, is missing; verify
  executable availability before speculating about FASTA or matrix paths.
- For gene-density feature tracks, do not accept a file based only on existence:
  require binned intervals at the selected resolution, matching chromosome names,
  column 4 sum > 0, and non-zero bins > 0.

Return exactly one JSON object and no prose:
{{
  "assessment": "ACCEPTABLE|CONCERN|UNKNOWN",
  "concerns": ["evidence-bound concern"],
  "suggestions": ["advisory suggestion without routing semantics"],
  "confidence": 0.0
}}
"""
        try:
            self._invocations.register(reviewer_context.binding)
            r, first_snapshot = await ainvoke_with_snapshot(
                self.llm,
                [HumanMessage(content=prompt)],
                data_path=self.data_path,
                task_id=task_id,
                metadata={
                    "node_name": "supervisor",
                    "call_kind": "supervisor_reviewer",
                    "controller_binding": reviewer_context.binding.to_dict(),
                    **metadata,
                },
            )
            self._invocations.consume(reviewer_context.binding.invocation_id)
            latest_plan_state = (
                load_plan_state(self.data_path, task_id)
                if self.data_path and task_id else plan_state
            )
            latest_task_spec = (
                canonical_task_spec_for_state(latest_plan_state)
                if latest_plan_state
                else task_spec
            )
            latest_binding = build_node_context(
                node_type="supervisor",
                task_spec=latest_task_spec,
                plan_state=latest_plan_state,
                state_generation=int(latest_plan_state.get("state_generation") or 0),
                current_step_id=str(metadata.get("step_id") or ""),
                attempt_id=str(metadata.get("attempt_id") or ""),
                action_identity=str(metadata.get("action_id") or ""),
                evidence_identity=reviewer_context.binding.evidence_identity,
            ).binding
            staleness = evaluate_node_staleness(
                reviewer_context.binding,
                latest_binding.to_dict(),
            )
            if not staleness.usable:
                self._invocations.mark_stale_signature_handled(
                    staleness.stale_signature
                )
                return json.dumps({
                    "assessment": "UNKNOWN",
                    "concerns": ["Supervisor advisory became stale and was ignored."],
                    "suggestions": [],
                    "confidence": None,
                    "parse_status": "stale_ignored",
                }, ensure_ascii=False, sort_keys=True)
            decision = self._parse_decision(r.content)
            if not decision:
                # A malformed advisory is non-authoritative.  Treat it as
                # unavailable instead of paying for an equivalent rerender.
                decision = {
                    "assessment": "UNKNOWN",
                    "concerns": ["Supervisor advisory failed schema validation and was ignored."],
                    "suggestions": [],
                    "confidence": None,
                    "parse_status": "invalid_ignored",
                }
            if not decision:
                decision = {
                    "assessment": "UNKNOWN",
                    "concerns": ["Supervisor advisory failed schema validation and was ignored."],
                    "suggestions": [],
                    "confidence": None,
                    "parse_status": "invalid_ignored",
                }
            return json.dumps(decision, ensure_ascii=False, sort_keys=True)
        except Exception as e:
            for context in (locals().get("reviewer_context"), locals().get("repair_context")):
                if context is None:
                    continue
                try:
                    self._invocations.terminate(context.binding.invocation_id, status="failed")
                except Exception:
                    pass
            return json.dumps({
                "assessment": "UNKNOWN",
                "concerns": [f"LLM advisory failed and was ignored: {type(e).__name__}: {e}"],
                "suggestions": [],
                "confidence": None,
                "parse_status": "request_failed_ignored",
            }, ensure_ascii=False, sort_keys=True)


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


# ─────────────────────────────────────────────────────────────
# 4.  思维链 / 任务流程记录器
# ─────────────────────────────────────────────────────────────

class ChainOfThoughtLogger:
    """将任务流程写成可重播的 Markdown 文件"""

    def __init__(self, output_dir: str, task_id: str):
        self.path = Path(output_dir) / f"cot_{task_id}.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._step = 0

        # 初始化文件头
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"# Task Chain-of-Thought: {task_id}\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n\n")
            f.write("---\n\n")

    def log_plan(self, plan: str):
        self._append(f"## 📋 Execution Plan\n\n```\n{plan}\n```\n")

    def log_step(
        self,
        node: str,
        code: str,
        language: str,
        stdout: str,
        stderr: str,
        returncode: int,
        verdict: str,
        llm_review: str = "",
    ):
        self._step += 1
        ts = datetime.now().strftime("%H:%M:%S")
        status_icon = "✅" if returncode == 0 else "❌"

        block = f"""## Step {self._step} [{ts}] – {node}  {status_icon}

**Language**: {language}  
**Verdict**: `{verdict}`

### Code
```{language.lower()}
{code[:2000]}
```

### Output
```
{stdout[:800]}
```
"""
        if stderr:
            block += f"\n### Stderr\n```\n{stderr[:400]}\n```\n"
        if llm_review:
            block += f"\n### Supervisor Review\n{llm_review}\n"

        block += "\n---\n\n"
        self._append(block)

    def log_summary(self, summary: str):
        self._append(f"## 🏁 Final Summary\n\n{summary}\n")

    def _append(self, text: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)


# ─────────────────────────────────────────────────────────────
# 5.  SupervisorNode  ―  LangGraph 节点函数
# ─────────────────────────────────────────────────────────────

class SupervisorNode:
    """
    在 LangGraph 中作为节点使用:

        workflow.add_node("supervisor", supervisor_node.run)
        workflow.add_edge("execute", "supervisor")
        workflow.add_conditional_edges("supervisor", supervisor_node.route,
            {"generate": "generate", "finish": END, "escalate": END})
    """

    def __init__(
        self,
        llm,
        output_dir: str = "./task_outputs",
        max_retries: int = 2,
        use_llm_review: bool = True,
    ):
        self.llm = llm
        self.output_dir = output_dir
        self.max_retries = max_retries
        self.use_llm_review = use_llm_review
        output_path = Path(output_dir).expanduser()
        trace_data_path = output_path.parent if output_path.name == "task_outputs" else output_path
        self._llm_reviewer = LLMReviewer(llm, data_path=str(trace_data_path))
        self._cot_loggers: Dict[str, ChainOfThoughtLogger] = {}

    def _get_cot(self, task_id: str) -> ChainOfThoughtLogger:
        if task_id not in self._cot_loggers:
            task_dir = os.path.join(self.output_dir, task_id)
            self._cot_loggers[task_id] = ChainOfThoughtLogger(task_dir, task_id)
        return self._cot_loggers[task_id]

    @staticmethod
    def _extract_stream(content: str, label: str) -> str:
        pattern = rf"{re.escape(label)}:[ \t]*\r?\n?(.*?)(?=\r?\nSTDOUT:|\r?\nSTDERR:|(?:\r?\n)?<validation_result|(?:\r?\n)?<runtime_status>|</observation>|\r?\n</observation>|$)"
        match = re.search(pattern, content, re.DOTALL)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _returncode_from_observation(content: str) -> int:
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

    @staticmethod
    def _non_progress_is_allowed(plan_progress: str, current_plan: str) -> bool:
        match = re.search(r"^Next step:\s*(.+)$", str(plan_progress or ""), re.MULTILINE | re.IGNORECASE)
        context = (match.group(1) if match else current_plan).lower()
        allowed_terms = (
            "preflight",
            "verify",
            "verification",
            "inspect",
            "check",
            "confirm",
            "list",
            "locate",
            "find input",
            "collect input",
            "healthcheck",
            "tool check",
        )
        return any(term in context for term in allowed_terms)

    @staticmethod
    def _validation_checks_from_observation(observation: str) -> List[CheckResult]:
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

    @staticmethod
    def _validation_level_from_observation(observation: str) -> str:
        match = re.search(
            r"<validation_result\b(?P<attrs>[^>]*)>",
            str(observation or ""),
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return ""
        level_match = re.search(r"level=['\"](?P<level>[^'\"]+)['\"]", match.group("attrs") or "", re.IGNORECASE)
        return level_match.group("level").lower() if level_match else ""

    @staticmethod
    def _validation_status_from_observation(observation: str) -> str:
        match = re.search(
            r"<validation_result\b(?P<attrs>[^>]*)>",
            str(observation or ""),
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return ""
        status_match = re.search(r"status=['\"](?P<status>[^'\"]+)['\"]", match.group("attrs") or "", re.IGNORECASE)
        return status_match.group("status").lower() if status_match else ""

    @staticmethod
    def _extract_review_verdict(llm_text: str) -> str:
        try:
            value = json.loads(str(llm_text or ""))
        except (TypeError, json.JSONDecodeError):
            return ""
        verdict = str(value.get("verdict_recommendation") or "").upper() if isinstance(value, dict) else ""
        return verdict if verdict in {"PASS", "WARN", "RETRY", "ESCALATE"} else ""

    @staticmethod
    def _review_decision(llm_text: str) -> Dict[str, Any]:
        try:
            value = json.loads(str(llm_text or ""))
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _verification_checks_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
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

    def _artifact_check_results(self, state: Dict[str, Any]) -> List[CheckResult]:
        checks = self._verification_checks_from_state(state)
        if not checks:
            return []
        evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
        cwd = str((evidence or {}).get("cwd") or self.output_dir)
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

    @staticmethod
    def _retry_contract(
        state: Dict[str, Any],
        rule_results: List[CheckResult],
        *,
        fallback_reason: str = "Supervisor requested retry.",
    ) -> Dict[str, Any]:
        evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
        evidence = evidence if isinstance(evidence, dict) else {}
        analysis = evidence.get("root_cause_analysis") or state.get("root_cause_analysis") or {}
        analysis = analysis if isinstance(analysis, dict) else {}
        failed_command = str(analysis.get("failed_command") or "").strip()
        failed_unresolved = bool(analysis.get("failed_command_unresolved")) or not failed_command
        source_records = analysis.get("evidence_records") or []
        direct_sources = {
            str(item.get("source") or "")
            for item in source_records
            if isinstance(item, dict) and bool(item.get("confirms_runtime_failure"))
        }
        evidence_sufficient = bool(failed_command and not failed_unresolved and direct_sources & {"stderr", "tool_log", "shell_trace"})
        failed_rows = [row for row in rule_results if not row.passed]
        retry_reason = " | ".join(str(row.message or "") for row in failed_rows[:4]).strip() or fallback_reason
        conflict_summary = state.get("knowledge_conflict_summary") or {}
        conflict_decisions = (
            conflict_summary.get("decisions", [])
            if isinstance(conflict_summary, dict)
            else []
        )
        explicit_plan_revision = any(
            isinstance(item, dict)
            and bool(item.get("requires_plan_revision"))
            and str(item.get("decision") or "") in {
                "local_replan", "stage_replan", "downstream_replan", "global_replan",
            }
            for item in conflict_decisions
        )
        plan_patch = (
            any(row.rule == "StalePlanCheck" for row in failed_rows)
            or explicit_plan_revision
        )
        if plan_patch:
            required_next_action = "PLAN_PATCH"
            recovery_scope = "PLAN"
            failure_owner = RecoveryFailureOwner.PLAN.value
        elif evidence_sufficient:
            required_next_action = "SCRIPT_PATCH"
            recovery_scope = "CURRENT_SCRIPT" if analysis.get("failed_script_path") else "CURRENT_STEP"
            failure_owner = RecoveryFailureOwner.SCRIPT.value
        else:
            required_next_action = "PROBE"
            recovery_scope = "CURRENT_STEP"
            failure_owner = RecoveryFailureOwner.DIAGNOSTIC.value
        fingerprint = str(analysis.get("inner_fingerprint") or "").strip()
        if not fingerprint:
            fingerprint = hashlib.sha256(
                f"{evidence.get('step_id')}|{retry_reason}".encode("utf-8", errors="replace")
            ).hexdigest()[:20]
        return {
            "retry_reason": retry_reason[:4000],
            "failed_step_id": str(evidence.get("step_id") or state.get("current_step_id") or ""),
            "failure_fingerprint": fingerprint,
            "required_next_action": required_next_action,
            "failure_owner": failure_owner,
            "recovery_scope": recovery_scope,
            "evidence_status": "sufficient" if evidence_sufficient else "insufficient",
            "pending_recovery_action": {
                "type": "run_probe" if required_next_action == "PROBE" else required_next_action.lower(),
                "reason": retry_reason[:4000],
                "failure_scope": recovery_scope,
                "failure_owner": failure_owner,
                "evidence_state": "SUFFICIENT" if evidence_sufficient else "INSUFFICIENT",
            },
            "recovery_phase": "probe" if required_next_action == "PROBE" else (
                "plan_repair" if required_next_action == "PLAN_PATCH" else "apply_repair"
            ),
        }

    @staticmethod
    def _root_cause_check_results(state: Dict[str, Any]) -> List[CheckResult]:
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

    @staticmethod
    def _legacy_compat_status(status: str) -> str:
        return {
            ValidationStatus.VALID.value: "PASS",
            ValidationStatus.VALID_WITH_WARNINGS.value: "WARN",
            ValidationStatus.INVALID.value: "FAIL",
            ValidationStatus.INCOMPLETE_EVIDENCE.value: "FAIL",
            ValidationStatus.VALIDATION_ERROR.value: "FAIL",
        }[str(status)]

    @staticmethod
    def _structured_execution_context(state: Dict[str, Any]) -> Dict[str, Any]:
        evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
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
                    evidence.get("stdout_summary")
                    if evidence.get("stdout_summary") is not None
                    else record.raw_stdout
                ),
                "stderr_summary": str(
                    evidence.get("stderr_summary")
                    if evidence.get("stderr_summary") is not None
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
            "stdout": SupervisorNode._extract_stream(last_observation, "STDOUT"),
            "stderr": SupervisorNode._extract_stream(last_observation, "STDERR"),
            "returncode": SupervisorNode._returncode_from_observation(last_observation),
            "evidence_source": "legacy_observation",
            "evidence_complete": False,
            "migration_warnings": [
                "Legacy observation was parsed because no complete ExecutionEvidence was available."
            ],
        }

    def _structured_artifact_results(
        self,
        state: Dict[str, Any],
        *,
        command_role: str,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if command_role in {"probe", "diagnostic", "optional_probe", "capability_probe"}:
            return [], []
        checks = self._verification_checks_from_state(state)
        if not checks:
            return [], []
        execution = state.get("execution_evidence") if isinstance(state, dict) else {}
        cwd = str((execution or {}).get("cwd") or self.output_dir)
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

    @staticmethod
    def _manifest_validation_results(execution: Dict[str, Any], command_role: str) -> List[Dict[str, Any]]:
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

    async def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Validate one execution attempt without choosing a state transition."""
        started = time.monotonic()
        context = self._structured_execution_context(state)
        execution = dict(context["execution"] or {})
        task_id = str(state.get("task_id") or "default")
        step_id = str(execution.get("step_id") or state.get("current_step_id") or "")
        action_id = str(execution.get("action_id") or execution.get("execution_id") or "")
        attempt_id = str(execution.get("attempt_id") or execution.get("execution_id") or "legacy")
        role = str(execution.get("command_role") or "analysis_execution").strip().lower()
        if role in {"primary", "analysis"}:
            role = "analysis_execution"
        elif role in {"diagnostic", "optional_probe"}:
            role = "probe"

        identity = ValidationEvidence.from_mapping({
            "task_id": task_id,
            "step_id": step_id,
            "action_id": action_id,
            "attempt_id": attempt_id,
            "command_role": role,
        })
        existing = state.get("validation_evidence")
        if isinstance(existing, dict) and existing:
            prior = ValidationEvidence.from_mapping(existing)
            if prior.validation_id == identity.validation_id:
                return {
                    "messages": [],
                    "validation_evidence": prior.to_dict(),
                    "supervisor_verdict": self._legacy_compat_status(prior.status),
                }

        try:
            returncode = int(context["returncode"])
            rule_results = RuleEngine.run(
                context["code"],
                context["stdout"],
                context["stderr"],
                returncode,
                command_role=role,
                expect_stdout=bool(execution.get("expect_stdout")),
                ai_claim_text=str(state.get("completion_claim") or ""),
            )
            rule_results.extend(self._root_cause_check_results(state))
            rule_results.extend(self._knowledge_conflict_check_results(state))
            if str(execution.get("execution_status") or "").lower() == "failed" and returncode == 0:
                rule_results.append(CheckResult(
                    "ExecutionStatusCheck",
                    False,
                    "error",
                    "ExecutionRecord reports failed although the projected return code is zero.",
                ))
            if bool(execution.get("timed_out")):
                rule_results.append(CheckResult("TimeoutCheck", False, "error", "Executor timeout was recorded."))
            if bool(execution.get("oom_detected")) or returncode in {137, -9}:
                rule_results.append(CheckResult("OOMCheck", False, "error", "Executor recorded an OOM or kill signal."))
            post_validation = execution.get("validation")
            if isinstance(post_validation, dict) and post_validation.get("passed") is False:
                rule_results.append(CheckResult(
                    "PostRunValidationCheck",
                    False,
                    "error",
                    str(post_validation.get("message") or post_validation.get("feedback") or "Post-run validation failed."),
                ))
            declared_requirements = _effective_declared_requirements(state)
            requirement_result = evaluate_requirements(declared_requirements)
            for requirement_id in requirement_result.get("missing_required", []) + requirement_result.get("missing_conditional", []):
                rule_results.append(CheckResult(
                    "RequirementContractCheck",
                    False,
                    "error",
                    f"Required contract input is missing: {requirement_id}",
                ))
            for requirement_id in requirement_result.get("unknown", []):
                rule_results.append(CheckResult(
                    "RequirementContractCheck",
                    False,
                    "error",
                    f"Requirement needs contract reconciliation: {requirement_id}",
                ))
            for requirement_id in requirement_result.get("missing_recommended", []):
                rule_results.append(CheckResult(
                    "RecommendedRequirementCheck",
                    False,
                    "warning",
                    f"Recommended input is unavailable: {requirement_id}",
                ))
            artifact_results, criteria_results = self._structured_artifact_results(
                state,
                command_role=role,
            )
            manifest_results = self._manifest_validation_results(execution, role)
            errors = [row.message for row in rule_results if not row.passed and row.severity == "error"]
            warnings = [row.message for row in rule_results if not row.passed and row.severity == "warning"]
            info = [row.message for row in rule_results if row.passed or row.severity == "info"]
            errors.extend(
                "Artifact verification failed: " + row["evidence"]
                for row in artifact_results
                if not row["satisfied"] and row["required"]
            )
            warnings.extend(row["evidence"] for row in artifact_results if not row["satisfied"] and not row["required"])
            errors.extend(row["evidence"] for row in manifest_results if not row["satisfied"])

            evidence_complete = bool(context["evidence_complete"])
            if not evidence_complete:
                status = ValidationStatus.INCOMPLETE_EVIDENCE.value
            elif errors:
                status = ValidationStatus.INVALID.value
            elif warnings:
                status = ValidationStatus.VALID_WITH_WARNINGS.value
            else:
                status = ValidationStatus.VALID.value

            advisory: Optional[Dict[str, Any]] = None
            advisory_conflict = False
            scientific_checks: List[Dict[str, Any]] = []
            validation_level = str(state.get("validation_level") or "").lower()
            if self.use_llm_review and role not in {"probe", "capability_probe"} and validation_level in {"scientific", "artifact"}:
                task_context = (
                    f"task_id={task_id}\nstep_id={step_id}\ncommand_role={role}\n"
                    f"deterministic_status={status}"
                )
                projected = failure_log_projection(
                    "\n".join([context["stderr"], context["stdout"]]),
                    log_path=str(execution.get("stderr_log") or execution.get("stdout_log") or ""),
                )
                review_text = await self._llm_reviewer.review(
                    context["code"],
                    "\n".join([*projected.get("matched_lines", []), str(projected.get("tail") or "")]),
                    str(context["stderr"])[-4000:],
                    task_context,
                    returncode,
                    task_id=task_id,
                    trace_metadata={
                        "step_id": step_id,
                        "action_id": str(execution.get("action_id") or ""),
                        "attempt_id": str(execution.get("attempt_id") or ""),
                        "action_contract_ref": str(execution.get("manifest_path") or ""),
                    },
                )
                try:
                    advisory = json.loads(review_text)
                except (TypeError, json.JSONDecodeError):
                    advisory = {
                        "assessment": "UNKNOWN",
                        "concerns": ["LLM advisory could not be parsed and was ignored."],
                        "suggestions": [],
                        "confidence": None,
                        "parse_status": "invalid_ignored",
                    }
                assessment = str((advisory or {}).get("assessment") or "UNKNOWN").upper()
                advisory_conflict = bool(
                    assessment == "CONCERN"
                    and status == ValidationStatus.VALID.value
                )
                scientific_checks.append({
                    "source": "llm_advisory",
                    "assessment": assessment,
                    "deterministic_status_unchanged": True,
                })

            evidence = ValidationEvidence.from_mapping({
                "task_id": task_id,
                "step_id": step_id,
                "action_id": action_id,
                "attempt_id": attempt_id,
                "status": status,
                "errors": list(dict.fromkeys(errors)),
                "warnings": list(dict.fromkeys(warnings)),
                "info": list(dict.fromkeys(info)),
                "returncode_check": {
                    "returncode": returncode,
                    "passed": returncode == 0,
                    "source": context["evidence_source"],
                    "process_signal": execution.get("process_signal"),
                    "timeout": bool(execution.get("timed_out")),
                    "oom": bool(execution.get("oom_detected")) or returncode in {137, -9},
                    "executor_exception": str(execution.get("executor_exception") or ""),
                },
                "manifest_results": manifest_results,
                "artifact_results": artifact_results,
                "success_criteria_results": criteria_results,
                "scientific_checks": scientific_checks,
                "recoverability_hints": (
                    ["probe"] if not evidence_complete else [
                        row.rule for row in rule_results if not row.passed
                    ]
                ),
                "llm_advisory": advisory,
                "llm_advisory_conflict": advisory_conflict,
                "evidence_complete": evidence_complete,
                "evidence_source": context["evidence_source"],
                "migration_source": "legacy_observation" if not evidence_complete else "",
                "migration_warnings": context["migration_warnings"],
                "command_role": role,
                "validation_duration": round(time.monotonic() - started, 6),
            })
        except Exception as exc:
            logger.exception("Supervisor validation failed for task=%s step=%s", task_id, step_id)
            evidence = ValidationEvidence.from_mapping({
                "task_id": task_id,
                "step_id": step_id,
                "action_id": action_id,
                "attempt_id": attempt_id,
                "status": ValidationStatus.VALIDATION_ERROR.value,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "recoverability_hints": ["validator_internal_error"],
                "evidence_complete": bool(context["evidence_complete"]),
                "evidence_source": context["evidence_source"],
                "command_role": role,
                "validation_duration": round(time.monotonic() - started, 6),
            })

        logger.info(
            "Supervisor validation: task=%s step=%s action=%s attempt=%s role=%s returncode=%s "
            "status=%s errors=%s warnings=%s artifacts=%s criteria=%s advisory=%s conflict=%s source=%s "
            "version=%s duration=%.3fs legacy_compat_only=true",
            evidence.task_id,
            evidence.step_id,
            evidence.action_id,
            evidence.attempt_id,
            evidence.command_role,
            evidence.returncode_check.get("returncode"),
            evidence.status,
            len(evidence.errors),
            len(evidence.warnings),
            len(evidence.artifact_results),
            len(evidence.success_criteria_results),
            bool(evidence.llm_advisory),
            evidence.llm_advisory_conflict,
            evidence.evidence_source,
            evidence.validator_version,
            evidence.validation_duration,
        )
        print(f"\n{Fore.CYAN}  Validation status: {evidence.status}{Style.RESET_ALL}\n")
        summary = {
            "validation_id": evidence.validation_id,
            "status": evidence.status,
            "errors": evidence.errors[:8],
            "warnings": evidence.warnings[:8],
        }
        return {
            "messages": [SystemMessage(content=(
                "<validation_evidence>\n"
                + json.dumps(summary, ensure_ascii=False, sort_keys=True)
                + "\n</validation_evidence>"
            ))],
            "validation_evidence": evidence.to_dict(),
            # Compatibility-only UI field. Routers and RecoveryPolicy must not
            # use it; INVALID maps to FAIL rather than recovery semantics.
            "supervisor_verdict": self._legacy_compat_status(evidence.status),
        }

    async def _legacy_run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        messages = state.get("messages", [])
        task_id = state.get("task_id", "default")
        retry_count = state.get("retry_count", 0)
        current_plan = state.get("current_plan", "")
        plan_progress = state.get("plan_progress", "")

        cot = self._get_cot(task_id)

        # ── 从最近消息中提取执行上下文 ──────────────────────
        last_obs = ""
        last_code = ""
        language = "Bash"
        returncode = 0
        stdout = ""
        stderr = ""

        evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
        has_structured_evidence = bool(
            isinstance(evidence, dict)
            and (
                evidence.get("execution_id")
                or evidence.get("returncode") is not None
                or evidence.get("stdout_log")
                or evidence.get("stderr_log")
            )
        )
        if isinstance(evidence, dict):
            evidence_command = str(evidence.get("generated_code") or evidence.get("command") or "").strip()
            if evidence_command:
                last_code = evidence_command
            stdout = str(evidence.get("stdout_summary") or evidence.get("stdout") or "")
            stderr = str(evidence.get("stderr_summary") or evidence.get("stderr") or "")
            projection = evidence.get("failure_log_projection") or {}
            if isinstance(projection, dict):
                matched = "\n".join(str(item) for item in projection.get("matched_lines", [])[-80:])
                tail = str(projection.get("tail") or "")
                if matched or tail:
                    stdout = "\n".join(part for part in (stdout, matched, tail[-8000:]) if part)
            if evidence.get("returncode") is not None:
                try:
                    returncode = int(evidence.get("returncode"))
                except (TypeError, ValueError):
                    pass
            language = str(evidence.get("language") or language)

        for m in reversed(messages):
            content = str(getattr(m, "content", m))
            if "<observation>" in content and not last_obs:
                last_obs = content
                # Observation text is a compatibility fallback. It may contain
                # validator summaries and literal stream labels, so it must
                # never overwrite persisted execution evidence.
                if not has_structured_evidence:
                    returncode = self._returncode_from_observation(content)
                    stdout = self._extract_stream(content, "STDOUT")
                    stderr = self._extract_stream(content, "STDERR")
            if "<execute>" in content and not last_code:
                match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
                if match:
                    last_code = match.group(1).strip()
                    language = detect_script_language(last_code)

        if not last_obs:
            contract = self._retry_contract(state, [], fallback_reason="Supervisor received no execution observation.")
            return {
                "next_step": "generate",
                "retry_count": retry_count + 1,
                "supervisor_verdict": Verdict.RETRY.value,
                **contract,
            }

        # ── 规则引擎检查 ─────────────────────────────────────
        print(f"\n{Fore.CYAN}🔬 [Supervisor] Analyzing execution result...{Style.RESET_ALL}")
        rule_results = RuleEngine.run(last_code, stdout, stderr, returncode)
        rule_results.extend(self._validation_checks_from_observation(last_obs))
        rule_results.extend(self._artifact_check_results(state))
        rule_results.extend(self._root_cause_check_results(state))
        rule_results.extend(self._knowledge_conflict_check_results(state))
        declared_requirements = _effective_declared_requirements(state)
        initial_contract = bound_supervisor_contract({}, declared_requirements)
        if initial_contract["missing_required_inputs"]:
            rule_results.append(CheckResult(
                rule="RequirementContractCheck",
                passed=False,
                severity="error",
                message="Required contract inputs are missing: " + ", ".join(initial_contract["missing_required_inputs"]),
                suggestion="Ask for or block on the declared required input; do not infer a replacement.",
            ))
        if initial_contract["unknown_requirements"]:
            rule_results.append(CheckResult(
                rule="RequirementContractCheck",
                passed=False,
                severity="error",
                message="Requirement levels need reconciliation: " + ", ".join(initial_contract["unknown_requirements"]),
                suggestion="Reconcile the existing contract without promoting an LLM proposal to required.",
            ))
        if initial_contract["missing_recommended"]:
            rule_results.append(CheckResult(
                rule="RecommendedRequirementCheck",
                passed=False,
                severity="warning",
                message="Recommended inputs are unavailable: " + ", ".join(initial_contract["missing_recommended"]),
                suggestion="Continue with a disclosed warning unless the declared contract says otherwise.",
            ))

        errors   = [r for r in rule_results if not r.passed and r.severity == "error"]
        warnings = [r for r in rule_results if not r.passed and r.severity == "warning"]
        warning_rules = {r.rule for r in warnings}
        validation_level = self._validation_level_from_observation(last_obs)
        validation_status = self._validation_status_from_observation(last_obs)
        if validation_status == "passed" and warning_rules <= {"EmptyOutputCheck"}:
            rule_results = [r for r in rule_results if r.rule != "EmptyOutputCheck"]
            warnings = []
            warning_rules = set()
        validated_silent_success = (
            returncode == 0
            and validation_status == "passed"
            and not stdout.strip()
            and not stderr.strip()
        )
        validated_artifact_inspection = (
            returncode == 0
            and validation_status == "passed"
            and "NonProgressCommandCheck" in warning_rules
            and RuleEngine.is_artifact_verification_command(last_code, stdout, stderr)
        )
        bounded_empty_attempt = any(
            result.rule == "EmptyArtifactStatusCheck" and result.passed
            for result in rule_results
        )
        needs_model_review = validation_level in {"artifact", "scientific"} and not validated_silent_success
        only_empty_output_warning = bool(warnings) and warning_rules <= {"EmptyOutputCheck"}

        # ── LLM 深度审查：问题步骤 + artifact/scientific 级别步骤 ────
        llm_review_text = ""
        if (errors or (warnings and not only_empty_output_warning) or needs_model_review) and self.use_llm_review:
            next_step_match = re.search(
                r"^Next step:\s*(.+)$",
                str(plan_progress or ""),
                re.MULTILINE | re.IGNORECASE,
            )
            current_step = (
                next_step_match.group(1).strip()
                if next_step_match
                else str(current_plan or "")[:800]
            )
            task_context = (
                f"Validation level: {validation_level or 'basic'}\n"
                f"Current step:\n{current_step[:800]}\n\nLast observation:\n{last_obs[:1200]}"
            )
            projected = failure_log_projection(
                "\n".join([stderr, stdout]),
                log_path=str((evidence or {}).get("stderr_log") or (evidence or {}).get("stdout_log") or ""),
            )
            review_stdout = "\n".join([
                *[str(item) for item in projected.get("matched_lines", [])],
                str(projected.get("tail") or ""),
            ])
            llm_review_text = await self._llm_reviewer.review(
                last_code,
                review_stdout,
                stderr[-4000:],
                task_context,
                returncode,
                task_id=task_id,
                trace_metadata={
                    "step_id": str((evidence or {}).get("step_id") or ""),
                    "action_id": str((evidence or {}).get("action_id") or ""),
                    "attempt_id": str((evidence or {}).get("attempt_id") or ""),
                    "action_contract_ref": str((evidence or {}).get("manifest_path") or ""),
                },
            )
            llm_review_text = self._enforce_review_hard_constraints(
                llm_review_text,
                hard_failed=bool(errors),
                returncode=returncode,
                hard_failure_reasons=[row.rule for row in errors],
            )

        # ── 判决逻辑 ─────────────────────────────────────────
        if not errors and not warnings:
            verdict = Verdict.PASS
        elif errors:
            if retry_count >= self.max_retries:
                verdict = Verdict.ESCALATE
            else:
                verdict = Verdict.RETRY
        else:
            verdict = Verdict.WARN

        if (
            verdict == Verdict.WARN
            and "NonProgressCommandCheck" in warning_rules
            and not (
                self._non_progress_is_allowed(plan_progress, current_plan)
                or str((evidence or {}).get("command_role") or "").lower() in {"diagnostic", "optional_probe"}
            )
            and not bounded_empty_attempt
            and not validated_artifact_inspection
        ):
            verdict = Verdict.ESCALATE if retry_count >= self.max_retries else Verdict.RETRY

        # 从 LLM 审查中覆盖判决（如果 LLM 说 RETRY/ESCALATE）
        if llm_review_text:
            review_verdict = self._extract_review_verdict(llm_review_text)
            if review_verdict == "WARN" and verdict == Verdict.PASS:
                verdict = Verdict.WARN
            if (
                review_verdict == "RETRY"
                and verdict in {Verdict.PASS, Verdict.WARN}
                and (bool(errors) or needs_model_review)
                and not only_empty_output_warning
            ):
                verdict = Verdict.RETRY
            if review_verdict == "ESCALATE":
                verdict = Verdict.ESCALATE

        review_decision = self._review_decision(llm_review_text)
        contract_bounds = bound_supervisor_contract(review_decision, declared_requirements)

        # ── 打印审查报告 ──────────────────────────────────────
        self._print_report(rule_results, verdict, llm_review_text, retry_count)

        # ── 写入思维链日志 ────────────────────────────────────
        cot.log_step(
            node="execute",
            code=last_code,
            language=language,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            verdict=verdict.value,
            llm_review=llm_review_text,
        )

        # ── 构造反馈消息注入对话 ─────────────────────────────
        feedback = self._build_feedback_message(
            rule_results,
            verdict,
            llm_review_text,
            retry_count,
            max_retries=self.max_retries,
            last_code=last_code,
            stdout=stdout,
            stderr=stderr,
        )
        feedback_msg = SystemMessage(content=feedback)

        # ── 决定下一步 ────────────────────────────────────────
        retry_already_recorded = bool(
            isinstance(evidence, dict) and evidence.get("retry_count_recorded")
        )
        new_retry = (
            retry_count
            if verdict == Verdict.RETRY and retry_already_recorded
            else (retry_count + 1 if verdict == Verdict.RETRY else 0)
        )
        next_step = {
            Verdict.PASS:     "generate",
            Verdict.WARN:     "generate",
            Verdict.RETRY:    "generate",
            Verdict.ESCALATE: "finish",
        }[verdict]

        recovery_update: Dict[str, Any] = {}
        prior_recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
        if prior_recovery_phase in {"rerun", "apply_repair", "probe", "verify", "rollback"}:
            if verdict in {Verdict.PASS, Verdict.WARN}:
                recovery_update = {
                    "recovery_phase": "success",
                    "recovery_status": "SUCCESS" if verdict == Verdict.PASS else "SUCCESS_WITH_WARNINGS",
                }
            elif verdict == Verdict.RETRY:
                recovery_update = {"recovery_phase": "verify", "recovery_status": "RETRY_REQUIRED"}
            elif verdict == Verdict.ESCALATE:
                recovery_update = {"recovery_phase": "exhausted", "recovery_status": "RECOVERY_EXHAUSTED"}
            try:
                from agent_core.recovery import RecoveryStateStore

                store = RecoveryStateStore(Path(self.output_dir).parent, str(task_id))
                store.save_status(
                    {
                        "phase": recovery_update.get("recovery_phase", prior_recovery_phase or "verify"),
                        "status": recovery_update.get("recovery_status", verdict.value),
                        "supervisor_verdict": verdict.value,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                store.append_trace("VERIFY", {"verdict": verdict.value, "rule_count": len(rule_results)})
            except Exception as exc:  # audit persistence must not break routing
                logger.warning("Could not persist recovery verification state: %s", exc)

        retry_contract = (
            self._retry_contract(state, rule_results)
            if verdict == Verdict.RETRY
            else {}
        )
        if retry_contract.get("required_next_action") == "PLAN_PATCH":
            next_step = "planner"
        if verdict == Verdict.RETRY and contract_bounds["contract_status"] != "valid":
            if contract_bounds["missing_required_inputs"]:
                retry_contract.update({
                    "failure_owner": RecoveryFailureOwner.INPUT_DATA.value,
                    "required_next_action": "ASK_USER",
                    "recovery_scope": "CURRENT_STEP",
                    "evidence_status": "sufficient",
                })
            else:
                retry_contract.update({
                    "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                    "required_next_action": "RECONCILE_ARTIFACTS",
                    "recovery_scope": "CURRENT_STEP",
                    "evidence_status": "insufficient",
                })
        return {
            "messages": [feedback_msg],
            "next_step": next_step,
            "retry_count": new_retry,
            "supervisor_verdict": verdict.value,
            "supervisor_feedback": {
                "verdict": verdict.value,
                "failed_checks": [
                    {"rule": row.rule, "severity": row.severity, "message": row.message}
                    for row in rule_results
                    if not row.passed
                ],
                "required_fix": [
                    row.suggestion
                    for row in rule_results
                    if not row.passed and str(row.suggestion or "").strip()
                ],
                "retryable": verdict in {Verdict.WARN, Verdict.RETRY},
            },
            "supervisor_review_decision": review_decision,
            "action_status": "completed" if verdict in {Verdict.PASS, Verdict.WARN} else "failed",
            "step_status": "in_progress",
            "contract_status": contract_bounds["contract_status"],
            "missing_required_inputs": contract_bounds["missing_required_inputs"],
            "skipped_optional_checks": contract_bounds["skipped_optional_checks"],
            "proposed_requirements": contract_bounds["proposed_requirements"],
            **recovery_update,
            **retry_contract,
        }

    @staticmethod
    def _knowledge_conflict_check_results(state: Dict[str, Any]) -> List[CheckResult]:
        """Turn persisted conflict state into a deterministic Supervisor gate."""
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

    async def final_audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Validate finalization evidence without deciding whether to finish."""
        audit_state = dict(state or {})
        execution = dict(audit_state.get("execution_evidence") or {})
        execution["command_role"] = "finalization"
        audit_state["execution_evidence"] = execution
        result = await self.run(audit_state)
        evidence = ValidationEvidence.from_mapping(result["validation_evidence"])
        checks = list(evidence.scientific_checks)
        checks.append({
            "source": "final_audit",
            "assessment": evidence.status,
            "deterministic_status_unchanged": True,
        })
        value = evidence.to_dict()
        value["scientific_checks"] = checks
        result["validation_evidence"] = ValidationEvidence.from_mapping(value).to_dict()
        result["final_audit"] = True
        return result

    async def _legacy_final_audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Audit a claimed final state without trusting the last LLM message.

        Main routing can request this path before finishing. It deliberately
        uses only explicit execution evidence and declared verification checks;
        an absent check is reported as WARN rather than silently treated as a
        passed analysis.
        """
        checks = self._verification_checks_from_state(state)
        evidence = state.get("execution_evidence") if isinstance(state, dict) else {}
        try:
            execution_returncode = int(evidence.get("returncode")) if isinstance(evidence, dict) and evidence.get("returncode") is not None else None
        except (TypeError, ValueError):
            execution_returncode = None
        has_execution = bool(
            isinstance(evidence, dict)
            and any(str(evidence.get(key) or "").strip() for key in ("command", "generated_code", "script_file"))
            and execution_returncode == 0
        )
        rows: List[CheckResult] = []
        if not has_execution:
            rows.append(
                CheckResult(
                    "FinalExecutionEvidence",
                    False,
                    "error",
                    "Final audit found no successful Execute evidence.",
                    "Route back to Generate/Execute or write a truthful blocked status.",
                )
            )
        if checks:
            rows.extend(self._artifact_check_results(state))
        else:
            rows.append(
                CheckResult(
                    "FinalArtifactContract",
                    False,
                    "warning",
                    "No explicit final verification checks were supplied.",
                    "Planner/skill should declare required final artifacts before allowing PASS.",
                )
            )
        rows.extend(self._root_cause_check_results(state))
        rows.extend(self._knowledge_conflict_check_results(state))
        progress = state.get("recovery_progress") if isinstance(state, dict) else {}
        if not isinstance(progress, dict):
            progress = {}
        progress_class = str(progress.get("classification") or "").lower()
        if progress_class in {"regression", "exhausted"}:
            rows.append(
                CheckResult(
                    "RecoveryProgressAudit",
                    False,
                    "error",
                    f"Recovery progress is {progress_class}; the claimed result cannot be finalized.",
                    "Route to a different repair/rollback strategy or write a truthful blocked status artifact.",
                )
            )
        elif progress_class in {"no_progress", "stalled"}:
            rows.append(
                CheckResult(
                    "RecoveryProgressAudit",
                    False,
                    "error",
                    f"Recovery progress is {progress_class}; no independent diagnostic change was recorded.",
                    "Do not treat repeated checks as completion; require a strategy change before finalizing.",
                )
            )
        errors = [row for row in rows if not row.passed and row.severity == "error"]
        warnings = [row for row in rows if not row.passed and row.severity == "warning"]
        if errors:
            verdict = Verdict.RETRY
            next_step = "generate"
        elif warnings:
            verdict = Verdict.WARN
            next_step = "finish"
        else:
            verdict = Verdict.PASS
            next_step = "finish"
        feedback = "\n".join(
            f"[{row.rule}] {row.message} {row.suggestion}" for row in rows if not row.passed
        ) or "Final audit passed all declared evidence checks."
        retry_contract = self._retry_contract(state, rows) if verdict == Verdict.RETRY else {}
        if retry_contract.get("required_next_action") == "PLAN_PATCH":
            next_step = "planner"
        return {
            "messages": [SystemMessage(content=f"<final_audit>\n{feedback}\n</final_audit>")],
            "next_step": next_step,
            "supervisor_verdict": verdict.value,
            "supervisor_feedback": {
                "verdict": verdict.value,
                "failed_checks": [
                    {"rule": row.rule, "severity": row.severity, "message": row.message}
                    for row in rows
                    if not row.passed
                ],
                "required_fix": [
                    row.suggestion
                    for row in rows
                    if not row.passed and str(row.suggestion or "").strip()
                ],
                "retryable": verdict in {Verdict.WARN, Verdict.RETRY},
            },
            "final_audit": True,
            "recovery_phase": "success" if verdict in {Verdict.PASS, Verdict.WARN} else "verify",
            "recovery_status": "SUCCESS" if verdict == Verdict.PASS else ("SUCCESS_WITH_WARNINGS" if verdict == Verdict.WARN else "RETRY_REQUIRED"),
            **retry_contract,
        }

    @staticmethod
    def _enforce_review_hard_constraints(
        llm_text: str,
        hard_failed: bool,
        returncode: int,
        hard_failure_reasons: Optional[List[str]] = None,
    ) -> str:
        """Prevent confusing LLM review text from declaring PASS on hard failures."""
        if not llm_text or not hard_failed:
            return llm_text
        try:
            decision = json.loads(llm_text)
        except (TypeError, json.JSONDecodeError):
            return llm_text
        if not isinstance(decision, dict):
            return llm_text
        if str(decision.get("verdict_recommendation") or "").upper() == "PASS":
            reason_text = ", ".join(hard_failure_reasons or []) or "unspecified hard check"
            decision["verdict_recommendation"] = "RETRY"
            decision["reason"] = (
                f"System contract override: hard checks failed ({reason_text}); return code={returncode}. "
                + str(decision.get("reason") or "")
            )[:2000]
            decision["parse_status"] = "valid_hard_constraint_override"
        return json.dumps(decision, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def route(state: Dict[str, Any]) -> str:
        """Deprecated compatibility hook; routing belongs to RecoveryPolicy."""
        return ""

    # ── 内部辅助 ─────────────────────────────────────────────

    def _print_report(
        self,
        results: List[CheckResult],
        verdict: Verdict,
        llm_text: str,
        retry_count: int,
    ):
        color = {
            Verdict.PASS:     Fore.GREEN,
            Verdict.WARN:     Fore.YELLOW,
            Verdict.RETRY:    Fore.RED,
            Verdict.ESCALATE: Fore.MAGENTA,
        }[verdict]

        print(f"\n{color}{'─'*55}")
        print(f"  🔬 SUPERVISOR VERDICT: {verdict.value}  (retry #{retry_count})")
        print(f"{'─'*55}{Style.RESET_ALL}")

        for r in results:
            icon = "✅" if r.passed else ("❌" if r.severity == "error" else "⚠️ ")
            print(f"  {icon} [{r.rule}] {r.message}")
            if not r.passed and r.suggestion:
                print(f"      💡 {r.suggestion}")

        if llm_text:
            print(f"\n  {Fore.CYAN}📝 LLM Review:{Style.RESET_ALL}")
            for line in llm_text.split("\n")[:6]:
                print(f"  {line}")

        print(f"{color}{'─'*55}{Style.RESET_ALL}\n")

    @staticmethod
    def _build_feedback_message(
        results: List[CheckResult],
        verdict: Verdict,
        llm_text: str,
        retry_count: int,
        max_retries: int = 2,
        last_code: str = "",
        stdout: str = "",
        stderr: str = "",
    ) -> str:
        errors   = [r for r in results if not r.passed and r.severity == "error"]
        warnings = [r for r in results if not r.passed and r.severity == "warning"]

        parts = [f"<supervisor_feedback verdict='{verdict.value}' retry='{retry_count}'>"]

        if verdict == Verdict.PASS:
            parts.append("✅ Supervisor: Execution validated. Proceed to next step.")
        elif verdict == Verdict.WARN:
            parts.append("⚠️ Supervisor: Execution passed with warnings. Proceed but note issues:")
            for w in warnings:
                parts.append(f"  - {w.message} | Fix: {w.suggestion}")
            if llm_text:
                parts.append(f"\nEXPERT DIAGNOSIS:\n{llm_text}")
        elif verdict == Verdict.RETRY:
            parts.append(
                f"❌ Supervisor: Execution failed. RETRY required "
                f"(attempt {retry_count + 1}/{max_retries})."
            )
            for e in errors:
                parts.append(f"  ERROR: {e.message}")
                if e.suggestion:
                    parts.append(f"  SUGGESTION: {e.suggestion}")
            if llm_text:
                parts.append(f"\nEXPERT DIAGNOSIS:\n{llm_text}")
            parts.append(
                "\nINSTRUCTION: Fix the above issue and retry. "
                "Do NOT repeat the same failing command."
            )
        elif verdict == Verdict.ESCALATE:
            parts.append(
                "🚨 Supervisor: MAX RETRIES EXCEEDED. "
                "Escalating to user. Task cannot continue automatically."
            )
            if last_code:
                parts.append(f"\nLAST COMMAND:\n{last_code[:1200]}")
            if stdout.strip():
                parts.append(f"\nSTDOUT SUMMARY:\n{stdout[:1200]}")
            if stderr.strip():
                parts.append(f"\nSTDERR SUMMARY:\n{stderr[:1200]}")
            if errors or warnings:
                parts.append("\nSTATIC CHECKS:")
                for item in errors + warnings:
                    parts.append(f"  - {item.rule}: {item.message}")
            if llm_text:
                parts.append(f"\nFINAL DIAGNOSIS:\n{llm_text}")

        parts.append("</supervisor_feedback>")
        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# 6.  便利函数：记录计划到思维链
# ─────────────────────────────────────────────────────────────

def log_plan_to_cot(supervisor: SupervisorNode, task_id: str, plan: str):
    """在 plan_review 节点后调用，将计划写入 CoT 文件"""
    cot = supervisor._get_cot(task_id)
    cot.log_plan(plan)
