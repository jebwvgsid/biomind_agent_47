import re
import functools
import logging
from typing import Tuple, Dict, Any, List
from langgraph.types import interrupt
from langchain_core.messages import HumanMessage
from execution.confirmation import RiskAnalyzer, RiskLevel
from execution.confirmation import RiskAnalyzer, RiskLevel
from execution.monitoring import LogCleaner
from execution.profiling import get_current_node, get_current_profiler, get_current_task_id
from config.config import KB_MAP
from colorama import Fore, Style
from agent_core.llm_safety import clip_text, llm_failure_summary

logger = logging.getLogger("BioMind.Middleware")

LOG_TAIL_POLICY = (
    "When an error points to a .err, .log, .out, or trace file, inspect only a "
    "small tail first, for example `tail -n 80 path/to/file.log`. For very large "
    "logs, combine targeted grep with a tail, for example "
    "`grep -iE 'error|exception|fatal|traceback|killed' path/to/file.log | tail -n 40`. "
    "Never `cat` or paste an entire long log into the prompt."
)

class HITLMiddleware:
    def __init__(self, auto_approve_safe: bool = True):
        # 这个开关控制是否自动放行安全操作
        self.auto_approve_safe = auto_approve_safe

    def review_code(self, code: str, language: str) -> Tuple[str, str]:
        """
        代码审查核心逻辑：
        1. 先进行风险分析（使用您提供的稳健逻辑）
        2. 如果是安全操作 -> 直接返回 approve (自动放行)
        3. 如果是风险操作 -> 触发 interrupt (支持审批/修改/拒绝)
        """
        
        # --- A. 移植您的动态风险分析逻辑 (包含异常处理) ---
        try:
            risk_analysis = RiskAnalyzer.analyze(code, language)
        except Exception as e:
            logger.warning(f"RiskAnalyzer failed: {e}")
            # Fallback default (保底机制)
            risk_analysis = {
                "risk_level": RiskLevel.MEDIUM,
                "risk_score": 50,
                "risky_operations": ["Analysis Failed"],
                "safe_operations": [],
                "recommendations": ["Manual Review Required"]
            }

        # --- B. 自动放行判断 (保留原有优势) ---
        # 如果配置了自动放行，且风险等级为 SAFE，直接通过，不打扰用户
        if self.auto_approve_safe and risk_analysis['risk_level'] == RiskLevel.SAFE:
            logger.info(f"✅ Auto-approved safe code: {code[:30]}...")
            profiler = get_current_profiler()
            task_id = get_current_task_id()
            if profiler and task_id:
                profiler.record_confirmation(
                    task_id=task_id,
                    prompt_type="code_review",
                    response="auto-approved",
                    duration_seconds=0.0,
                    node=get_current_node(),
                    payload={
                        "content": code,
                        "risk": {
                            "risk_level": risk_analysis["risk_level"].value,
                            "risk_score": risk_analysis["risk_score"],
                        },
                    },
                    automatic=True,
                )
            return "approve", code

        # --- C. 构造中断请求 (仅针对非 Safe 操作) ---
        # 构造 risk_info 用于前端展示
        risk_info = {
            "risk_level": risk_analysis["risk_level"].value,
            "risk_score": risk_analysis["risk_score"],
            "risky_operations": risk_analysis["risky_operations"],
            "safe_operations": risk_analysis["safe_operations"],
            "recommendations": risk_analysis["recommendations"]
        }

        payload = {
            "type": "code_review",
            "msg": f"⚠️ Code Execution Request ({language}) | y/Enter=run, n=reject, q=quit, e:/edit:/code:=replacement; text=feedback, not code",
            "content": code,   # 原始代码
            "language": language,
            "risk": risk_info, # 传入风险详情
            "options": ["(y)es", "(n)o", "(e)dit"],
            "instructions": "Plain text is feedback, not executable code. Replacement code must start with e:, edit:, or code:."
        }

        # --- D. 触发中断 ---
        user_response = interrupt(payload)

        # --- E. 解析用户反馈 (支持编辑) ---
        return self._parse_code_response(user_response, original_code=code)

    def _parse_code_response(self, response: Any, original_code: str) -> Tuple[str, str]:
        """解析用户输入：支持带理由的拒绝"""
        
        # 1. 处理结构化反馈 (来自交互界面的高级输入)
        if isinstance(response, dict):
            if response.get("action") == "reject":
                return "reject", response.get("reason", "No reason provided.")
            if response.get("action") == "paste":
                return "approve", response.get("code")

        resp_str = str(response).strip()
        
        # 2. 批准
        if resp_str.lower() in ['y', 'yes', 'approve', 'ok', '']:
            return "approve", original_code
        
        # 3. 拒绝 (普通拒绝，无理由)
        elif resp_str.lower() in ['n', 'no', 'reject']:
            return "reject", "User rejected explicitly."  # <--- 给一个明确的默认理由
            
        # 4. === [新增] 支持 "n: 理由" 这种快捷输入 ===
        elif resp_str.lower().startswith("n:") or resp_str.lower().startswith("no:"):
            # 提取冒号后面的内容作为理由
            reason = resp_str.split(":", 1)[1].strip()
            return "reject", reason
            
        # 5. 退出
        elif resp_str.lower() in ['q', 'quit']:
            return "quit", ""

        # 6. Explicit edit only. Plain text is feedback, not executable code.
        elif re.match(r'^(edit:|code:|e:)\s*', resp_str, flags=re.IGNORECASE):
            edited_code = re.sub(r'^(edit:|code:|e:)\s*', '', resp_str, flags=re.IGNORECASE).strip()
            if edited_code:
                return "edit", edited_code
            return "reject", "Edit prefix was provided without replacement code."

        # 7. Other text is usually a parameter answer or guidance typed into the
        # review prompt by mistake. Do not run it as shell code.
        else:
            return "reject", (
                "User provided feedback at code review instead of approval: "
                f"{resp_str}. Regenerate the next command using this feedback if relevant."
            )


