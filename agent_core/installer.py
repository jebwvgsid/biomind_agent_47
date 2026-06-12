import re
import logging
import shlex
from typing import Any, Dict, Literal
from pathlib import Path
from datetime import datetime

# LangGraph & LangChain Imports
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver 
from langgraph.types import interrupt
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

# Local Imports
from agent_core.state import InstallerState
from agent_core.middleware import HITLMiddleware
from agent_core.llm_safety import is_llm_input_rejected, llm_failure_summary
from package_manager import PackageManager
from execution.executor import detect_script_language, normalize_script_code, run_bash_script, run_python_repl
from prompts.manager import PromptManager

# RAG Imports
from .retriever import RAGFlowRetriever
from config import (
    RAGFLOW_ADDRESS, 
    RAGFLOW_API_KEY, 
    KB_MAP, 
    RERANK_MODEL_ID, 
    BIO_SOFTWARE_ROOT,
    BIO_SOURCE_INSTALL_DIR,
    ANACONDA_ROOT
)

logger = logging.getLogger("BioMind.Installer")

class InstallerAgent:
    MAX_INSTALLER_GENERATE_TURNS = 10
    MAX_REPEATED_OBSERVATIONS = 3

    def __init__(self, llm, work_dir: str = "./data", package_manager=None):
        self.llm = llm
        if package_manager:
            self.pm = package_manager
        else:
            self.pm = PackageManager(work_dir=work_dir)
        self.checkpointer = MemorySaver()
        self.middleware = HITLMiddleware(auto_approve_safe=True)

        # 1. 初始化 RAG
        self.retriever = None
        if RAGFLOW_API_KEY:
            try:
                self.retriever = RAGFlowRetriever(
                    api_key=RAGFLOW_API_KEY,
                    base_url=RAGFLOW_ADDRESS,
                    dataset_ids=[], 
                    rerank_id=RERANK_MODEL_ID
                )
            except Exception as e:
                logger.warning(f"Installer RAG init failed: {e}")

        self.graph = self._build_graph()

    def _is_low_friction_installer_command(self, code: str, language: str) -> bool:
        if language != "Bash":
            return False
        stripped = code.strip()
        if not stripped:
            return False
        lines = [
            line.strip() for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            return False
        dangerous_patterns = [
            r"\brm\b",
            r"\bmv\b",
            r"\bchmod\b",
            r"\bchown\b",
            r"\bsudo\b",
            r"\bwget\b",
            r"\bcurl\b",
            r"\bgit\s+clone\b",
            r"\bscp\b",
            r"\bssh\b",
        ]
        if any(re.search(pattern, stripped, re.IGNORECASE) for pattern in dangerous_patterns):
            return False
        allowed_prefixes = (
            "conda run -n ",
            "mamba run -n ",
            "echo ",
            "which ",
            "pwd",
            "ls ",
        )
        return all(line.startswith(allowed_prefixes) or line == "pwd" for line in lines)

    def _installer_generate_turns(self, state: InstallerState) -> int:
        return sum(1 for msg in state.get("messages", []) if isinstance(msg, AIMessage))

    def _installer_observations(self, state: InstallerState) -> list[str]:
        observations = []
        for msg in state.get("messages", []):
            content = str(getattr(msg, "content", "") or "").strip()
            if isinstance(msg, HumanMessage) and "<observation>" in content:
                observations.append(content)
        return observations

    def _repeated_installer_observation_count(self, state: InstallerState) -> int:
        observations = self._installer_observations(state)
        if not observations:
            return 0
        last = re.sub(r"\s+", " ", observations[-1]).strip()
        count = 0
        for obs in reversed(observations):
            normalized = re.sub(r"\s+", " ", obs).strip()
            if normalized != last:
                break
            count += 1
        return count

    def _installer_failure_reason(self, state: InstallerState) -> str:
        turns = self._installer_generate_turns(state)
        repeated = self._repeated_installer_observation_count(state)
        if turns >= self.MAX_INSTALLER_GENERATE_TURNS:
            return f"installer exceeded {self.MAX_INSTALLER_GENERATE_TURNS} generate turns without successful registration."
        if repeated >= self.MAX_REPEATED_OBSERVATIONS:
            return f"installer repeated the same observation {repeated} times without progress."
        observations = self._installer_observations(state)
        if observations:
            return observations[-1][:500]
        return "installer stopped before successful verification and registration."

    def _normalize_install_command_for_contract(self, command: str) -> str:
        text = str(command or "").replace("\\\n", " ").strip()
        text = re.sub(r"\s+", " ", text)
        return text.rstrip(";")

    def _strict_runtime_contract(self, state: InstallerState) -> Dict[str, Any]:
        text = "\n".join(str(getattr(msg, "content", "") or "") for msg in state.get("messages", []))
        matches = re.findall(
            r"\[STRICT SKILL RUNTIME CONTRACT\](.*?)\[/STRICT SKILL RUNTIME CONTRACT\]",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not matches:
            return {"active": False, "allowed": []}
        block = matches[-1]
        allowed = [
            self._normalize_install_command_for_contract(command)
            for command in re.findall(r"allowed_install_command:\s*(.+)", block)
        ]
        allowed = [command for command in allowed if command]
        metadata = self._selected_skill_install_metadata(text)
        metadata.update({"active": bool(allowed), "allowed": list(dict.fromkeys(allowed))})
        return metadata

    def _last_contract_value(self, text: str, key: str) -> str:
        matches = re.findall(
            rf"^\s*-\s*{re.escape(key)}:\s*(.*?)\s*$",
            str(text or ""),
            re.MULTILINE,
        )
        return matches[-1].strip() if matches else ""

    def _split_contract_list(self, value: str) -> list[str]:
        return [part.strip() for part in re.split(r"[,\n;]+", str(value or "")) if part.strip() and part.strip().lower() != "none"]

    def _selected_skill_install_metadata(self, text: str) -> Dict[str, Any]:
        packages = self._split_contract_list(
            self._last_contract_value(text, "packages")
            or self._last_contract_value(text, "requested packages")
        )
        capabilities = self._split_contract_list(
            self._last_contract_value(text, "capabilities")
            or self._last_contract_value(text, "requested capabilities")
        )
        return {
            "selected_skills": self._split_contract_list(self._last_contract_value(text, "selected_skills")),
            "target_env": self._last_contract_value(text, "target_env"),
            "language": self._last_contract_value(text, "language"),
            "packages": packages,
            "capabilities": capabilities,
            "healthcheck": self._last_contract_value(text, "healthcheck"),
            "install_policy": self._last_contract_value(text, "install_policy"),
        }

    def _command_lines(self, code: str) -> list[str]:
        return [
            line.strip()
            for line in str(code or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _declared_install_like_command(self, code: str, contract: Dict[str, Any]) -> str:
        if not contract.get("active"):
            return ""
        allowed = set(contract.get("allowed") or [])
        normalized_code = self._normalize_install_command_for_contract(code)
        if normalized_code in allowed and self._is_install_like_command(normalized_code):
            return normalized_code
        for line in self._command_lines(code):
            normalized = self._normalize_install_command_for_contract(line)
            if normalized in allowed and self._is_install_like_command(line):
                return normalized
        return ""

    def _verify_and_register_contract_runtime(
        self,
        contract: Dict[str, Any],
        tool_name: str,
        attempted_command: str,
    ) -> str:
        if not contract.get("active"):
            return ""
        env = str(contract.get("target_env") or "").strip()
        healthcheck = str(contract.get("healthcheck") or "").strip()
        if not env or not healthcheck:
            return ""
        selected_skills = list(contract.get("selected_skills") or [])
        skill_id = selected_skills[0] if selected_skills else None
        language = str(contract.get("language") or "").strip()
        packages = contract.get("packages") or []
        capabilities = contract.get("capabilities") or []

        try:
            self.pm.register_environment(
                env_name=env,
                purpose=f"Runtime for {', '.join(selected_skills) if selected_skills else tool_name}",
                packages=packages,
                skill_id=skill_id,
                notes="Registered from strict skill runtime contract before healthcheck.",
                language=language,
                capabilities=capabilities,
                healthcheck=healthcheck,
                status="registered",
                tool_name=tool_name,
            )
            ok, detail = self.pm.verify_environment(
                env_name=env,
                language=language,
                healthcheck=healthcheck,
            )
        except Exception as exc:
            return (
                "<observation>❌ Strict runtime healthcheck could not run after declared install command.\n"
                f"Command: {attempted_command}\n"
                f"Healthcheck: {healthcheck}\n"
                f"Error: {exc}</observation>"
            )

        if ok:
            return (
                "<observation>✅ Declared runtime healthcheck passed.\n"
                f"Healthcheck: {healthcheck}\n"
                f"Detail: {str(detail or 'OK')[:500]}\n"
                f"Environment '{env}' registered successfully via PackageManager.</observation>"
            )
        return (
            "<observation>❌ Strict runtime healthcheck failed after declared install command.\n"
            f"Command: {attempted_command}\n"
            f"Healthcheck: {healthcheck}\n"
            f"Detail: {str(detail or 'Healthcheck failed.')[:700]}\n"
            "If another allowed_install_command remains, use it once; otherwise stop and report this failure.</observation>"
        )

    def _is_install_like_command(self, line: str) -> bool:
        return bool(re.search(
            r"\b(?:conda|mamba)\s+(?:install|create|env\s+(?:create|update))\b"
            r"|\b(?:python(?:3)?\s+-m\s+)?pip\s+install\b"
            r"|install\.packages\s*\("
            r"|install_github\s*\("
            r"|BiocManager::install\s*\(",
            str(line or ""),
            re.IGNORECASE,
        ))

    def _runtime_contract_violation(self, code: str, language: str, state: InstallerState) -> str:
        if language != "Bash":
            return ""
        contract = self._strict_runtime_contract(state)
        if not contract.get("active"):
            return ""
        allowed = set(contract.get("allowed", []))
        lines = [
            line.strip()
            for line in str(code or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in lines:
            if contract.get("active") and re.match(r"^cd(?:\s|$)", line, re.IGNORECASE):
                return (
                    "The selected skill declares a strict runtime installation contract. "
                    "Refusing a manual `cd` in installer code. The installer runs Bash commands "
                    "inside the configured source install directory automatically; use the declared "
                    "command exactly without changing directories."
                )
            if not self._is_install_like_command(line):
                continue
            normalized = self._normalize_install_command_for_contract(line)
            if normalized in allowed:
                continue
            return (
                "The selected skill declares a strict runtime installation contract. "
                f"Refusing undeclared install command `{normalized}`. "
                "Use one of the `allowed_install_command` entries exactly; if they fail, report the failure instead of inventing a fallback."
            )
        return ""

    def _build_graph(self):
        workflow = StateGraph(InstallerState)

        # ------------------------------------
        # Node 1: Generate (智能 RAG + Prompt)
        # ------------------------------------
        async def inst_generate(state: InstallerState):
            messages = state["messages"]
            tool_name = state["tool_name"]
            
            # --- 1. 判定当前状态 ---
            is_init = False
            last_msg = messages[-1] if messages else None
            is_error_recovery = False

            if not messages or (len(messages) > 0 and not isinstance(messages[0], SystemMessage)):
                is_init = True
            
            if last_msg and isinstance(last_msg, HumanMessage) and "<observation>" in str(last_msg.content):
                if "❌" in str(last_msg.content) or "Failed" in str(last_msg.content):
                    is_error_recovery = True

            # --- 2. 执行 RAG ---
            rag_context = ""
            query = ""

            if is_init:
                query = f"How to install {tool_name} linux command line"
            elif is_error_recovery:
                error_snippet = str(last_msg.content)[-300:].replace("\n", " ")
                query = f"Error installing {tool_name}: {error_snippet}"

            if query and self.retriever and KB_MAP.get("TOOLS"):
                try:
                    target_kb = KB_MAP.get("ERRORS", KB_MAP["TOOLS"]) if is_error_recovery else KB_MAP["TOOLS"]
                    raw_result = self.retriever.retrieve(
                        query=query,
                        target_datasets=[target_kb],
                        top_k=2,
                        similarity_threshold=0.2 
                    )
                    if raw_result and len(raw_result.strip()) > 0:
                        rag_context = raw_result
                except Exception as e:
                    logger.warning(f"RAG Error: {e}")

            # --- 3. 构建 Prompt (核心修改) ---
            msgs_to_return = []

            if is_init:
                user_order = messages[0].content if messages else "Install this tool."

                # [核心修改] 强制逻辑注入
                # 1. 告诉 AI 源码目录在哪里 (BIO_SOURCE_INSTALL_DIR)
                # 2. 给出注册标签的具体 XML 示例，强迫它模仿
                
                source_dir_hint = f"Source directory: {BIO_SOURCE_INSTALL_DIR}" if BIO_SOURCE_INSTALL_DIR else "Current directory"

                work_order_section = f"""
<user_work_order>
{user_order}
</user_work_order>

<strict_anti_hallucination_rules>
⚠️ **CRITICAL RULES**:
1. **CODE IS MANDATORY**: You MUST output code blocks (```bash) for every action.
2. **VERIFY FIRST**: Verify the requested runtime or tool in the target environment and WAIT for the real result.
3. **NO FAKE SUCCESS**: Do NOT say "Ready" or output `<register_tool>` / `<register_environment>` until you see a `✅` log.
4. **REGISTER THE REAL ENV**: If the verified tool path is inside `/envs/ENV_NAME/bin`, use `env="ENV_NAME"` in `<register_tool>` and `<register_environment>`. Do not default to `bio_tool` for an already-installed conda tool.
</strict_anti_hallucination_rules>
"""

                base_prompt = PromptManager.get_installer_system_prompt(
                    tool_name=tool_name,
                    work_dir=self.pm.work_dir,
                    source_path=BIO_SOURCE_INSTALL_DIR,
                    conda_root=ANACONDA_ROOT
                ) + work_order_section
                
                sys_msgs = [SystemMessage(content=base_prompt)]
                if rag_context:
                    sys_msgs.append(SystemMessage(content=f"### REFERENCE GUIDE ###\n{rag_context}"))
                
                msgs_to_return.extend(sys_msgs)
            
            elif is_error_recovery and rag_context:
                msgs_to_return.append(SystemMessage(content=f"💡 **RAG Suggestion**:\n{rag_context}"))

            if is_init:
                full_context = msgs_to_return + messages
            else:
                full_context = messages + msgs_to_return

            try:
                response = await self.llm.ainvoke(full_context)
            except Exception as exc:  # noqa: BLE001 - convert provider failures into graph state.
                summary = llm_failure_summary(exc, f"installer_generate:{tool_name}")
                if is_llm_input_rejected(exc):
                    guidance = (
                        "The model provider rejected the installer prompt payload. "
                        "The installer is stopping instead of resubmitting the same accumulated logs."
                    )
                else:
                    guidance = "The installer LLM call failed before it could generate the next action."
                return {
                    "messages": [
                        HumanMessage(
                            content=(
                                "<failure>"
                                f"{summary}\n{guidance}\n"
                                "No installation command was executed after this failure."
                                "</failure>"
                            )
                        )
                    ]
                }
            msgs_to_return.append(response)
            
            return {"messages": msgs_to_return}

        # ------------------------------------
        # Node 2: Execute (执行 + 注册)
        # ------------------------------------
        async def inst_execute(state: InstallerState):
            last_msg = state["messages"][-1]
            content = last_msg.content
            tool_name = state["tool_name"]
            observations = []
            contract = self._strict_runtime_contract(state)
            registration_found = False
            
            # 1. 执行 Shell/Python 代码
            exec_matches = re.findall(r"<execute.*?>(.*?)</execute>", content, re.DOTALL)
            if not exec_matches: # Markdown 保底
                exec_matches = re.findall(r"```(?:bash|sh|python)?\n(.*?)```", content, re.DOTALL)

            execution_success = False
            for original_code in exec_matches:
                language = detect_script_language(original_code)
                code = normalize_script_code(original_code)
                stripped_code = code.strip()

                if re.fullmatch(r"<register_(?:environment|tool)\b.*?/?>", stripped_code, re.DOTALL):
                    continue

                if self._is_low_friction_installer_command(stripped_code, language):
                    final_code = stripped_code
                else:
                    contract_violation = self._runtime_contract_violation(stripped_code, language, state)
                    if contract_violation:
                        observations.append(f"<observation>❌ Runtime contract violation: {contract_violation}</observation>")
                        continue
                    action, final_code = self.middleware.review_code(code, language)
                    if action == "reject":
                        observations.append(
                            "<observation>❌ User rejected execution. "
                            f"Feedback: {str(final_code or 'No reason provided.')[:500]}</observation>"
                        )
                        continue
                    elif action == "quit":
                        return {"messages": [HumanMessage(content="Installation terminated.")]}
                    elif action == "edit":
                        final_code = normalize_script_code(final_code)
                        language = detect_script_language(final_code)
                    else:
                        final_code = normalize_script_code(final_code)

                contract_violation = self._runtime_contract_violation(final_code, language, state)
                if contract_violation:
                    observations.append(f"<observation>❌ Runtime contract violation: {contract_violation}</observation>")
                    continue

                if language == "Python":
                    stdout, stderr, rc = await run_python_repl(final_code)
                else:
                    shim_path = self.pm.get_shim_dir_str()
                    source_root = str(self.pm.source_root)
                    full_code = (
                        f'export PATH="{shim_path}:$PATH"\n'
                        f"cd {shlex.quote(source_root)}\n"
                        f"{final_code}"
                    )
                    stdout, stderr, rc = await run_bash_script(full_code)

                status = "✅" if rc == 0 else "❌"
                if rc == 0: execution_success = True
                observations.append(
                    f"<observation>[{language}] {status}\n"
                    f"Command:\n{final_code[:500]}\n"
                    f"STDOUT:\n{stdout[:500]}\n"
                    f"STDERR:\n{stderr[:500]}</observation>"
                )
                declared_command = self._declared_install_like_command(final_code, contract)
                if declared_command:
                    runtime_check = self._verify_and_register_contract_runtime(
                        contract,
                        tool_name,
                        declared_command,
                    )
                    if runtime_check:
                        observations.append(runtime_check)
                        if "registered successfully" in runtime_check:
                            registration_found = True

            # 2. [核心修复] 注册标签解析与强制校验
            attr_pattern = re.compile(r'(\w+)=(["\'])(.*?)\2')

            for tag_attrs in re.findall(r"<register_environment\b(.*?)/?>", content, re.DOTALL):
                attrs = {key: value for key, _, value in attr_pattern.findall(tag_attrs)}
                env = attrs.get("env") or attrs.get("name") or f"bio_{tool_name}"
                language = attrs.get("language", "")
                packages = attrs.get("packages", "")
                capabilities = attrs.get("capabilities", "")
                healthcheck = attrs.get("healthcheck", "")
                status = attrs.get("status", "registered")
                try:
                    self.pm.register_environment(
                        env_name=env,
                        purpose=attrs.get("purpose", f"Runtime for {tool_name}"),
                        packages=packages,
                        notes=attrs.get("notes", ""),
                        language=language,
                        capabilities=capabilities,
                        healthcheck=healthcheck,
                        status=status,
                    )
                    observations.append(
                        f"<observation>✅ Environment '{env}' registered successfully via PackageManager.</observation>"
                    )
                    registration_found = True
                except Exception as e:
                    observations.append(f"<observation>❌ Environment Registration Failed: {e}</observation>")

            for tag_attrs in re.findall(r"<register_tool\b(.*?)/?>", content, re.DOTALL):
                attrs = {key: value for key, _, value in attr_pattern.findall(tag_attrs)}
                cmd = attrs.get("command") or tool_name
                path = attrs.get("path")
                inferred_env = self.pm.infer_conda_env_from_path(path) if path else ""
                env = attrs.get("env") or inferred_env or f"bio_{tool_name}"
                try:
                    self.pm.register_existing_tool(
                        tool_name=cmd,
                        env_name=env,
                        source_path=path
                    )
                    registered_env = self.pm.registry.get("tools", {}).get(cmd, {}).get("env", env)
                    self.pm.bind_tool_to_environment(cmd, registered_env)
                    observations.append(f"<observation>✅ Tool '{cmd}' registered successfully via PackageManager. Shim created.</observation>")
                    registration_found = True
                except Exception as e:
                    observations.append(f"<observation>❌ Registration Failed: {e}</observation>")

            # 3. [关键逻辑] 如果执行成功了，但是没注册，也没有报错，那就是幻觉！
            # 我们强制添加一条“系统警告”，让 AI 在下一轮必须补上注册标签
            if not exec_matches and not registration_found:
                if "ready" in content.lower() or "success" in content.lower():
                     rejection_msg = """
<system_error>
❌ HALLUCINATION DETECTED.
You claimed "Ready/Success", but you did NOT output any executable code block (```bash)!
NOTHING WAS INSTALLED.

Action Required:
1. Write the ACTUAL installation commands inside ```bash ... ``` blocks.
2. Wait for execution results.
3. Then register.
</system_error>
"""
                     observations.append(rejection_msg)

            if not observations:
                observations.append("<observation>❌ No executable code or registration tag found.</observation>")
            
            return {"messages": [HumanMessage(content="\n".join(observations))]}

        # ------------------------------------
        # 3. Router
        # ------------------------------------
        def inst_router(state: InstallerState):
            last_msg = state["messages"][-1]
            content = last_msg.content
            
            if isinstance(last_msg, AIMessage):
                if self._installer_generate_turns(state) > self.MAX_INSTALLER_GENERATE_TURNS:
                    return "end_failure"
                return "execute"

            if "<success>" in content: return "end_success"
            if "<failure>" in content: return "end_failure"
             
            # Observation 中明确注册了环境或工具，才算成功
            if "Shim created" in content or "registered successfully" in content:
                return "end_success"
            if self._installer_generate_turns(state) >= self.MAX_INSTALLER_GENERATE_TURNS:
                return "end_failure"
            if self._repeated_installer_observation_count(state) >= self.MAX_REPEATED_OBSERVATIONS:
                return "end_failure"
                 
            return "generate"

        # ------------------------------------
        # 4. Finish Nodes
        # ------------------------------------
        def end_success(state: InstallerState):
            return {"final_result": "✅ Installation & Registration Complete."}
        def end_failure(state: InstallerState):
            return {"final_result": f"❌ Installation Failed: {self._installer_failure_reason(state)}"}

        # ------------------------------------
        # 5. Graph Wiring
        # ------------------------------------
        workflow.add_node("generate", inst_generate)
        workflow.add_node("execute", inst_execute)
        workflow.add_node("end_success", end_success)
        workflow.add_node("end_failure", end_failure)

        workflow.add_edge(START, "generate")
        
        workflow.add_conditional_edges("generate", inst_router, 
            {"execute": "execute", "end_success": "end_success", "end_failure": "end_failure", "generate": "generate"})
        
        workflow.add_conditional_edges("execute", inst_router, 
            {"generate": "generate", "end_success": "end_success", "end_failure": "end_failure"})
        
        workflow.add_edge("end_success", END)
        workflow.add_edge("end_failure", END)

        return workflow.compile(checkpointer=self.checkpointer)

    def get_graph(self):
        return self.graph
