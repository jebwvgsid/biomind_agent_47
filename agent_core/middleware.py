import re
import functools
import logging
from typing import Tuple, Dict, Any
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


def error_recovery_middleware(llm, retriever):
    """
    错误恢复中间件装饰器工厂
    :param llm: 用于生成修复方案的 LLM 实例
    :param retriever: 用于检索 RAG 的检索器实例
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

                # --- B. RAG 检索 (查病历) ---
                rag_advice = ""
                if retriever and KB_MAP.get("ERRORS"):
                    try:
                        print(f"{Fore.YELLOW}📚 Querying RAG for solution...{Style.RESET_ALL}")
                        docs = retriever.retrieve(query=error_fingerprint, target_datasets=[KB_MAP["ERRORS"]])
                        if docs:
                            rag_advice = f"\n\n### 🧠 RAG Knowledge Base Advice:\n{docs}"
                            print(f"{Fore.GREEN}✓ Found relevant solution in KB.{Style.RESET_ALL}")
                    except Exception as e:
                        print(f"RAG Error: {e}")

                # --- C. LLM 诊断 (开处方) ---
                # 我们不直接修改代码，而是生成一个“专家会诊意见”，注入到对话历史中
                # 让下一个 Generate 节点基于这个意见去重写代码
                diagnosis_prompt = f"""
Analyze the following execution error:
<error>
STDOUT:
{clip_text(stdout_content, 1200)}

STDERR:
{clip_text(stderr_content, 1200)}
</error>

{clip_text(rag_advice, 1200)}

Task:
1. Explain why it failed.
2. Provide the CORRECTED code block immediately.
{env_hint}
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
