import re
import json
import hashlib
import functools
import logging
from pathlib import Path
from typing import Tuple, Dict, Any, List
from dataclasses import asdict
from langgraph.types import interrupt
from langchain_core.messages import HumanMessage, SystemMessage
from execution.confirmation import RiskAnalyzer, RiskLevel
from execution.monitoring import LogCleaner
from execution.profiling import get_current_node, get_current_profiler, get_current_task_id
from config.config import (
    KB_MAP,
    ENABLE_PROGRESS_DETECTOR,
    ENABLE_RECOVERY_PATCHING,
)
from colorama import Fore, Style
from agent_core.llm_safety import clip_text, llm_failure_summary
from agent_core.context_lineage import ainvoke_with_snapshot
from agent_core.recovery import (
    ConstraintConflictDetector,
    ActionSignature,
    FailureContext,
    EvidenceAssessment,
    HypothesisManager,
    ProgressDetector,
    ProgressTracker,
    RecoveryAttempt,
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryAction,
    RecoveryFailureClass,
    RecoveryFailureOwner,
    RecoveryPhase,
    RecoveryPolicy,
    RecoveryStateStore,
    RepairExecutor,
    error_fingerprint,
    deterministic_recovery_decision,
    diagnosis_required,
    evaluate_recovery_trigger,
    recovery_action_route,
    constrain_recovery_decision,
    recovery_decision_from_diagnosis,
    contextualize_recovery_decision,
    structured_user_decision_required,
    parse_recovery_decision,
    parse_recovery_judge_decision,
)
from agent_core.diagnostics import (
    ProgressTracker as ActiveProgressTracker,
    build_progress_evidence,
    build_recovery_context,
)
from agent_core.knowledge_conflicts import (
    ClaimRegistry,
    ConflictResolver,
    KnowledgeConflictDetector,
    KnowledgeConflictStore,
)
from agent_core.plan_tracker import (
    apply_recovery_decision,
    increment_plan_budget,
    load_plan_state,
    save_plan_state,
    semantic_plan_hash,
)
from prompts.manager import PromptManager

logger = logging.getLogger("BioMind.Middleware")

LOG_TAIL_POLICY = (
    "When an error points to a .err, .log, .out, or trace file, inspect only a "
    "small tail first, for example `tail -n 80 path/to/file.log`. For very large "
    "logs, combine targeted grep with a tail, for example "
    "`grep -iE 'error|exception|fatal|traceback|killed' path/to/file.log | tail -n 40`. "
    "Never `cat` or paste an entire long log into the prompt."
)


def _current_step_plan_constraints(
    state: Dict[str, Any],
    data_path: str | Path | None,
    task_id: str,
    stage_id: str,
) -> List[str]:
    """Project only structured constraints for the failed step into Recovery."""
    if not data_path:
        return []
    plan = load_plan_state(data_path, task_id)
    steps = [item for item in plan.get("steps", []) or [] if isinstance(item, dict)]
    step = next((item for item in steps if str(item.get("step_id") or "") == stage_id), None)
    if step is None:
        step = next(
            (item for item in steps if str(item.get("status") or "").lower() in {"running", "in_progress"}),
            None,
        )
    if step is None:
        return []

    constraints: List[str] = []
    for key in (
        "required_constraints",
        "assumptions",
        "input_contracts",
        "output_contracts",
        "environment_contract",
        "required_environment",
        "expected_tool_versions",
    ):
        value = step.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item in (None, "", [], {}):
                continue
            text = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
            constraints.append(f"{key}: {text[:1000]}")
    return constraints[:32]

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
    if (
        "launcher class not found" in lower
        or "org/graalvm/polyglot/polyglotexception" in lower
        or "noclassdeffounderror" in lower and "graalvm" in lower
    ):
        return (
            "runtime_launcher_mismatch",
            "Regenerate the failed command with the script's declared runner or controller Python; "
            "do not reinstall the analysis tool or repeat the incompatible environment interpreter.",
        )
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
    selected = state.get("active_skills") or []
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