def _recovery_tier(retry_count: int) -> int:
    if retry_count <= 0:
        return 0
    if retry_count == 1:
        return 1
    return 2


def classify_execution_failure(text: str) -> Tuple[str, str]:
    lower = str(text or "").lower()
    if "biomind shim error" in lower or "stale" in lower and "shim" in lower:
        return "stale_shim", "Re-register the tool to the real executable/env; do not keep retrying the stale shim."
    if re.search(r"rscript:\s*command not found|which:\s*no rscript|command not found", lower, re.IGNORECASE):
        return "missing_executable", "Verify the executable with `which`/`--version`; install r-base before any R package install command."
    if re.search(r"there is no package called|calder2?\s+not available|modulenotfounderror|importerror", lower, re.IGNORECASE):
        return "missing_package", "Install/register the missing package only after its language runtime is available, then rerun the package load check."
    if re.search(r"gene_density(?:\.bed)?[^\n]*(?:sum|总和)[^\n]*(?:0|为 0)|no non-zero bins|chromosome names do not match", lower, re.IGNORECASE):
        return "semantic_validation_failed", "Regenerate the scientific input artifact and pass semantic validation before retrying downstream tools."
    if re.search(r"no such file or directory|file not found|not found:", lower, re.IGNORECASE):
        return "missing_input_file", "Verify the exact path named in the error before changing unrelated inputs."
    return "unknown_truncated_log", "Inspect the smallest relevant log tail or run a minimal deterministic availability check."


def _selected_skill_ids(state: Dict[str, Any]) -> List[str]:
    if not isinstance(state, dict):
        return []
    selected = state.get("active_skills") or state.get("selected_skills") or []
    return [str(item) for item in selected if str(item).strip()]


