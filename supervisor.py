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

from colorama import Fore, Style
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
        if returncode == 0 and not stdout.strip() and not stderr.strip():
            results.append(CheckResult(
                rule="EmptyOutputCheck",
                passed=False,
                severity="warning",
                message="Command succeeded but produced no output",
                suggestion="Add verbose flag (-v) or check output file directly.",
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

    async def review(self, code: str, stdout: str, stderr: str, task_context: str) -> str:
        prompt = f"""You are a senior bioinformatics scientist reviewing an agent's execution step.

[EXECUTED CODE]
{code[:1500]}

[STDOUT]
{stdout[:1000]}

[STDERR]
{stderr[:500]}

[TASK CONTEXT]
{task_context[:500]}

Evaluate:
1. Is the scientific approach correct for this bioinformatics task?
2. Are the parameters reasonable (threads, memory, genome version)?
3. Are there any red flags in the output (e.g., low mapping rate, 0 variants)?
4. Should this step be ACCEPTED, WARNED, or RETRIED?

Review rules:
- Do not guess a tool's CLI from memory when the executed code already shows its arguments.
- If a Python script exits non-zero with empty STDERR, inspect STDOUT first; many scripts print errors there.
- For `plot_methylation.py`, `--sample` intentionally takes five values:
  LABEL, CG_BW, CHG_BW, CHH_BW, COMP_BED. A dummy compartment BED path is allowed by the skill docs.

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

    async def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        messages = state.get("messages", [])
        task_id = state.get("task_id", "default")
        retry_count = state.get("retry_count", 0)
        current_plan = state.get("current_plan", "")

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
                if "❌ Failed" in content or "❌" in content:
                    returncode = 1
                # 提取 stdout / stderr
                if "STDOUT:" in content:
                    stdout = content.split("STDOUT:")[-1].split("</observation>")[0].strip()
                if "STDERR:" in content:
                    stderr = content.split("STDERR:")[-1].split("</observation>")[0].strip()
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

        errors   = [r for r in rule_results if not r.passed and r.severity == "error"]
        warnings = [r for r in rule_results if not r.passed and r.severity == "warning"]
        passes   = [r for r in rule_results if r.passed]

        # ── LLM 深度审查（仅在有问题时调用，节省 token）────
        llm_review_text = ""
        if (errors or warnings) and self.use_llm_review:
            task_context = f"Plan:\n{current_plan[:400]}\n\nLast observation:\n{last_obs[:400]}"
            llm_review_text = await self._llm_reviewer.review(
                last_code, stdout, stderr, task_context
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

        # 从 LLM 审查中覆盖判决（如果 LLM 说 RETRY/ESCALATE）
        if llm_review_text:
            if "RETRY" in llm_review_text.upper() and verdict == Verdict.WARN:
                verdict = Verdict.RETRY
            if "ESCALATE" in llm_review_text.upper():
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
            rule_results, verdict, llm_review_text, retry_count
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
        elif verdict == Verdict.RETRY:
            parts.append(
                f"❌ Supervisor: Execution failed. RETRY required "
                f"(attempt {retry_count + 1}/{3})."
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