def _legacy_error_recovery_middleware(llm, retriever, skill_manager=None, data_path=None):
    """
    DEPRECATED / NOT REGISTERED IN THE ACTIVE GRAPH.

    Retained only for old imports and tests. New execution failures use
    ``error_recovery_middleware`` plus the canonical RecoveryContext. Do not
    reconnect this observation-text/LLM patch path.

    错误恢复中间件装饰器工厂
    :param llm: 用于生成修复方案的 LLM 实例
    :param retriever: 用于检索 RAG 的检索器实例
    :param skill_manager: 可选，用于在二次失败后加载 active skill 的错误/运行上下文
    """
    def decorator(func):
        async def _recover(state, *args, _result_holder=None, **kwargs):
            # 1. 先执行原始的 execute 逻辑
            result = await func(state, *args, **kwargs)
            if _result_holder is not None:
                _result_holder["result"] = result
            
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
                # The observation is display text and may contain validation
                # summaries or literal stream labels. Use it only when the
                # structured execution record is unavailable.
                stdout_content = ""
                if "STDOUT:" in last_msg:
                    stdout_content = last_msg.split("STDOUT:")[-1].split("STDERR:")[0]
                stderr_content = (
                    last_msg.split("STDERR:")[-1].split("</observation>")[0]
                    if "STDERR:" in last_msg else ""
                )
                task_id = str((state or {}).get("task_id") or result.get("task_id") or "default") if isinstance(state, dict) else "default"
                raw_evidence = result.get("execution_evidence") if isinstance(result, dict) else {}
                if (not isinstance(raw_evidence, dict) or not raw_evidence) and isinstance(state, dict):
                    raw_evidence = state.get("execution_evidence") or {}
                if not isinstance(raw_evidence, dict):
                    raw_evidence = {}

                def _stream_from_evidence(name: str, fallback: str) -> str:
                    if name in raw_evidence:
                        return str(raw_evidence.get(name) or "")
                    log_path = str(raw_evidence.get(f"{name}_log") or "").strip()
                    if log_path:
                        try:
                            return Path(log_path).read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            pass
                    summary_name = f"{name}_summary"
                    if summary_name in raw_evidence:
                        return str(raw_evidence.get(summary_name) or "")
                    return fallback

                raw_stdout = _stream_from_evidence("stdout", stdout_content)
                raw_stderr = _stream_from_evidence("stderr", stderr_content)
                deterministic_error = str(
                    (raw_evidence.get("root_cause_analysis") or {}).get("primary_error_message")
                    if isinstance(raw_evidence.get("root_cause_analysis"), dict)
                    else ""
                ).strip()
                error_text = raw_stderr.strip() or raw_stdout.strip() or deterministic_error
                fingerprint_source = deterministic_error or error_text
                legacy_error_fingerprint = LogCleaner.extract_error_fingerprint(fingerprint_source)
                failure_class, failure_action = classify_execution_failure(
                    "\n".join(part for part in (deterministic_error, raw_stderr[-4000:], raw_stdout[-4000:]) if part)
                    or error_text
                )
                raw_returncode = raw_evidence.get("returncode")
                try:
                    normalized_returncode = int(raw_returncode) if raw_returncode is not None else 1
                except (TypeError, ValueError):
                    normalized_returncode = 1
                recovery_store = RecoveryStateStore(data_path, task_id) if data_path else None
                stage_id = str(
                    raw_evidence.get("step_id")
                    or (state or {}).get("current_step_id")
                    or (state or {}).get("current_stage_id")
                    or (state or {}).get("active_batch_id")
                    or ""
                )
                all_previous_attempts = recovery_store.load_attempts() if recovery_store else list((state or {}).get("recovery_attempts", []) or [])
                previous_attempts = [
                    item for item in all_previous_attempts
                    if not stage_id or not item.get("stage_id") or item.get("stage_id") == stage_id
                ]
                policy = RecoveryPolicy()
                recovery_context = FailureContext.from_execution(
                    task_id=task_id,
                    command=str(raw_evidence.get("command") or (state or {}).get("pending_action", {}).get("code") or ""),
                    cwd=str(raw_evidence.get("cwd") or (state or {}).get("cwd") or ""),
                    stdout=raw_stdout,
                    stderr=raw_stderr,
                    exit_code=normalized_returncode,
                    signal=(
                        int(raw_evidence.get("signal"))
                        if str(raw_evidence.get("signal") or "").lstrip("-").isdigit()
                        else None
                    ),
                    command_argv=raw_evidence.get("argv") if isinstance(raw_evidence.get("argv"), list) else None,
                    stage_id=stage_id,
                    skill_id=(_selected_skill_ids(state)[0] if _selected_skill_ids(state) else None),
                    environment_name=str((state or {}).get("runtime_status", {}).get("env") or "") or None,
                    generated_script_path=raw_evidence.get("script_file"),
                    expected_artifacts=(state or {}).get("artifacts", []) if isinstance(state, dict) else [],
                    user_constraints=(state or {}).get("user_constraints", []) if isinstance(state, dict) else [],
                    plan_constraints=_current_step_plan_constraints(
                        dict(state or {}), data_path, task_id, stage_id
                    ),
                    previous_attempts=previous_attempts,
                    environment_snapshot=(state or {}).get("runtime_status", {}) if isinstance(state, dict) else {},
                    trace_text=str(raw_evidence.get("trace_text") or ""),
                    pipeline_statuses=raw_evidence.get("pipeline_statuses") if isinstance(raw_evidence.get("pipeline_statuses"), list) else None,
                    artifact_state={
                        "missing_artifacts": (state or {}).get("missing_artifacts", []) if isinstance(state, dict) else [],
                    },
                    stage_status=(state or {}).get("plan_progress") if isinstance(state, dict) else None,
                    log_metadata_before=raw_evidence.get("log_metadata_before") if isinstance(raw_evidence.get("log_metadata_before"), dict) else None,
                    stdout_log=str(raw_evidence.get("stdout_log") or ""),
                    stderr_log=str(raw_evidence.get("stderr_log") or ""),
                )
                context_primary_error = str(
                    (recovery_context.root_cause_analysis or {}).get("primary_error_message")
                    or recovery_context.inner_error
                    or ""
                ).strip()
                if context_primary_error:
                    deterministic_error = context_primary_error
                    fingerprint_source = context_primary_error
                    legacy_error_fingerprint = LogCleaner.extract_error_fingerprint(fingerprint_source)
                    failure_class, failure_action = classify_execution_failure(
                        "\n".join(
                            part
                            for part in (
                                context_primary_error,
                                raw_stderr[-4000:],
                                raw_stdout[-4000:],
                            )
                            if part
                        )
                    )
                print(f"{Fore.RED}🔍 Error Fingerprint: {legacy_error_fingerprint[:100]}...{Style.RESET_ALL}")
                if recovery_store:
                    recovery_store.save_context(recovery_context)
                    recovery_store.save_phase({
                        "phase": RecoveryPhase.CAPTURE.value,
                        "status": "CAPTURED",
                        "failure_fingerprint": recovery_context.error_fingerprint,
                    })
                    recovery_store.append_trace("CAPTURE_FAILURE", recovery_context.llm_view())
                    recovery_store.save_phase({
                        "phase": RecoveryPhase.UNWRAP.value,
                        "status": "UNWRAPPED",
                        "failure_fingerprint": recovery_context.error_fingerprint,
                        "inner_command": recovery_context.inner_command,
                        "inner_error": recovery_context.inner_error,
                        "related_log_paths": recovery_context.related_log_paths,
                    })
                persisted_progress = recovery_store.load_progress() if recovery_store else ProgressTracker()
                latest_progress = persisted_progress.decisions[-1] if persisted_progress.decisions else None
                allowed, policy_reason = policy.check(
                    previous_attempts,
                    recovery_context.error_fingerprint,
                    recovery_context.inner_command or recovery_context.command,
                    stage_id=recovery_context.stage_id,
                    progress_decision=latest_progress,
                )
                if not allowed:
                    task_mode = str((state or {}).get("task_mode") or "managed").strip().lower()
                    user_interaction_allowed = (
                        task_mode == "managed"
                        and not bool((state or {}).get("non_interactive"))
                        and not bool((state or {}).get("disable_user_questions"))
                    )
                    blocked = RecoveryDecision(
                        verdict="blocked",
                        root_cause=policy_reason,
                        confidence=1.0,
                        action_type="block_task",
                        evidence=[recovery_context.inner_error or error_text],
                        expected_new_evidence=["user decision or a genuinely different repair strategy"],
                    )
                    status = {
                        "phase": RecoveryPhase.EXHAUSTED.value,
                        "status": (
                            "NEEDS_USER_DECISION"
                            if user_interaction_allowed
                            else "NO_PROGRESS"
                            if any(token in policy_reason for token in ("no_progress", "readonly", "same_recovery_command"))
                            else "RECOVERY_EXHAUSTED"
                        ),
                        "reason": policy_reason,
                        "failure_fingerprint": recovery_context.error_fingerprint,
                    }
                    if recovery_store:
                        recovery_store.save_status(status)
                        recovery_store.append_trace("EXHAUSTED", status)
                    result["recovery_phase"] = RecoveryPhase.EXHAUSTED.value
                    result["recovery_status"] = status["status"]
                    result["recovery_context"] = recovery_context.llm_view()
                    result["root_cause_analysis"] = recovery_context.root_cause_analysis
                    result["command_chain"] = recovery_context.command_chain
                    result["recovery_decision"] = blocked.to_dict()
                    result["recovery_attempts"] = previous_attempts
                    if latest_progress:
                        result["recovery_progress"] = asdict(latest_progress)
                        result["forbidden_recovery_actions"] = list(latest_progress.forbidden_next_actions or [])
                        result["diagnostic_summary"] = {"latest_progress": asdict(latest_progress)}
                    pending_type = "ask_user" if user_interaction_allowed else "blocked"
                    question = (
                        "Automatic recovery cannot make further safe progress without repeating an action. "
                        f"Reason: {policy_reason}. Please provide a different recovery direction, new evidence, "
                        "or confirm that this stage should be recorded as blocked."
                    )
                    result["pending_recovery_action"] = {
                        "type": pending_type,
                        "reason": policy_reason,
                        "blocking_reason": policy_reason,
                        "required": "user decision or a genuinely different repair strategy",
                        "progress": asdict(latest_progress) if latest_progress else {},
                        "forbidden_next_actions": list(latest_progress.forbidden_next_actions or []) if latest_progress else [],
                        "step_id": recovery_context.stage_id,
                        "evidence_ids": [],
                        "attempted_probes": [
                            item.get("commands", []) for item in previous_attempts
                            if item.get("action_type") in {"run_probe", "inspect_file", "inspect_log"}
                        ],
                        "remaining_safe_probes": [],
                        "resume_condition": "new user input, environment change, or a genuinely different recovery strategy",
                    }
                    if user_interaction_allowed:
                        result["pending_recovery_action"]["question"] = question
                        result["pending_human_question"] = {
                            "type": "recovery_user_decision",
                            "stage": "recovery",
                            "question": question,
                            "resume_step": "generate",
                        }
                    status_tag = "recovery_needs_user_decision" if user_interaction_allowed else "recovery_blocked"
                    result["messages"] = [HumanMessage(content=(
                        last_msg
                        + f"\n\n<{status_tag}>\n"
                        + json.dumps(status, ensure_ascii=False)
                        + "\nDo not repeat the same read-only checks.\n"
                        + f"</{status_tag}>"
                    ))]
                    result["next_step"] = "ask_user" if user_interaction_allowed else "finish"
                    return result
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
                missing_path_match = re.search(
                    r"([A-Za-z0-9_.+-]+)\s+(?:not found on PATH|command not found|not found)",
                    error_text,
                    re.IGNORECASE,
                )
                if missing_path_match and isinstance(runtime_status, dict):
                    missing_tool = missing_path_match.group(1)
                    passed_records = [
                        record for record in runtime_status.values()
                        if isinstance(record, dict)
                        and record.get("ok")
                        and (
                            missing_tool in str(record.get("healthcheck", ""))
                            or missing_tool in " ".join(str(tool) for tool in record.get("tools", []) or [])
                            or missing_tool in " ".join(str(pkg) for pkg in record.get("packages", []) or [])
                        )
                    ]
                    if not passed_records and len(runtime_status) == 1:
                        only_record = next(iter(runtime_status.values()), {})
                        if isinstance(only_record, dict) and only_record.get("ok"):
                            # A declared healthcheck can validate an
                            # environment without listing every executable
                            # exposed by that runtime.  Treat a lone healthy
                            # selected runtime as path/runner evidence, not
                            # as proof that installation is required.
                            passed_records = [only_record]
                    if passed_records:
                        record = passed_records[-1]
                        runner = record.get("env_runner") or (
                            f"conda run -n {record.get('env')} " if record.get("env") else ""
                        )
                        env_hint = (
                            "\n\nRuntime diagnosis: the selected skill runtime healthcheck already passed, "
                            "so this is not an installation failure. The command likely ran outside the "
                            f"`{record.get('env', 'declared')}` environment. Regenerate the same skill command "
                            f"with the skill-declared runner/prefix `{runner or 'from the active skill runtime'}`. Do not output `<install_tool>` unless a new "
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
                        docs = retriever.retrieve(query=legacy_error_fingerprint, target_datasets=[KB_MAP["ERRORS"]])
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

                deterministic_root_cause = json.dumps(
                    recovery_context.root_cause_analysis or {},
                    ensure_ascii=False,
                )
                deterministic_command_chain = json.dumps(
                    recovery_context.command_chain or {},
                    ensure_ascii=False,
                )

                # Build the persisted state before the LLM call so the model
                # sees prior hypotheses, evidence reliability, relations,
                # probes, and forbidden actions instead of starting over.
                prompt_hypothesis_manager = (
                    recovery_store.load_diagnostic_manager()
                    if recovery_store else None
                ) or HypothesisManager(
                    (state or {}).get("recovery_hypotheses", [])
                    if isinstance(state, dict) else []
                )
                prompt_had_hypotheses = bool(getattr(prompt_hypothesis_manager, "hypotheses", []))
                prompt_analysis_records = prompt_hypothesis_manager.register_analysis(
                    recovery_context.root_cause_analysis,
                    recovery_context.llm_view(),
                    attempt=len(previous_attempts),
                )
                if not prompt_had_hypotheses:
                    prompt_hypothesis_manager.initialize_from_analysis(
                        recovery_context.root_cause_analysis,
                        attempt=len(previous_attempts),
                        context=recovery_context.llm_view(),
                    )
                persisted_progress = persisted_progress if "persisted_progress" in locals() else (recovery_store.load_progress() if recovery_store else ProgressTracker())
                prompt_progress = persisted_progress.decisions[-1] if persisted_progress.decisions else None
                diagnostic_state_context = json.dumps(
                    prompt_hypothesis_manager.to_dict(), ensure_ascii=False
                )
                progress_state_context = json.dumps(
                    asdict(prompt_progress) if prompt_progress else {}, ensure_ascii=False
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
8. For conda/subprocess errors, first check executable availability with 'which', '--version', and package import/load tests.
9. For scientific file generation, success requires semantic validation, not only file existence.
10. The deterministic RootCauseAnalyzer block below has precedence over prose:
    do not replace executor argv, do not call a wrapper the primary failed tool,
    and do not mark a probable cause as confirmed.
11. If the analyzer reports a masked pipeline failure, the next action must repair
    or verify the inner segment; a zero outer exit code is not success.
12. Hypothesis state and progress are persisted across attempts.  Do not restart
    diagnosis from prose.  Use the supplied evidence IDs and prior state.
13. You may assess evidence with evidence_assessments, but never set confidence,
    new_confidence, probability, or progress_score.  Deterministic code computes
    those values.  Do not create a duplicate hypothesis merely with different wording.
14. Recovery patches are ScriptPatch objects only: set patch_type=script_patch,
    target_file, base_hash, operation, and the minimal text change. Ordinary path,
    shell, parameter, redirection, or environment errors must not become PlanPatch.
15. Evidence is source typed. Script source (including a trap that prints
    `[ERROR]`) and validator summaries cannot confirm a runtime failed command.
    Only stderr, a tool log, or shell trace may directly confirm it. If the
    original command or exit status is unresolved, verdict must be `probe`, not
    `repair` or `rerun`.
16. A diagnostic command must preserve every failed check. Do not use `|| echo`,
    `|| true`, or mixed `a && b || echo ... && c` chains. Use an explicit
    `check_failed=0`, independent `if ! check; then ...; check_failed=1; fi`
    blocks, and `exit "$check_failed"`.
17. File, directory, config, and log existence are only partial-artifact
    evidence. Validate the declared non-empty core artifacts, fatal-free final
    log status, and a minimal load check when the active skill declares one.

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

When the failure is dependency/runtime related, represent the repair hint in the
mandatory JSON decision fields below:
```json
{{
  "failure_class": "missing_executable|missing_package",
  "missing_tools": ["tool_or_package_name"],
  "affected_skill": "selected skill id if known",
  "recommended_action": "install_runtime|verify_existing_runtime|block_runtime",
  "suggested_package": "package name if known",
  "healthcheck": ["exact minimal healthcheck commands to run after repair"]
}}
```
If the active skill context includes a declared healthcheck or install command,
use that exact command or a strict subset of it; do not invent a Python/R binding
or alternative package name.

Domain-specific recovery rule:
- Do not rely on benchmark-case memories or one-off fixes. Use the active skill
  context, the executed command, and the concrete error evidence to infer the
  smallest valid repair.
- If the failed step involves derived inputs, validate the invariant documented
  by the active skill rather than inventing a new domain rule. General examples:
  matched sample names, paired input completeness, chromosome/contig name
  compatibility, coordinate system compatibility, non-empty required columns,
  and tool-declared runtime healthchecks.
- If no active skill context documents the invariant or repair, ask for the
  missing decision/evidence or produce a truthful blocked/unresolved status
  after retry limits; do not add another hard-coded workflow-specific patch.

Analyze the following execution error:
Recovery tier: {tier} (retry_count={retry_count})
Primary failure class detected by rules: {failure_class}
Rule-based next action: {failure_action}

<error>
STDOUT:
{clip_text(raw_stdout[-4000:], 1800)}

STDERR:
{clip_text(raw_stderr[-4000:], 1800)}
</error>

<deterministic_root_cause>
{deterministic_root_cause}
</deterministic_root_cause>

<command_execution_chain>
{deterministic_command_chain}
</command_execution_chain>

<persisted_diagnostic_state>
{clip_text(diagnostic_state_context, 12000)}
</persisted_diagnostic_state>

<persisted_progress_state>
{clip_text(progress_state_context, 6000)}
</persisted_progress_state>

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

Machine-readable output contract (mandatory): return exactly one JSON object and
no prose or markdown. It must contain at least:
{{
  "verdict": "probe|repair|rerun|rollback|blocked|escalate",
  "root_cause": "evidence-backed root cause",
  "confidence": 0.0,
  "action_type": "inspect_file|inspect_log|run_probe|patch_file|replace_command|change_environment|install_dependency|regenerate_script|rerun_stage|fallback_method|block_task",
  "evidence": [],
  "commands": ["one complete executable command string per item; never emit command objects"],
  "patches": [],
  "verification_checks": [
    {{"check_type": "file_nonempty|content_pattern|command_exit|artifact_bundle", "target": "path", "pattern": "actual regex for content_pattern only", "expected": "typed expected value for other checks", "required": true}}
  ],
  "expected_new_evidence": [],
  "evidence_assessments": [
    {{"evidence_id": "EV_id", "hypothesis_id": "H_id", "relation": "supports|contradicts|neutral|ambiguous", "strength": "weak|moderate|strong|decisive", "directness": "direct|indirect|inferred", "reason": "evidence-backed explanation"}}
  ],
  "new_hypothesis_proposals": [],
  "recommended_probe_id": null,
  "risk_level": "safe|low|medium|high",
  "violates_user_constraint": false
}}
Do not emit a patch that removes a user-required option unless the evidence and
constraint-conflict fields prove that no compatible invocation exists. A probe
must name the hypothesis it will distinguish; a repair/rerun must name the
changed strategy and its verification checks.
For `content_pattern`, `pattern` must be a literal machine-evaluable regex that
should occur in `target`; never write narrative conditions such as "must be
identified". Probe actions should put the evidence they seek in
`expected_new_evidence`, not in artifact `verification_checks`.

Log policy:
{LOG_TAIL_POLICY}
{env_hint}
{external_hint}
{repeated_class_hint}
"""
                # 调用 LLM 生成诊断
                try:
                    diagnosis_msg, diagnosis_snapshot = await ainvoke_with_snapshot(
                        llm,
                        [HumanMessage(content=diagnosis_prompt)],
                        data_path=data_path,
                        task_id=task_id,
                        metadata={
                            "node_name": "recovery_judge",
                            "call_kind": "recovery_judge",
                            "step_id": str((state or {}).get("current_step_id") or ""),
                            "legacy_recovery": True,
                        },
                    )
                    diagnosis_text = diagnosis_msg.content
                except Exception as exc:
                    diagnosis_text = (
                        "Automatic diagnosis LLM call failed. "
                        f"{llm_failure_summary(exc, 'error_recovery_middleware')}\n"
                        "Use the observed STDOUT/STDERR above and retry with a different command; "
                        "do not repeat the same failing command."
                    )

                # The text is retained in the observation for compatibility,
                # but routing consumes one validated JSON decision. A single
                # repair call is allowed; malformed output must never become
                # an unbounded LLM repair loop.
                decision: RecoveryDecision
                decision_parse_error = ""
                try:
                    decision = parse_recovery_decision(diagnosis_text)
                except Exception as exc:
                    decision_parse_error = str(exc)
                    repair_prompt = (
                        clip_text(diagnosis_prompt, 16000)
                        + "\n\nReturn exactly one JSON object for the recovery decision. "
                        "No markdown and no prose. Required keys: verdict, root_cause, confidence (legacy and ignored), "
                        "action_type, evidence, commands, patches, verification_checks, "
                        "rollback_steps, expected_new_evidence, evidence_assessments, new_hypothesis_proposals, "
                        "recommended_probe_id, risk_level, violates_user_constraint. Never include confidence fields "
                        "inside evidence_assessments.\n"
                        "Every verification_checks item must be an object such as "
                        '{"check_type":"file_nonempty","target":"output.tsv","required":true}; '
                        "never return verification checks as strings.\n"
                        "Allowed verdicts: probe, repair, rerun, rollback, blocked, escalate.\n"
                        "Allowed action_type: inspect_file, inspect_log, run_probe, patch_file, "
                        "replace_command, change_environment, install_dependency, regenerate_script, "
                        "rerun_stage, fallback_method, block_task.\n"
                        "The commands field must be an array of non-empty executable command strings, never objects. "
                        "Actions inspect_file, inspect_log, run_probe, rerun_stage, and verdict rerun require a command.\n"
                        f"Failure context:\n{json.dumps(recovery_context.llm_view(), ensure_ascii=False)}\n"
                        f"Previous diagnosis:\n{clip_text(diagnosis_text, 3000)}\n"
                        "Use the exact evidence; do not delete a user-required option merely because a wrapper rejected it."
                    )
                    try:
                        repaired_msg, _ = await ainvoke_with_snapshot(
                            llm,
                            [HumanMessage(content=repair_prompt)],
                            data_path=data_path,
                            task_id=task_id,
                            metadata={
                                "node_name": "recovery_judge",
                                "call_kind": "recovery_judge",
                                "parent_call_id": str(diagnosis_snapshot.get("call_id") or ""),
                                "legacy_recovery": True,
                                "repair": True,
                            },
                        )
                        repaired_text = str(getattr(repaired_msg, "content", repaired_msg) or "")
                        diagnosis_text = diagnosis_text + "\n\n[JSON repair response]\n" + repaired_text
                        decision = parse_recovery_decision(repaired_text)
                    except Exception as repair_exc:
                        decision = RecoveryDecision(
                            verdict="repair",
                            root_cause="recovery_decision_unavailable_regenerate_from_deterministic_evidence",
                            confidence=0.2,
                            action_type="regenerate_script",
                            evidence=[decision_parse_error, str(repair_exc), recovery_context.inner_error or error_text],
                            expected_new_evidence=[
                                "a changed command that addresses the confirmed failed command and primary error"
                            ],
                        )

                recovery_requires_user = False
                deterministic_status = str(
                    (recovery_context.root_cause_analysis or {}).get("status") or ""
                ).lower()
                deterministic_error = str(
                    (recovery_context.root_cause_analysis or {}).get("primary_error_message") or ""
                ).strip()
                recommended_probes = list(
                    (recovery_context.root_cause_analysis or {}).get("recommended_probes") or []
                )
                evidence_state = "SUFFICIENT_FOR_SCRIPT_REPAIR"
                if deterministic_status in {"ambiguous", "insufficient_evidence", ""} or not (
                    recovery_context.inner_command and (recovery_context.inner_error or deterministic_error)
                ):
                    evidence_state = "INSUFFICIENT_NEEDS_PROBE"
                    probe = next(
                        (
                            item for item in recommended_probes
                            if isinstance(item, dict) and str(item.get("command") or "").strip()
                        ),
                        {},
                    )
                    if probe:
                        decision.verdict = "probe"
                        decision.action_type = "run_probe"
                        decision.commands = [str(probe.get("command"))]
                        decision.patches = []
                        decision.rerun_command = None
                        decision.root_cause = "insufficient_evidence_requires_targeted_probe"
                        decision.evidence = list(dict.fromkeys([
                            *decision.evidence,
                            *[str(item) for item in (recovery_context.root_cause_analysis or {}).get("unresolved_questions", [])],
                        ]))
                        decision.expected_new_evidence = [
                            str(probe.get("information_gain") or "the first failing command and source line")
                        ]
                    else:
                        recovery_requires_user = True
                        decision.verdict = "blocked"
                        decision.action_type = "block_task"
                        decision.patches = []
                        decision.root_cause = "insufficient_evidence_and_no_safe_probe"
                        decision.expected_new_evidence = ["raw failing command or a safe targeted probe"]

                # Diagnosis state is durable across attempts.  The legacy
                # decision.confidence field is parsed for compatibility but
                # deliberately ignored; only deterministic evidence updates
                # the hypothesis confidence.
                hypothesis_manager = prompt_hypothesis_manager
                progress_tracker = persisted_progress
                analysis_records = prompt_analysis_records
                for proposal in decision.new_hypothesis_proposals:
                    if not isinstance(proposal, dict):
                        continue
                    description = str(proposal.get("description") or proposal.get("title") or "")
                    if description:
                        hypothesis_manager.upsert(
                            description,
                            str(proposal.get("category") or "unknown"),
                            evidence_for=[],
                            evidence_against=[],
                            proposed_probe=[],
                            proposed_repair=[],
                            title=str(proposal.get("title") or ""),
                            attempt=len(previous_attempts),
                            deterministic=False,
                        )
                # LLM evidence strings are low-reliability interpretations;
                # raw executor/argv/stderr records above remain authoritative.
                for evidence_text in decision.evidence:
                    if str(evidence_text).strip():
                        hypothesis_manager.register_evidence(
                            "llm_interpretation", evidence_text, attempt=len(previous_attempts)
                        )
                hypothesis_manager.apply_llm_assessments(
                    decision.evidence_assessments,
                    attempt=len(previous_attempts),
                )
                hypothesis = next(
                    (
                        item for item in hypothesis_manager.hypotheses
                        if item.hypothesis_id == decision.selected_hypothesis_id
                    ),
                    None,
                )
                if hypothesis is None:
                    hypothesis = hypothesis_manager.upsert(
                        decision.root_cause or failure_class,
                        failure_class,
                        evidence_for=[],
                        evidence_against=[],
                        proposed_probe=[],
                        proposed_repair=[],
                        attempt=len(previous_attempts),
                        deterministic=False,
                    )
                decision.selected_hypothesis_id = hypothesis.hypothesis_id
                # Expose the deterministic value in the audit record; never
                # propagate the LLM's legacy top-level confidence claim.
                decision.confidence = float(hypothesis.confidence)
                diagnosis_fingerprint = error_fingerprint(
                    decision.root_cause,
                    decision.action_type,
                    "\n".join(decision.commands),
                )
                if any(
                    item.get("diagnosis_fingerprint") == diagnosis_fingerprint
                    for item in previous_attempts
                ) and not decision.patches:
                    recovery_requires_user = True
                    decision.verdict = "blocked"
                    decision.action_type = "block_task"
                    decision.root_cause = "repeated_recovery_diagnosis_without_new_action"
                    decision.evidence.append(
                        "The same recovery diagnosis/action was already attempted for this stage."
                    )
                conflict = ConstraintConflictDetector.detect(recovery_context.user_constraints, decision)
                if conflict:
                    recovery_requires_user = True
                    decision.verdict = "blocked"
                    decision.action_type = "block_task"
                    decision.violates_user_constraint = True
                    decision.constraint_conflict_reason = conflict

                # Recovery is another knowledge source, not an authority that
                # may rewrite user constraints. Run the generic detector before
                # RepairExecutor so a patch cannot silently drift the plan.
                if data_path:
                    try:
                        conflict_root = Path(data_path) / "task_outputs" / str(task_id)
                        claim_registry = ClaimRegistry.load(conflict_root)
                        for item in recovery_context.user_constraints:
                            if str(item).strip():
                                claim_registry.register_user_constraint(str(item), scope="task", scope_id=str(task_id))
                        for item in recovery_context.plan_constraints:
                            if str(item).strip():
                                claim_registry.register_proposition(
                                    str(item),
                                    "plan_assumption",
                                    strength="assumption",
                                    scope="stage",
                                    scope_id=recovery_context.stage_id or str(task_id),
                                )
                        if recovery_context.command:
                            claim_registry.register_proposition(
                                f"Runtime failed command: {recovery_context.command[:2500]}",
                                "runtime_argv",
                                strength="observation",
                                scope="command",
                                scope_id=recovery_context.stage_id or str(task_id),
                                confidence=0.95,
                                reliability=0.95,
                                attributes={"fact_key": "recovery_command", "value": recovery_context.command[:2500]},
                            )
                        recovery_text = "\n".join(
                            [decision.action_type, decision.rerun_command or "", *decision.commands]
                            + [json.dumps(patch, ensure_ascii=False) for patch in decision.patches]
                        ).strip()
                        if recovery_text:
                            claim_registry.register_proposition(
                                f"Recovery proposal: {recovery_text[:4000]}",
                                "recovery_proposal",
                                strength="inference",
                                scope="stage",
                                scope_id=recovery_context.stage_id or str(task_id),
                                confidence=0.45,
                                reliability=0.35,
                                attributes={"step_ids": [recovery_context.stage_id] if recovery_context.stage_id else []},
                            )
                        detected_conflicts = KnowledgeConflictDetector.detect(
                            claim_registry,
                            attempt=len(previous_attempts),
                        )
                        conflict_store = KnowledgeConflictStore(conflict_root, str(task_id))
                        conflict_store.save_registry(claim_registry)
                        conflict_store.append("conflicts.jsonl", detected_conflicts)
                        conflict_decisions = [ConflictResolver.resolve(item, claim_registry) for item in detected_conflicts]
                        conflict_store.append("conflict_resolutions.jsonl", conflict_decisions)
                        conflict_store.save_summary(
                            conflicts=detected_conflicts,
                            resolutions=conflict_decisions,
                            user_hard_constraints=recovery_context.user_constraints,
                        )
                        blocking_conflicts = [item for item in detected_conflicts if item.blocking]
                        if blocking_conflicts:
                            blocking_ids = {item.conflict_id for item in blocking_conflicts}
                            blocking_resolutions = [
                                item for item in conflict_decisions
                                if item.conflict_id in blocking_ids
                            ]
                            user_resolutions = [
                                item for item in blocking_resolutions
                                if item.decision == "request_user_decision" or item.requires_user_approval
                            ]
                            terminal_resolutions = [
                                item for item in blocking_resolutions
                                if item.decision == "block"
                            ]
                            probe_resolutions = [
                                item for item in blocking_resolutions
                                if item.decision == "require_probe"
                            ]
                            conflict_reason = " | ".join(
                                item.reason for item in blocking_resolutions[:3] if item.reason
                            ) or " | ".join(item.description for item in blocking_conflicts[:3])
                            if user_resolutions:
                                recovery_requires_user = True
                                decision.verdict = "blocked"
                                decision.action_type = "block_task"
                                decision.violates_user_constraint = any(
                                    item.user_constraint_involved for item in blocking_conflicts
                                )
                                decision.constraint_conflict_reason = conflict_reason
                            elif terminal_resolutions:
                                decision.verdict = "blocked"
                                decision.action_type = "block_task"
                                decision.constraint_conflict_reason = conflict_reason
                            elif probe_resolutions:
                                # A blocking severity means the proposed repair
                                # cannot run yet. It does not mean the task must
                                # terminate when the resolver selected a safe
                                # evidence-gathering probe.
                                if not (
                                    decision.verdict == "probe"
                                    and decision.action_type in {"inspect_file", "inspect_log", "run_probe"}
                                    and decision.commands
                                ):
                                    probe = next(
                                        (
                                            item for item in recommended_probes
                                            if isinstance(item, dict) and str(item.get("command") or "").strip()
                                        ),
                                        {},
                                    )
                                    if probe:
                                        decision.verdict = "probe"
                                        decision.action_type = "run_probe"
                                        decision.commands = [str(probe.get("command"))]
                                        decision.patches = []
                                        decision.rerun_command = None
                                        decision.root_cause = "knowledge_conflict_requires_probe"
                                        decision.expected_new_evidence = [
                                            str(probe.get("information_gain") or conflict_reason)
                                        ]
                                    else:
                                        recovery_requires_user = True
                                        decision.verdict = "blocked"
                                        decision.action_type = "block_task"
                                        decision.constraint_conflict_reason = (
                                            conflict_reason + "; no safe probe command is available"
                                        )
                    except Exception as exc:
                        logger.warning("Knowledge conflict check before repair failed closed: %s", exc)
                        recovery_requires_user = True
                        decision.verdict = "blocked"
                        decision.action_type = "block_task"
                        decision.constraint_conflict_reason = "knowledge_conflict_check_failed_before_repair"

                repair_result: Dict[str, Any] = {"ok": False, "files": []}
                if ENABLE_RECOVERY_PATCHING and decision.verdict == "repair" and decision.patches and recovery_store and data_path:
                    repair_result = RepairExecutor().apply(
                        decision.patches,
                        task_root=recovery_store.root.parent,
                        backup_root=recovery_store.root / "backups",
                        attempt_id=f"attempt_{len(previous_attempts) + 1:03d}",
                    )
                    if not repair_result.get("ok"):
                        recovery_requires_user = True
                        decision.verdict = "rollback"
                        decision.action_type = "block_task"
                        decision.evidence.append(str(repair_result.get("error") or "repair failed and was rolled back"))

                managed_user_interaction = (
                    str((state or {}).get("task_mode") or "managed").strip().lower() == "managed"
                    and not bool((state or {}).get("non_interactive"))
                    and not bool((state or {}).get("disable_user_questions"))
                )
                if decision.verdict in {"blocked", "escalate", "rollback"} and managed_user_interaction:
                    recovery_requires_user = True

                attempt_id = f"attempt_{len(previous_attempts) + 1:03d}"
                action_command = decision.rerun_command or (decision.commands[0] if decision.commands else "")
                durable_plan = load_plan_state(data_path, task_id) if data_path else {}
                action_signature = ActionSignature.create(
                    decision.action_type,
                    action_command,
                    target_paths=[recovery_context.generated_script_path or "", *recovery_context.related_log_paths],
                    hypothesis_ids=[decision.selected_hypothesis_id or ""],
                    patches=decision.patches,
                    environment_name=recovery_context.environment_name,
                    cwd=recovery_context.cwd,
                )
                previous_signature = str(previous_attempts[-1].get("action_signature") or "") if previous_attempts else ""
                independent_evidence_ids = [record.evidence_id for record in analysis_records if record.is_independent]
                deterministic_analysis = (
                    recovery_context.root_cause_analysis
                    if isinstance(recovery_context.root_cause_analysis, dict)
                    else {}
                )
                confirmed_failed_command = bool(
                    str(deterministic_analysis.get("status") or "").lower() == "confirmed"
                    and deterministic_analysis.get("failed_command")
                    and (
                        (deterministic_analysis.get("command_chain") or {}).get("confirmed_failed_node_id")
                        or deterministic_analysis.get("primary_failure")
                    )
                )
                confirmed_primary_error = bool(
                    deterministic_analysis.get("primary_error_message")
                    and any(
                        isinstance(item, dict)
                        and item.get("claim") == "primary_error"
                        and item.get("confirms_runtime_failure")
                        for item in deterministic_analysis.get("evidence_records", []) or []
                    )
                )
                progress_decision = progress_tracker.evaluate(
                    attempt_id=attempt_id,
                    stage_id=recovery_context.stage_id,
                    action_type=decision.action_type,
                    command=action_command,
                    hypotheses=hypothesis_manager.hypotheses,
                    evidence=list(hypothesis_manager.evidence.values()),
                    new_evidence_ids=independent_evidence_ids,
                    failure_fingerprint=recovery_context.error_fingerprint,
                    previous_failure_fingerprint=(previous_attempts[-1].get("failure_fingerprint_before") if previous_attempts else ""),
                    expected_artifacts=len(recovery_context.expected_artifacts),
                    existing_artifacts=len(recovery_context.existing_artifacts),
                    valid_artifacts=len(recovery_context.existing_artifacts),
                    known_failed_command=confirmed_failed_command,
                    known_primary_error=confirmed_primary_error,
                    patch_applied=bool(repair_result.get("ok")),
                    constraint_conflict=bool(conflict),
                    target_paths=[recovery_context.generated_script_path or "", *recovery_context.related_log_paths],
                    environment_name=recovery_context.environment_name,
                    cwd=recovery_context.cwd,
                    hypothesis_ids=[decision.selected_hypothesis_id or ""],
                ) if ENABLE_PROGRESS_DETECTOR else ProgressTracker().evaluate(
                    attempt_id=attempt_id,
                    stage_id=recovery_context.stage_id,
                    action_type=decision.action_type,
                    command=action_command,
                    hypotheses=hypothesis_manager.hypotheses,
                    evidence=list(hypothesis_manager.evidence.values()),
                    new_evidence_ids=independent_evidence_ids,
                    failure_fingerprint=recovery_context.error_fingerprint,
                    known_failed_command=confirmed_failed_command,
                    known_primary_error=confirmed_primary_error,
                )
                if progress_decision.classification in {"regression", "exhausted"}:
                    decision.verdict = "blocked"
                    decision.action_type = "block_task"
                    decision.root_cause = f"recovery_{progress_decision.classification}"
                    decision.evidence.append(progress_decision.reason)
                progress_score = float(progress_decision.progress_score)
                attempt = RecoveryAttempt(
                    attempt_id=attempt_id,
                    strategy_level=min(len(previous_attempts), 5),
                    failure_fingerprint_before=recovery_context.error_fingerprint,
                    stage_id=recovery_context.stage_id,
                    diagnosis_fingerprint=diagnosis_fingerprint,
                    hypothesis_id=decision.selected_hypothesis_id,
                    action_type=decision.action_type,
                    commands=decision.commands,
                    patches=decision.patches,
                    evidence_gained=decision.evidence,
                    failure_fingerprint_after=None,
                    progress_score=progress_score,
                    result=(
                        "no_progress" if progress_decision.classification in {"no_progress", "stalled", "exhausted"}
                        else "new_failure" if decision.verdict in {"blocked", "escalate"}
                        else "same_failure"
                    ),
                    action_signature=action_signature.fingerprint(),
                    evidence_ids=independent_evidence_ids,
                    progress_classification=progress_decision.classification,
                    progress_events=progress_decision.events,
                    forbidden_next_actions=progress_decision.forbidden_next_actions,
                    strategy_change=bool(previous_signature and previous_signature != action_signature.fingerprint()),
                    plan_version=str(durable_plan.get("plan_version") or raw_evidence.get("plan_version") or ""),
                    plan_hash=semantic_plan_hash(durable_plan) if durable_plan.get("steps") else "",
                    step_id=recovery_context.stage_id,
                    command_hash=hashlib.sha256(action_command.encode("utf-8", errors="replace")).hexdigest()[:20] if action_command else "",
                    prompt_projection_hash=hashlib.sha256(diagnosis_prompt.encode("utf-8", errors="replace")).hexdigest()[:20],
                    new_evidence_count=len(independent_evidence_ids),
                )
                previous_attempts.append(asdict(attempt))
                if data_path:
                    durable_plan = load_plan_state(data_path, task_id)
                    task_state = dict(durable_plan.get("task_state") or {})
                    budgets = dict(task_state.get("budgets") or {})
                    fingerprint_counts = dict(budgets.get("recovery_attempts_by_fingerprint") or {})
                    fingerprint_key = str(recovery_context.error_fingerprint or "unknown")
                    fingerprint_counts[fingerprint_key] = int(fingerprint_counts.get(fingerprint_key) or 0) + 1
                    budgets["recovery_attempts_by_fingerprint"] = fingerprint_counts
                    budgets["remaining_recovery_budget"] = max(0, 3 - fingerprint_counts[fingerprint_key])
                    task_state["budgets"] = budgets
                    durable_plan["task_state"] = task_state
                    if decision.action_type in {"inspect_file", "inspect_log", "run_probe"}:
                        increment_plan_budget(durable_plan, "diagnostic_probe_count")
                    elif decision.action_type in {"patch_file", "replace_command", "regenerate_script"}:
                        increment_plan_budget(durable_plan, "script_patch_count")
                    save_plan_state(data_path, task_id, durable_plan)
                # A probe gathers evidence; it is not a repaired artifact run.
                # Artifact verification applies only to a concrete repair/rerun.
                if decision.verdict not in {"repair", "rerun", "rollback"}:
                    decision.verification_checks = []
                else:
                    decision.verification_checks = [
                        {
                            **dict(check),
                            "_recovery_step_id": recovery_context.stage_id,
                        }
                        for check in decision.verification_checks
                        if isinstance(check, dict)
                    ]

                if recovery_store:
                    recovery_store.save_diagnostic_state(hypothesis_manager)
                    recovery_store.save_progress(progress_tracker)
                    recovery_store.save_diagnostic_summary(
                        hypothesis_manager,
                        progress_tracker,
                        failure_fingerprint=recovery_context.error_fingerprint,
                        recovery_budget={"attempts": len(previous_attempts)},
                        forbidden_next_actions=progress_decision.forbidden_next_actions,
                    )
                    recovery_store.save_attempt(attempt)
                    recovery_store.append_trace("DIAGNOSE", {"decision": decision.to_dict(), "attempt": asdict(attempt), "progress": asdict(progress_decision), "repair": repair_result})
                    if recovery_requires_user:
                        recovery_phase = RecoveryPhase.DIAGNOSE.value
                    elif decision.verdict == "rollback":
                        recovery_phase = RecoveryPhase.ROLLBACK.value
                    elif repair_result.get("ok") or decision.verdict == "rerun":
                        recovery_phase = RecoveryPhase.RERUN.value
                    elif decision.verdict == "probe":
                        recovery_phase = RecoveryPhase.PROBE.value
                    elif decision.verdict == "repair":
                        recovery_phase = RecoveryPhase.APPLY_REPAIR.value
                    elif decision.verdict == "blocked":
                        recovery_phase = RecoveryPhase.BLOCKED.value
                    else:
                        recovery_phase = RecoveryPhase.DIAGNOSE.value
                    recovery_store.save_status({
                        "phase": recovery_phase,
                        "status": (
                            "NEEDS_USER_DECISION"
                            if recovery_requires_user
                            else "REPAIR_APPLIED" if repair_result.get("ok") else decision.verdict.upper()
                        ),
                        "failure_fingerprint": recovery_context.error_fingerprint,
                        "attempt_id": attempt.attempt_id,
                        "pending_rerun": bool(repair_result.get("ok") or decision.verdict == "rerun"),
                    })
                    if decision.verification_checks:
                        recovery_store.save_verification({
                            "status": "pending_after_rerun",
                            "checks": decision.verification_checks,
                            "attempt_id": attempt.attempt_id,
                        })

                # --- D. 注入修正后的上下文 ---
                # 我们修改 result 中的 messages，追加诊断信息
                # 这样主 Agent 在下一次 generate 时，就会看到错误 + 专家的修复建议
                augmented_observation = (
                    last_msg
                    + f"\n\n<failure_context>\n{json.dumps(recovery_context.llm_view(), ensure_ascii=False)}\n</failure_context>"
                    + f"\n\n<recovery_decision>\n{json.dumps(decision.to_dict(), ensure_ascii=False)}\n</recovery_decision>"
                    + f"\n\n<system_diagnosis>\n{diagnosis_text}\n</system_diagnosis>"
                )
                pending_type = (
                    "ask_user" if recovery_requires_user
                    else "blocked" if decision.verdict in {"blocked", "escalate"}
                    else "rerun_stage" if (repair_result.get("ok") or decision.verdict == "rerun")
                    else decision.action_type
                )
                pending_action = {
                    "type": pending_type,
                    "command": decision.rerun_command or (decision.commands[0] if decision.verdict == "rerun" and decision.commands else ""),
                    "reason": decision.root_cause,
                    "verification_checks": decision.verification_checks,
                    "do_not_repeat": decision.commands if decision.verdict in {"probe", "rerun"} else [],
                    "progress": asdict(progress_decision),
                    "forbidden_next_actions": progress_decision.forbidden_next_actions,
                    "strategy_change_required": progress_decision.recommended_mode != "continue",
                    "evidence_state": evidence_state,
                    "failure_scope": (
                        "CURRENT_SCRIPT"
                        if recovery_context.generated_script_path
                        else "CURRENT_COMMAND"
                    ),
                    "step_id": recovery_context.stage_id,
                    "plan_version": str(durable_plan.get("plan_version") or ""),
                }
                if pending_type == "ask_user":
                    pending_action.update({
                        "question": (
                            "Automatic recovery cannot continue safely without your decision. "
                            + str(decision.constraint_conflict_reason or decision.root_cause)
                            + " Please provide a different recovery direction or confirm that this stage should be recorded as blocked."
                        ),
                        "resume_condition": "user decision",
                    })
                elif pending_type == "blocked":
                    pending_action.update({
                        "blocking_reason": decision.root_cause,
                        "evidence_ids": independent_evidence_ids,
                        "attempted_probes": [
                            item.get("commands", []) for item in previous_attempts
                            if item.get("action_type") in {"run_probe", "inspect_file", "inspect_log"}
                        ],
                        "remaining_safe_probes": [],
                        "resume_condition": "new user input, environment change, or an explicitly different safe recovery strategy",
                    })
                augmented_observation += (
                    f"\n\n<recovery_next_action>\n{json.dumps(pending_action, ensure_ascii=False)}\n"
                    "Generate must carry out this repair/rerun or produce a truthful blocked status; "
                    "do not emit another equivalent read-only inspection.\n</recovery_next_action>"
                )
                
                # 更新返回结果
                result["messages"] = [HumanMessage(content=augmented_observation)]
                if recovery_requires_user:
                    result["recovery_phase"] = RecoveryPhase.DIAGNOSE.value
                elif decision.verdict == "rollback":
                    result["recovery_phase"] = RecoveryPhase.ROLLBACK.value
                elif repair_result.get("ok") or decision.verdict == "rerun":
                    result["recovery_phase"] = RecoveryPhase.RERUN.value
                elif decision.verdict == "probe":
                    result["recovery_phase"] = RecoveryPhase.PROBE.value
                elif decision.verdict == "repair":
                    result["recovery_phase"] = RecoveryPhase.APPLY_REPAIR.value
                elif decision.verdict == "blocked":
                    result["recovery_phase"] = RecoveryPhase.BLOCKED.value
                else:
                    result["recovery_phase"] = RecoveryPhase.DIAGNOSE.value
                result["recovery_status"] = (
                    "NEEDS_USER_DECISION"
                    if recovery_requires_user
                    else "REPAIR_APPLIED" if repair_result.get("ok") else decision.verdict.upper()
                )
                result["recovery_context"] = recovery_context.llm_view()
                result["root_cause_analysis"] = recovery_context.root_cause_analysis
                result["command_chain"] = recovery_context.command_chain
                result["recovery_decision"] = decision.to_dict()
                result["recovery_hypotheses"] = hypothesis_manager.to_list()
                result["recovery_attempts"] = previous_attempts
                result["verification_checks"] = decision.verification_checks
                result["pending_recovery_action"] = pending_action
                if pending_type == "ask_user":
                    result["pending_human_question"] = {
                        "type": "recovery_user_decision",
                        "stage": "recovery",
                        "question": pending_action["question"],
                        "resume_step": "generate",
                    }
                result["recovery_progress"] = asdict(progress_decision)
                result["diagnostic_summary"] = {
                    "active_hypotheses": [asdict(item) for item in hypothesis_manager.hypotheses if item.status not in {"rejected", "superseded", "unresolvable"}],
                    "confirmed_hypotheses": [item.hypothesis_id for item in hypothesis_manager.hypotheses if item.status == "confirmed"],
                    "latest_progress": asdict(progress_decision),
                    "evidence_state": evidence_state,
                }
                result["forbidden_recovery_actions"] = progress_decision.forbidden_next_actions
                # Generate must either execute the declared rerun/probe, apply a
                # different repair, or write a truthful blocked stage artifact.
                result["next_step"] = (
                    "ask_user" if pending_type == "ask_user"
                    else "finish" if pending_type == "blocked"
                    else "generate"
                )
                
            return result

        @functools.wraps(func)
        async def wrapper(state, *args, **kwargs):
            result_holder: Dict[str, Any] = {}
            try:
                return await _recover(
                    state,
                    *args,
                    _result_holder=result_holder,
                    **kwargs,
                )
            except Exception as exc:
                # The executor itself has no result to preserve in this case.
                # Let its normal node-level error handling remain authoritative.
                if "result" not in result_holder:
                    raise

                result = result_holder["result"]
                if not isinstance(result, dict):
                    raise
                task_id = str(
                    (state or {}).get("task_id")
                    or result.get("task_id")
                    or "default"
                ) if isinstance(state, dict) else str(result.get("task_id") or "default")
                preserved_evidence = result.get("execution_evidence")
                if not isinstance(preserved_evidence, dict):
                    preserved_evidence = {}
                failed_step_id = str(
                    preserved_evidence.get("step_id")
                    or (state or {}).get("current_step_id")
                    or ""
                ) if isinstance(state, dict) else ""
                detail = f"{type(exc).__name__}: {clip_text(str(exc), 800)}"
                logger.exception(
                    "Recovery middleware internal failure after Execute result: task=%s step=%s",
                    task_id,
                    failed_step_id,
                )

                pending_action = {
                    "type": "recovery_internal_error",
                    "reason": detail,
                    "step_id": failed_step_id,
                    "required": "Supervisor must review the preserved execution evidence before another recovery attempt.",
                    "resume_condition": "recovery middleware contract is repaired or Supervisor supplies a structured retry context",
                }
                existing_messages = result.get("messages")
                messages = (
                    list(existing_messages)
                    if isinstance(existing_messages, (list, tuple))
                    else ([existing_messages] if existing_messages is not None else [])
                )
                messages.append(SystemMessage(content=(
                    "<recovery_internal_error>\n"
                    f"task_id={task_id}\n"
                    f"step_id={failed_step_id}\n"
                    f"error={detail}\n"
                    "The original Execute result and execution_evidence were preserved. "
                    "Route to Supervisor; do not execute an unvalidated recovery command.\n"
                    "</recovery_internal_error>"
                )))
                result["messages"] = messages
                result["recovery_phase"] = RecoveryPhase.DIAGNOSE.value
                result["recovery_status"] = "RECOVERY_INTERNAL_ERROR"
                result["pending_recovery_action"] = pending_action
                result["next_step"] = "supervisor"

                if data_path:
                    try:
                        store = RecoveryStateStore(data_path, task_id)
                        status = {
                            "phase": RecoveryPhase.DIAGNOSE.value,
                            "status": "RECOVERY_INTERNAL_ERROR",
                            "reason": detail,
                            "step_id": failed_step_id,
                        }
                        store.save_status(status)
                        store.append_trace("RECOVERY_INTERNAL_ERROR", status)
                    except Exception as persist_exc:  # noqa: BLE001
                        logger.error(
                            "Failed to persist recovery internal error: task=%s detail=%s",
                            task_id,
                            persist_exc,
                        )
                return result
        return wrapper
    return decorator


def error_recovery_middleware(llm, retriever=None, skill_manager=None, data_path=None):
    """Attach structured, judge-only recovery to one Execute node.

    The graph controller owns repair generation, plan changes, user questions,
    and CompletionGate. This middleware only captures evidence, evaluates the
    trigger, obtains a code-free RecoveryDecision, and returns that decision.
    """

    def decorator(func):
        async def _structured_wrapper(state, *args, _result_holder=None, **kwargs):
            result = await func(state, *args, **kwargs)
            if _result_holder is not None:
                _result_holder["result"] = result
            if not isinstance(result, dict):
                return result

            state_map = dict(state or {}) if isinstance(state, dict) else {}
            task_id = str(state_map.get("task_id") or result.get("task_id") or "default")
            evidence = result.get("execution_evidence") or state_map.get("execution_evidence") or {}
            evidence = dict(evidence) if isinstance(evidence, dict) else {}

            # Legacy checkpoints may only contain the display observation.
            messages = list(result.get("messages") or [])
            last_content = str(getattr(messages[-1], "content", messages[-1]) or "") if messages else ""
            if not evidence and "❌" in last_content:
                stdout = ""
                stderr = last_content
                if "STDOUT:" in last_content:
                    stream_text = last_content.split("STDOUT:", 1)[1]
                    if "STDERR:" in stream_text:
                        stdout, stderr = stream_text.split("STDERR:", 1)
                    else:
                        stdout, stderr = stream_text, ""
                evidence = {
                    "execution_id": f"legacy-{task_id}",
                    "execution_status": "failed",
                    "returncode": 1,
                    "stderr_summary": clip_text(stderr, 2000),
                    "stdout_summary": clip_text(stdout, 2000),
                    "command_role": "primary",
                    "step_id": str(state_map.get("current_step_id") or ""),
                }
            if not evidence:
                return result

            store = RecoveryStateStore(data_path, task_id) if data_path else None
            history = store.load_attempts() if store else list(state_map.get("recovery_attempts") or [])
            if store:
                current_execution_id = str(evidence.get("execution_id") or "")
                history.extend(
                    item for item in store.load_execution_records()
                    if str(item.get("execution_id") or "") != current_execution_id
                )
            merged_state = {**state_map, **result}
            plan_state = load_plan_state(data_path, task_id) if data_path else {}
            loaded_progress = store.load_progress() if store else None
            progress_tracker = (
                loaded_progress
                if isinstance(loaded_progress, ActiveProgressTracker)
                else ActiveProgressTracker()
            )
            progress = build_progress_evidence(
                merged_state,
                evidence,
                progress_tracker,
            )
            merged_state["recovery_progress"] = progress
            result["recovery_progress"] = progress
            result["progress_snapshot"] = progress
            result["progress_evidence"] = progress
            if store:
                store.save_progress(progress_tracker)
            trigger = evaluate_recovery_trigger(
                merged_state,
                evidence,
                recovery_history=history,
            )
            result["recovery_trigger"] = trigger.to_dict()
            if not trigger.required:
                return result

            active_skills = _selected_skill_ids(merged_state)
            skill_policy = {
                "skill_ids": active_skills,
                "relevant_context": clip_text(
                    _skill_error_context(skill_manager, merged_state, max_chars=3000),
                    3000,
                ),
            }
            task_root = Path(data_path) / "task_outputs" / task_id if data_path else None
            diagnostic_context = build_recovery_context(
                merged_state,
                evidence,
                plan_state=plan_state,
                task_root=task_root,
                skill_policy=skill_policy,
                recovery_history=history,
            )
            prompt_context = diagnostic_context.prompt_view()
            diagnosis = dict(prompt_context.get("diagnosis") or {})
            decision: RecoveryDecision
            judge_needed = diagnosis_required(trigger, evidence, merged_state)

            try:
                evidence_returncode = int(
                    evidence.get("returncode")
                    if evidence.get("returncode") is not None
                    else evidence.get("exit_code")
                )
            except (TypeError, ValueError):
                evidence_returncode = None
            structured_execution_failed = (
                evidence_returncode not in {None, 0}
                or str(evidence.get("execution_status") or "").lower() == "failed"
            )

            if diagnosis.get("collection_complete") and not judge_needed and (
                structured_execution_failed or diagnosis.get("evidence_sufficient")
            ):
                # The builder has already read the persisted script, trace,
                # streams, runtime contract, and manifest. Do not ask an LLM
                # to invent another shell probe for locally available facts.
                decision = recovery_decision_from_diagnosis(diagnosis)
            elif judge_needed:
                judge_prompt = PromptManager.get_recovery_judge_prompt(prompt_context)
                cached_judge = (
                    store.load_llm_result(diagnostic_context.context_id, "recovery_judge")
                    if store else {}
                )
                cached_judge_valid = False
                if cached_judge.get("parsed_result"):
                    try:
                        decision = RecoveryDecision.from_mapping(cached_judge["parsed_result"])
                        cached_judge_valid = True
                    except (TypeError, ValueError):
                        cached_judge_valid = False
                if cached_judge and not cached_judge_valid:
                    # A persisted invalid result still consumed this attempt's
                    # single RecoveryJudge call. Replaying the same context
                    # must escalate deterministically instead of calling the
                    # model again with identical evidence.
                    decision = recovery_decision_from_diagnosis({
                        "root_cause": "The cached RecoveryJudge result is invalid for this unchanged recovery context.",
                        "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                        "failure_type": "recovery_judge_schema_error",
                        "evidence": [
                            *trigger.reasons,
                            f"recovery_context_id:{diagnostic_context.context_id}",
                        ],
                        "confidence": 1.0,
                        "missing_evidence": [],
                        "repair_strategy": "Repair the internal decision protocol without another LLM call.",
                        "next_action": RecoveryAction.ESCALATE.value,
                        "requires_user_input": False,
                        "evidence_sufficient": True,
                    })
                elif not cached_judge_valid:
                    if data_path:
                        increment_plan_budget(plan_state, "recovery_judge_count")
                        save_plan_state(data_path, task_id, plan_state)
                    try:
                        judge_message, _ = await ainvoke_with_snapshot(
                            llm,
                            [HumanMessage(content=judge_prompt)],
                            data_path=data_path,
                            task_id=task_id,
                            metadata={
                                "node_name": "recovery_judge",
                                "call_kind": "recovery_judge",
                                "step_id": str(evidence.get("step_id") or ""),
                                "action_id": str(evidence.get("action_id") or ""),
                                "attempt_id": str(evidence.get("attempt_id") or ""),
                                "recovery_context_ref": str(
                                    Path(data_path) / "task_outputs" / task_id / "recovery" / "recovery_context.json"
                                ) if data_path else "",
                            },
                        )
                        judge_text = str(getattr(judge_message, "content", judge_message) or "")
                        decision = parse_recovery_judge_decision(judge_text)
                        if store:
                            store.save_llm_result(
                                diagnostic_context.context_id,
                                "recovery_judge",
                                raw_response=judge_text,
                                parsed_result=decision.to_dict(),
                            )
                    except Exception as judge_exc:
                        if store:
                            store.save_llm_result(
                                diagnostic_context.context_id,
                                "recovery_judge",
                                raw_response=str(getattr(locals().get("judge_message"), "content", "") or ""),
                                parsed_result={},
                            )
                        decision = recovery_decision_from_diagnosis({
                            "root_cause": "RecoveryJudge returned an invalid result; preserved evidence requires internal escalation.",
                            "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                            "failure_type": "recovery_judge_schema_error",
                            "evidence": [*trigger.reasons, clip_text(str(judge_exc), 300)],
                            "confidence": 1.0,
                            "missing_evidence": [],
                            "repair_strategy": "Repair the internal decision protocol without involving the user.",
                            "next_action": RecoveryAction.ESCALATE.value,
                            "requires_user_input": False,
                            "evidence_sufficient": True,
                        })
            else:
                decision = deterministic_recovery_decision(trigger, evidence)

            decision = constrain_recovery_decision(
                decision,
                diagnosis,
                user_decision_supported=structured_user_decision_required(merged_state),
            )
            decision = contextualize_recovery_decision(
                decision,
                action_id=str(evidence.get("action_id") or evidence.get("execution_id") or ""),
                step_id=str(evidence.get("step_id") or state_map.get("current_step_id") or ""),
                attempt_id=str(evidence.get("attempt_id") or evidence.get("execution_id") or ""),
                source_node="execute",
            )
            decision = RecoveryPolicy().constrain_with_progress(
                decision,
                progress,
                dict((plan_state.get("task_state") or {}).get("budgets") or {}),
            )
            decision = contextualize_recovery_decision(decision, source_node="execute")

            application = None
            if data_path:
                application = apply_recovery_decision(plan_state, decision)
                save_plan_state(data_path, task_id, plan_state)

            if store:
                store.save_diagnostic_context(diagnostic_context)
                store.save_recovery_decision(decision)
                store.save_attempt_mapping({
                    "attempt_id": diagnostic_context.attempt_id,
                    "step_id": diagnostic_context.step_id,
                    "action_type": str(evidence.get("action_kind") or evidence.get("command_role") or ""),
                    "command_role": str(evidence.get("command_role") or ""),
                    "strategy_category": decision.recommended_action,
                    "error_fingerprint": diagnostic_context.error_fingerprint,
                    "returncode": evidence_returncode,
                    "validation_status": str(
                        (prompt_context.get("validation") or {}).get("status") or ""
                    ),
                    "recovery_decision": decision.to_dict(),
                    "manifest_delta": progress.get("manifest_delta") or {},
                    "artifact_delta": progress.get("artifact_delta") or {},
                    "meaningful_progress": bool(progress.get("meaningful_progress")),
                    "progress_classification": progress.get("classification") or "",
                })
                store.append_trace("RECOVERY_JUDGE", {
                    "trigger": trigger.to_dict(),
                    "decision": decision.to_dict(),
                })

            action = decision.recommended_action
            interaction_allowed = not bool(state_map.get("non_interactive") or state_map.get("disable_user_questions"))
            route = recovery_action_route(
                action,
                interaction_allowed=interaction_allowed,
                failure_owner=decision.failure_owner,
                requires_user_input=decision.requires_user_input,
            )
            next_step = decision.next_route
            pending_type = route["pending_type"]
            required_next_action = route["required_next_action"]
            recovery_scope = route["recovery_scope"]

            pending_action = {
                "type": pending_type,
                "reason": decision.reason or decision.root_cause,
                "step_id": str(evidence.get("step_id") or state_map.get("current_step_id") or ""),
                "failure_class": decision.failure_class,
                "failure_owner": decision.failure_owner,
                "recommended_action": action,
                "evidence": list(decision.evidence),
                "failure_type": decision.failure_type,
                "missing_evidence": list(decision.missing_evidence),
                "repair_strategy": decision.repair_strategy,
                "requires_user_input": decision.requires_user_input,
                "diagnostic_context_path": str(store.root / "recovery_context.json") if store else "",
                "do_not_repeat": [str(evidence.get("code_hash") or "")] if trigger.same_failure_count >= 2 else [],
            }
            result.update({
                "execution_evidence": evidence,
                "diagnostic_context": prompt_context,
                "recovery_context": prompt_context,
                "recovery_context_digest": diagnostic_context.context_id,
                "current_step_context": dict(prompt_context.get("current_step") or {}),
                "recovery_decision": decision.to_dict(),
                "diagnostic_summary": diagnosis,
                "structured_diagnosis": diagnosis,
                "failure_owner": decision.failure_owner,
                "recovery_history": list(prompt_context.get("recent_attempts") or []),
                "failure_fingerprint": str(
                    merged_state.get("failure_fingerprint")
                    or diagnostic_context.error_fingerprint
                    or ""
                ),
                "pending_recovery_action": pending_action,
                "required_next_action": required_next_action,
                "recovery_scope": recovery_scope,
                "evidence_status": "sufficient" if decision.evidence_sufficient else "insufficient",
                "retry_reason": decision.reason or decision.root_cause,
                "recovery_phase": "diagnose",
                "recovery_status": action,
                "recovery_application": application or {},
                "current_step_id": str((application or {}).get("current_step_id") or ""),
                "current_stage_id": str((application or {}).get("current_stage_id") or ""),
                # Compatibility mirror only. Graph routers consume
                # recovery_decision.kind/next_route as the authority.
                "next_step": decision.next_route,
            })
            if next_step == "ask_user":
                question = (
                    "BioMind cannot safely continue this step without your decision. "
                    + (decision.reason or decision.root_cause)
                )
                result["pending_human_question"] = {
                    "type": "recovery_user_decision",
                    "stage": "recovery",
                    "question": question,
                    "resume_step": decision.next_route or "generate",
                }
                pending_action["question"] = question
            result.setdefault("messages", []).append(SystemMessage(content=(
                "<structured_recovery>"
                + json.dumps({
                    "failure_class": decision.failure_class,
                    "failure_owner": decision.failure_owner,
                        "recommended_action": action,
                        "decision_kind": decision.kind,
                        "decision_id": decision.decision_id,
                        "next_route": decision.next_route,
                    "reason": decision.reason or decision.root_cause,
                    "evidence_sufficient": decision.evidence_sufficient,
                }, ensure_ascii=False)
                + "</structured_recovery>"
            )))
            return result

        @functools.wraps(func)
        async def wrapper(state, *args, **kwargs):
            holder: Dict[str, Any] = {}
            try:
                return await _structured_wrapper(
                    state,
                    *args,
                    _result_holder=holder,
                    **kwargs,
                )
            except Exception as exc:
                if "result" not in holder or not isinstance(holder["result"], dict):
                    raise
                result = holder["result"]
                evidence = result.get("execution_evidence") or {}
                detail = f"{type(exc).__name__}: {clip_text(str(exc), 800)}"
                logger.exception("Structured recovery failed after Execute; preserving evidence")
                result.setdefault("messages", []).append(SystemMessage(content=(
                    "<recovery_internal_error>"
                    + json.dumps({
                        "error": detail,
                        "step_id": evidence.get("step_id") if isinstance(evidence, dict) else "",
                        "execution_id": evidence.get("execution_id") if isinstance(evidence, dict) else "",
                    }, ensure_ascii=False)
                    + "</recovery_internal_error>"
                )))
                decision = RecoveryDecision.from_mapping({
                    "schema_version": 3,
                    "kind": RecoveryDecisionKind.ESCALATE.value,
                    "recommended_action": RecoveryAction.ESCALATE.value,
                    "reason_code": "recovery_middleware_internal_error",
                    "reason": detail,
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "evidence": [detail],
                    "source_node": "execute",
                    "action_id": str(evidence.get("action_id") or evidence.get("execution_id") or "") if isinstance(evidence, dict) else "",
                    "step_id": str(evidence.get("step_id") or "") if isinstance(evidence, dict) else "",
                    "attempt_id": str(evidence.get("attempt_id") or evidence.get("execution_id") or "") if isinstance(evidence, dict) else "",
                })
                application = {}
                if data_path:
                    fallback_task_id = str((state or {}).get("task_id") or result.get("task_id") or "default")
                    fallback_plan = load_plan_state(data_path, fallback_task_id)
                    application = apply_recovery_decision(fallback_plan, decision)
                    save_plan_state(data_path, fallback_task_id, fallback_plan)
                result.update({
                    "recovery_phase": "diagnose",
                    "recovery_status": "RECOVERY_INTERNAL_ERROR",
                    "recovery_decision": decision.to_dict(),
                    "recovery_application": application,
                    "current_step_id": str(application.get("current_step_id") or ""),
                    "current_stage_id": str(application.get("current_stage_id") or ""),
                    "pending_recovery_action": {
                        "type": "recovery_internal_error",
                        "reason": detail,
                        "required": "Supervisor review of preserved execution evidence",
                    },
                    "next_step": decision.next_route,
                })
                return result

        return wrapper

    return decorator