def _skill_error_context(skill_manager, state: Dict[str, Any], max_chars: int = 3000) -> str:
    if not skill_manager:
        return ""
    selected = _selected_skill_ids(state)
    if not selected:
        return ""

    chunks: List[str] = []
    try:
        cards = skill_manager.get_execution_cards(selected)
        if cards:
            chunks.append("### Active skill execution cards\n" + clip_text(cards, 1400))
    except Exception as exc:  # noqa: BLE001 - recovery context should be best-effort.
        chunks.append(f"### Active skill context unavailable\n{exc}")

    ref_keywords = ("error", "troubleshoot", "trouble", "faq", "preflight", "output_schema", "usage")
    loaded_refs = 0
    for sid in selected[:3]:
        try:
            refs = skill_manager.get_skill_reference_index(sid)
        except Exception:
            refs = []
        prioritized = [
            ref for ref in refs
            if any(keyword in str(ref).lower() for keyword in ref_keywords)
        ]
        for ref_path in prioritized[:2]:
            if loaded_refs >= 3:
                break
            try:
                body = skill_manager.load_reference(sid, ref_path)
            except Exception as exc:  # noqa: BLE001
                body = f"Unable to load reference: {exc}"
            chunks.append(f"### Skill reference: {sid} / {ref_path}\n{clip_text(body, 900)}")
            loaded_refs += 1

    if not chunks:
        return ""
    return "\n\n<active_skill_error_context>\n" + clip_text("\n\n".join(chunks), max_chars) + "\n</active_skill_error_context>"


