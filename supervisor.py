"""
supervisor.py  ―  Scientific Supervisor & QA Node for BioAnalyst Agent
========================================================================
功能：
  1. 在每次执行后自动审查结果的科学性（输出格式、数据完整性、工具参数合理性）
  2. 检测幻觉（AI 声称成功但无实际输出）
  3. 输出结构化审查报告，并决策：PASS / WARN / RETRY / ESCALATE
  4. 生成可重播的思维链（Chain-of-Thought trace）写入 .md 文件

设计：Supervisor 是一个独立节点，插入在 execute → generate 之间。
"""

from __future__ import annotations

import json
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

logger = logging.getLogger("BioMind.Supervisor")


# ─────────────────────────────────────────────────────────────
# 1.  判决枚举
# ─────────────────────────────────────────────────────────────

class Verdict(str, Enum):
    PASS      = "PASS"       # 结果正确，继续下一步
    WARN      = "WARN"       # 有小问题但可接受，附警告继续
    RETRY     = "RETRY"      # 结果有错，让 generate 重试（最多 max_retries 次）
    ESCALATE  = "ESCALATE"   # 多次重试仍失败，上报给用户决策


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
        return all(
            any(re.search(pattern, line, re.IGNORECASE) for pattern in cls.NON_PROGRESS_PATTERNS)
            for line in lines
        )

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
    def run(cls, code: str, stdout: str, stderr: str, returncode: int) -> List[CheckResult]:
        results: List[CheckResult] = []
        combined = (stdout + "\n" + stderr).lower()
        code_lower = code.lower()

        # ── Rule 1: 幻觉检测 ─────────────────────────────────
        # 只检查 AI 生成内容（不检查命令输出）
        ai_text = code_lower  # code 在 executor 前是 AI 的最后消息
        for pattern in cls.HALLUCINATION_PHRASES:
            if re.search(pattern, ai_text, re.IGNORECASE):
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
            if re.search(pattern, combined, re.IGNORECASE):
                results.append(CheckResult(
                    rule="SilentFailureDetector",
                    passed=False,
                    severity="warning",
                    message=f"Silent failure signal detected: {hint}",
                    suggestion=hint,
                ))

        # ── Rule 4: 空输出检测 ────────────────────────────────
        if returncode == 0 and not stdout.strip() and not stderr.strip() and not cls.is_expected_silent_command(code):
            results.append(CheckResult(
                rule="EmptyOutputCheck",
                passed=False,
                severity="warning",
                message="Command succeeded but produced no output",
                suggestion="Add verbose flag (-v) or check output file directly.",
            ))

        # ── Rule 4b: 非进展命令检测 ───────────────────────────
        if returncode == 0 and cls.is_non_progress_command(code):
            results.append(CheckResult(
                rule="NonProgressCommandCheck",
                passed=False,
                severity="warning",
                message="Command only inspected/help-listed resources and did not run an analysis step",
                suggestion="Use this only for explicit preflight/checklist steps; otherwise run the planned analysis command.",
            ))

        # ── Rule 5: 工具特异性科学验证 ───────────────────────
        for tool, expected_pattern in cls.BIO_POSITIVE_CHECKS.items():
            if tool in code_lower:
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
    def __init__(self, llm):
        self.llm = llm

    async def review(self, code: str, stdout: str, stderr: str, task_context: str, returncode: int = 0) -> str:
        prompt = f"""You are a senior bioinformatics scientist reviewing an agent's execution step.

[RETURN CODE]
{returncode}

[EXECUTED CODE]
{code[:1500]}

[STDOUT]
{stdout[:3000]}

[STDERR]
{stderr[:1200]}

[TASK CONTEXT]
{task_context[:1500]}

Evaluate:
1. Is the scientific approach correct for this bioinformatics task?
2. Are the parameters reasonable (threads, memory, genome version)?
3. Are there any red flags in the output (e.g., low mapping rate, 0 variants)?
4. If this step generated or summarized artifacts such as reports, tables, status files,
   manifests, or metrics, are the conclusions consistent with the visible evidence?
5. Should this step be ACCEPTED, WARNED, or RETRIED?

Review rules:
- If RETURN CODE is non-zero, the verdict must be RETRY or ESCALATE. Never output PASS.
- If post-run validation failed in the observation, the verdict must be RETRY or ESCALATE.
- For artifact/report steps, compare conclusions against any visible tables, status rows,
  metrics, logs, and file paths. If the required evidence is not visible, ask for a targeted
  inspection of the relevant artifact instead of accepting the conclusion.
- Do not use command success alone as evidence that a scientific or reporting conclusion is true.
- Do not infer missing inputs unless the exact error line proves the missing input.
- If the traceback/log is truncated, ask to inspect the relevant log tail or run a minimal availability check instead of guessing.
- Do not guess a tool's CLI from memory when the executed code already shows its arguments.
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

Be concise. Format:
**Verdict**: [PASS | WARN | RETRY]
**Reason**: [1-2 sentences]
**Action**: [What to do next if not PASS]
"""
        try:
            r = await self.llm.ainvoke([HumanMessage(content=prompt)])
            return r.content.strip()
        except Exception as e:
            return f"LLM review failed: {e}"


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
        max_retries: int = 3,
        use_llm_review: bool = True,
    ):
        self.llm = llm
        self.output_dir = output_dir
        self.max_retries = max_retries
        self.use_llm_review = use_llm_review
        self._llm_reviewer = LLMReviewer(llm)
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
        match = re.search(
            r"(?:\*\*)?\s*Verdict\s*(?:\*\*)?\s*:\s*(PASS|WARN|RETRY|ESCALATE)\b",
            str(llm_text or ""),
            re.IGNORECASE,
        )
        return match.group(1).upper() if match else ""

    async def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
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

        for m in reversed(messages):
            content = str(getattr(m, "content", m))
            if "<observation>" in content and not last_obs:
                last_obs = content
                # 解析退出状态
                returncode = self._returncode_from_observation(content)
                # 提取 stdout / stderr
                stdout = self._extract_stream(content, "STDOUT")
                stderr = self._extract_stream(content, "STDERR")
            if "<execute>" in content and not last_code:
                match = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
                if match:
                    last_code = match.group(1).strip()
                    language = detect_script_language(last_code)

        if not last_obs:
            # 没有观察结果，直接放行
            return {"next_step": "generate", "retry_count": retry_count}

        # ── 规则引擎检查 ─────────────────────────────────────
        print(f"\n{Fore.CYAN}🔬 [Supervisor] Analyzing execution result...{Style.RESET_ALL}")
        rule_results = RuleEngine.run(last_code, stdout, stderr, returncode)
        rule_results.extend(self._validation_checks_from_observation(last_obs))

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
        needs_model_review = validation_level in {"artifact", "scientific"} and not validated_silent_success
        only_empty_output_warning = bool(warnings) and warning_rules <= {"EmptyOutputCheck"}

        # ── LLM 深度审查：问题步骤 + artifact/scientific 级别步骤 ────
        llm_review_text = ""
        if (errors or (warnings and not only_empty_output_warning) or needs_model_review) and self.use_llm_review:
            task_context = (
                f"Validation level: {validation_level or 'basic'}\n"
                f"Plan:\n{current_plan[:800]}\n\nLast observation:\n{last_obs[:1200]}"
            )
            llm_review_text = await self._llm_reviewer.review(
                last_code, stdout, stderr, task_context, returncode
            )
            llm_review_text = self._enforce_review_hard_constraints(
                llm_review_text,
                hard_failed=bool(errors),
                returncode=returncode,
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
            and not self._non_progress_is_allowed(plan_progress, current_plan)
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
        new_retry = retry_count + 1 if verdict == Verdict.RETRY else 0
        next_step = {
            Verdict.PASS:     "generate",
            Verdict.WARN:     "generate",
            Verdict.RETRY:    "generate",
            Verdict.ESCALATE: "finish",
        }[verdict]

        return {
            "messages": [feedback_msg],
            "next_step": next_step,
            "retry_count": new_retry,
            "supervisor_verdict": verdict.value,
        }

    @staticmethod
    def _enforce_review_hard_constraints(llm_text: str, hard_failed: bool, returncode: int) -> str:
        """Prevent confusing LLM review text from declaring PASS on hard failures."""
        if not llm_text or not hard_failed:
            return llm_text
        if re.search(r"\*\*Verdict\*\*\s*:\s*PASS\b", llm_text, re.IGNORECASE):
            llm_text = re.sub(
                r"\*\*Verdict\*\*\s*:\s*PASS\b",
                "**Verdict**: RETRY",
                llm_text,
                count=1,
                flags=re.IGNORECASE,
            )
            llm_text += (
                f"\n\nSystem override: hard execution/validation failure detected "
                f"(return code {returncode}); PASS is not allowed."
            )
        return llm_text

    @staticmethod
    def route(state: Dict[str, Any]) -> str:
        return state.get("next_step", "generate")

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
        max_retries: int = 3,
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