def error_recovery_middleware(llm, retriever, skill_manager=None):
    """
    错误恢复中间件装饰器工厂
    :param llm: 用于生成修复方案的 LLM 实例
    :param retriever: 用于检索 RAG 的检索器实例
    :param skill_manager: 可选，用于在二次失败后加载 active skill 的错误/运行上下文
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(state, *args, **kwargs):
            # 1. 先执行原始的 execute 逻辑
            result = await func(state, *args, **kwargs)
            
            # 获取最新的 execute 结果消息
            last_msg = result["messages"][-1].content
            
            # 2. 检查是否包含“❌ Failed”标记 (这是我们在 execute 节点里定义的失败标记)
            if "❌ Failed" in last_msg:
                if "guard blocked execution" in last_msg.lower():
                    return result
                retry_count = 0
                if isinstance(state, dict):
                    try:
                        retry_count = int(state.get("retry_count", 0) or 0)
                    except (TypeError, ValueError):
                        retry_count = 0
                tier = _recovery_tier(retry_count)
                print(f"\n{Fore.RED}🚑 [Middleware] Detected Execution Failure. Starting Auto-Diagnosis...{Style.RESET_ALL}")
                
                # --- A. 提取错误指纹 ---
                # 假设 last_msg 包含了 <observation>...STDERR:...</observation>
                # 我们提取 STDERR 部分
                stdout_content = ""
                if "STDOUT:" in last_msg:
                    stdout_content = last_msg.split("STDOUT:")[-1].split("STDERR:")[0]
                stderr_content = last_msg.split("STDERR:")[-1].split("</observation>")[0]
                error_text = (stderr_content.strip() or stdout_content.strip())
                error_fingerprint = LogCleaner.extract_error_fingerprint(error_text)
                failure_class, failure_action = classify_execution_failure(error_text)
                print(f"{Fore.RED}🔍 Error Fingerprint: {error_fingerprint[:100]}...{Style.RESET_ALL}")
                env_hint = ""
                env_missing = re.search(r"EnvironmentLocationNotFound:.*?/envs/([A-Za-z0-9_.-]+)", error_text)
                if env_missing:
                    env_name = env_missing.group(1)
                    env_hint = (
                        f"\n\nDetected missing conda environment `{env_name}`. "
                        f"The next action should be an install delegation tag, for example: "
                        f"<install_tool env=\"{env_name}\">skill_python_deps</install_tool>. "
                        f"Do not emit a direct pip/conda install command in <execute>."
                    )
                runtime_status = {}
                if isinstance(result, dict):
                    runtime_status = result.get("runtime_status") or {}
                if not runtime_status and isinstance(state, dict):
                    runtime_status = state.get("runtime_status") or {}
                if "hicFindTADs not found on PATH" in error_text and isinstance(runtime_status, dict):
                    passed_records = [
                        record for record in runtime_status.values()
                        if isinstance(record, dict)
                        and record.get("ok")
                        and (
                            record.get("env") == "hicexplore"
                            or "hicFindTADs" in str(record.get("healthcheck", ""))
                        )
                    ]
                    if passed_records:
                        record = passed_records[-1]
                        runner = record.get("env_runner") or f"conda run -n {record.get('env', 'hicexplore')} python"
                        env_hint = (
                            "\n\nRuntime diagnosis: the selected skill runtime healthcheck already passed, "
                            "so this is not an installation failure. The command likely ran outside the "
                            f"`{record.get('env', 'hicexplore')}` environment. Regenerate the same skill command "
                            f"with the runner prefix `{runner}`. Do not output `<install_tool>` unless a new "
                            "healthcheck fails."
                        )

                # --- B. 分层检索：第一次只本地修复，后续才扩展上下文 ---
                rag_advice = ""
                skill_advice = ""
                if tier >= 1:
                    skill_advice = _skill_error_context(skill_manager, state if isinstance(state, dict) else {})
                if tier >= 1 and retriever and KB_MAP.get("ERRORS"):
                    try:
                        print(f"{Fore.YELLOW}📚 Querying RAG for solution...{Style.RESET_ALL}")
                        docs = retriever.retrieve(query=error_fingerprint, target_datasets=[KB_MAP["ERRORS"]])
                        if docs:
                            rag_advice = f"\n\n### 🧠 RAG Knowledge Base Advice:\n{docs}"
                            print(f"{Fore.GREEN}✓ Found relevant solution in KB.{Style.RESET_ALL}")
                    except Exception as e:
                        print(f"RAG Error: {e}")
                elif tier == 0:
                    print(f"{Fore.YELLOW}↪ Recovery tier 0: local stdout/stderr diagnosis only; skipping RAG for this first retry.{Style.RESET_ALL}")

                external_hint = ""
                if tier >= 2:
                    external_hint = (
                        "\n\nExternal search policy: local fix and RAG/skill context have already been tried. "
                        "If the next fix still depends on undocumented behavior and an approved web search "
                        "capability is available, search the exact error fingerprint plus tool/version, prefer "
                        "official docs and GitHub issues, and summarize only the actionable finding. If no "
                        "external search capability is available, stop escalating guesses and ask the user."
                    )
                repeated_class_hint = ""
                if tier >= 1 and failure_class != "unknown_truncated_log":
                    repeated_class_hint = (
                        f"\n\nRepeated failure class: `{failure_class}`. Stop broad guessing. "
                        f"Use this single next action: {failure_action}"
                    )

                # --- C. LLM 诊断 (开处方) ---
                # 我们不直接修改代码，而是生成一个“专家会诊意见”，注入到对话历史中
                # 让下一个 Generate 节点基于这个意见去重写代码
                diagnosis_prompt = f"""
You are a recovery engineer diagnosing exactly one failed execution. Your goal is
to identify the proven root cause before proposing any retry. Do not continue the
workflow blindly.

Hard rules:
1. If the observed exit code or validation status is failed, the step failed. Never call it PASS.
2. Do not infer missing inputs unless the error evidence proves that exact missing input.
3. First identify the failing command, failing line, and primary error class.
4. If the traceback or tool log is truncated, the next action must inspect the relevant tail of the log or run a minimal availability check; do not guess.
5. Do not proceed to downstream workflow steps until the failed step passes validation gates.
6. Do not repeat the same command without changing the diagnosed cause.
7. Prefer deterministic checks over clever shell constructs.
8. For conda/subprocess errors, first check executable availability with `which`, `--version`, and package import/load tests.
9. For scientific file generation, success requires semantic validation, not only file existence.

Classify the failure as exactly one primary class:
- command_syntax_error
- missing_executable
- missing_package
- missing_input_file
- wrong_path_or_uri
- format_or_schema_error
- chromosome_name_mismatch
- coordinate_system_mismatch
- scientific_validation_failed
- downstream_tool_runtime_error
- unknown_truncated_log

After the prose diagnosis, include a compact machine-readable repair hint block
when the failure is dependency/runtime related:
```json
{
  "failure_class": "missing_executable|missing_package",
  "missing_tools": ["tool_or_package_name"],
  "affected_skill": "selected skill id if known",
  "recommended_action": "install_runtime|verify_existing_runtime|block_runtime",
  "suggested_package": "package name if known",
  "healthcheck": ["exact minimal healthcheck commands to run after repair"]
}
```
If the active skill context includes a declared healthcheck or install command,
use that exact command or a strict subset of it; do not invent a Python/R binding
or alternative package name.

Domain-specific rules for CALDER2 compartment-calling:
- A gene-density BED is valid only when it is binned at the selected resolution,
  chromosome names match cooler chromnames, column 4 sum > 0, and non-zero bins > 0.
- Do not call gene intervals or gene lengths "gene density".
- Never use artificial chromosome lengths when FASTA/cooler lengths are available.
- Do not use process substitution `<(...)` under `conda run`; use real temp files.
- Before official CALDER2 mode, verify `Rscript`, `Rscript --version`, and
  `Rscript -e "library(CALDER); packageVersion('CALDER')"`.

Analyze the following execution error:
Recovery tier: {tier} (retry_count={retry_count})
Primary failure class detected by rules: {failure_class}
Rule-based next action: {failure_action}

<error>
STDOUT:
{clip_text(stdout_content, 1200)}

STDERR:
{clip_text(stderr_content, 1200)}
</error>

{clip_text(rag_advice, 1200)}

{clip_text(skill_advice, 1800)}

Output format:
Root cause:
- one sentence, evidence-backed.

Evidence:
- quote the exact error line or failed invariant.

Do not do:
- list commands or assumptions that must not be repeated.

Next diagnostic command:
- provide only if the root cause is not proven or the log is truncated.

Corrected command/code:
- provide only after the root cause is proven.

Validation gates:
- list concrete checks that must pass before continuing.

Escalation:
- if the same class failed twice, stop broad guessing and request the missing log,
  environment path, or tool availability result.

Log policy:
{LOG_TAIL_POLICY}
{env_hint}
{external_hint}
{repeated_class_hint}
"""
                # 调用 LLM 生成诊断
                try:
                    diagnosis_msg = await llm.ainvoke([HumanMessage(content=diagnosis_prompt)])
                    diagnosis_text = diagnosis_msg.content
                except Exception as exc:
                    diagnosis_text = (
                        "Automatic diagnosis LLM call failed. "
                        f"{llm_failure_summary(exc, 'error_recovery_middleware')}\n"
                        "Use the observed STDOUT/STDERR above and retry with a different command; "
                        "do not repeat the same failing command."
                    )
                
                # --- D. 注入修正后的上下文 ---
                # 我们修改 result 中的 messages，追加诊断信息
                # 这样主 Agent 在下一次 generate 时，就会看到错误 + 专家的修复建议
                augmented_observation = last_msg + f"\n\n<system_diagnosis>\n{diagnosis_text}\n</system_diagnosis>"
                
                # 更新返回结果
                result["messages"] = [HumanMessage(content=augmented_observation)]
                
            return result
        return wrapper
    return decorator
