import re
import logging
import shlex
from copy import deepcopy
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
from agent_core.context_lineage import ainvoke_with_snapshot
from agent_core.action_runtime import (
    build_action_approval_identity,
    stable_digest,
    validate_action_approval_identity,
)
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
    ANACONDA_ROOT,
    LLM_STEP_CONTEXT_CHARS,
    LLM_STEP_CONTEXT_MESSAGES,
)

logger = logging.getLogger("BioMind.Installer")

class InstallerAgent:
    MAX_INSTALLER_GENERATE_TURNS = 10
    MAX_REPEATED_OBSERVATIONS = 3

    def __init__(self, llm, work_dir: str = "./data", package_manager=None):
        self.llm = llm
        self.work_dir = str(work_dir)
        if package_manager:
            self.pm = package_manager
        else:
            self.pm = PackageManager(work_dir=work_dir)
        self.checkpointer = MemorySaver()
        # Installer commands mutate environments. Generic risk heuristics (in
        # particular `conda run`) are not installation authorization.
        self.middleware = HITLMiddleware(auto_approve_safe=False)

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
        # Read-only healthchecks belong to the parent's controlled verifier.
        # No generated shell prefix is proof that the entire script is read-only.
        return False

    @staticmethod
    def _request_scope(state: InstallerState) -> Dict[str, Any]:
        request = deepcopy(dict(state.get("installation_request") or {}))
        request["request_id"] = str(request.get("request_id") or request.get("fingerprint") or "")
        required = ("request_id", "attempt_id", "tool", "target_env", "target_prefix")
        missing = [name for name in required if not str(request.get(name) or "").strip()]
        if missing:
            raise ValueError("INSTALLATION_SCOPE_MISSING: " + ", ".join(missing))
        prefix = str(request["target_prefix"])
        if not Path(prefix).is_absolute():
            raise ValueError("INSTALLATION_TARGET_PREFIX_NOT_ABSOLUTE")
        if str(state.get("tool_name") or "") != str(request["tool"]):
            raise ValueError("INSTALLATION_TOOL_SCOPE_MISMATCH")
        if sorted(state.get("selected_skills") or []) != sorted(request.get("selected_skills") or []):
            raise ValueError("INSTALLATION_SKILL_SCOPE_MISMATCH")
        return request

    def _command_identity(self, state, request, code, language):
        """Use the existing action approval ABI for the complete install proposal."""
        return build_action_approval_identity(
            generated_script_sha256=stable_digest({"language": language, "code": code}),
            capability_contract_hash_value=stable_digest({
                "selected_skills": request.get("selected_skills", []),
                "capabilities": request.get("capabilities", []),
            }),
            runtime_env_manifest_hash=stable_digest(request),
            artifact_env_manifest_hash=stable_digest({}),
            execution_policy_manifest_hash=stable_digest({
                "approval_scope": state.get("approval_scope") or {},
                "path_prefix": str(self.pm.get_shim_dir_str()),
                "registration_owner": "parent_target_verifier",
            }),
            working_directory=str(self.pm.source_root),
        )

    @staticmethod
    def _receipt_scope(request):
        scope = {name: deepcopy(request.get(name)) for name in (
            "request_id", "attempt_id", "target_env", "target_prefix",
            "selected_skills", "capabilities",
            "generation_source_scope",
        )}
        scope["request_hash"] = stable_digest(request)
        return scope

    def _execution_receipt(self, state, request):
        previous = dict(state.get("installation_receipt") or {})
        scope = self._receipt_scope(request)
        if any(previous.get(name) != value for name, value in scope.items()):
            previous = {}
        return {**previous, **scope, "execution": list(previous.get("execution") or [])}

    @staticmethod
    def _execution_failure_category(stderr):
        # Classification preserves evidence; it never grants a repair or guesses
        # that a missing package caused an unrecognized process failure.
        patterns = (
            ("permission", r"Permission denied|PermissionError|\[Errno 13\]"),
            ("network", r"Could not resolve host|Temporary failure in name resolution|Connection timed out|Network is unreachable"),
            ("solver", r"UnsatisfiableError|LibMambaUnsatisfiableError|PackagesNotFoundError|ResolutionImpossible"),
            ("build", r"CMake Error|Failed building wheel|make(?:\[\d+\])?: \*\*\*"),
        )
        return next((category for category, pattern in patterns if re.search(pattern, str(stderr or ""), re.I)), "unknown")

    def _installer_generate_turns(self, state: InstallerState) -> int:
        return sum(1 for msg in state.get("messages", []) if isinstance(msg, AIMessage))

    @staticmethod
    def _clip_message_content(message, max_chars: int):
        content = str(getattr(message, "content", "") or "")
        if len(content) <= max_chars:
            return message
        head = max_chars // 2
        tail = max_chars - head
        clipped = content[:head] + "\n...[step context clipped]...\n" + content[-tail:]
        try:
            return message.model_copy(update={"content": clipped})
        except Exception:
            return message

    @classmethod
    def _step_local_messages(
        cls,
        messages,
        *,
        max_messages: int = LLM_STEP_CONTEXT_MESSAGES,
        max_chars: int = LLM_STEP_CONTEXT_CHARS,
    ):
        """Keep the installer work order and latest evidence, not old logs."""
        if not messages:
            return []

        selected = []

        def add(message):
            if message not in selected:
                selected.append(message)

        for message in messages:
            if isinstance(message, SystemMessage):
                add(message)
                break
        for message in messages:
            if isinstance(message, HumanMessage):
                add(message)
                break
        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "")
            if isinstance(message, HumanMessage) and (
                "<observation>" in content
                or "<failure>" in content
                or "Feedback:" in content
            ):
                add(message)
                break
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                add(message)
                break
        for message in reversed(messages):
            if isinstance(message, SystemMessage):
                add(message)
                break
        if not selected:
            add(messages[-1])

        # System instructions must precede the retained work-order/evidence messages.
        selected = [
            *[message for message in selected if isinstance(message, SystemMessage)],
            *[message for message in selected if not isinstance(message, SystemMessage)],
        ][:max_messages]
        if not selected:
            return []

        per_message_limit = max(800, max_chars // len(selected))
        clipped = [cls._clip_message_content(message, per_message_limit) for message in selected]
        total = sum(len(str(getattr(message, "content", "") or "")) for message in clipped)
        if total > max_chars:
            clipped[-1] = cls._clip_message_content(
                clipped[-1],
                max(400, max_chars - sum(
                    len(str(getattr(message, "content", "") or ""))
                    for message in clipped[:-1]
                )),
            )
        return clipped

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
        request = dict(state.get("installation_request") or {})
        if "install_policy" in request:
            strict = str(request.get("install_policy") or "").lower() in {"declared-only", "strict", "locked"}
            return {**request, "active": strict,
                    "allowed": [self._normalize_install_command_for_contract(command)
                                for command in request.get("allowed_install_commands") or []]}
        # Legacy handover compatibility: only the original work order is
        # authority, never a later model reply or reflected observation.
        text = next((str(getattr(msg, "content", "") or "") for msg in state.get("messages", [])
                     if isinstance(msg, HumanMessage)), "")
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
            try:
                request = self._request_scope(state)
                receipt = self._execution_receipt(state, request)
            except ValueError:
                receipt = {}
            if receipt.get("status") in {"cancelled", "rejected", "blocked", "execution_failed", "verification_required"}:
                return {"messages": [HumanMessage(content=str(receipt.get("detail") or "Installation receipt already recorded."))]}
            messages = state["messages"]
            tool_name = state["tool_name"]
            
            # --- 1. 判定当前状态 ---
            is_init = False
            last_msg = messages[-1] if messages else None
            is_error_recovery = False

            is_init = not any(isinstance(message, AIMessage) for message in messages)
            
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
                step_context = self._step_local_messages(messages)
                full_context = (
                    [message for message in step_context if isinstance(message, SystemMessage)]
                    + [message for message in msgs_to_return if isinstance(message, SystemMessage)]
                    + [message for message in step_context if not isinstance(message, SystemMessage)]
                    + [message for message in msgs_to_return if not isinstance(message, SystemMessage)]
                )

            try:
                response, _ = await ainvoke_with_snapshot(
                    self.llm,
                    full_context,
                    data_path=self.work_dir,
                    task_id=str(state.get("task_id") or tool_name or "installer"),
                    metadata={
                        "node_name": "install_subagent",
                        "call_kind": "installer",
                        "selected_skill_ids": list(state.get("selected_skills") or []),
                        "installer_tool_name": tool_name,
                        "installer_turn": self._installer_generate_turns(state) + 1,
                    },
                )
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
            content = str(state["messages"][-1].content or "")
            try:
                request = self._request_scope(state)
            except ValueError as exc:
                return {
                    "installation_receipt": {"status": "blocked", "failure_stage": "authorization", "detail": str(exc)},
                    "messages": [HumanMessage(content=f"<failure>{exc}</failure>")],
                }
            receipt = self._execution_receipt(state, request)
            if receipt.get("status") in {"rejected", "cancelled"}:
                return {"installation_receipt": receipt, "messages": [HumanMessage(
                    content="<failure>This installation proposal was already rejected or cancelled.</failure>"
                )]}
            observations = []
            approved_identity = dict(state.get("action_approval_identity") or {})
            exec_matches = re.findall(r"<execute.*?>(.*?)</execute>", content, re.DOTALL)
            if not exec_matches:
                exec_matches = re.findall(r"```(?:bash|sh|python)?\n(.*?)```", content, re.DOTALL)

            def finish(status, detail, stage=""):
                receipt.update(status=status, detail=detail)
                if stage:
                    receipt["failure_stage"] = stage
                return {"installation_receipt": receipt,
                        "action_approval_identity": approved_identity,
                        "messages": [HumanMessage(content=detail)]}

            for original_code in exec_matches:
                code = normalize_script_code(original_code).strip()
                if re.fullmatch(r"<register_(?:environment|tool)\b.*?/?>", code, re.DOTALL):
                    continue
                language = detect_script_language(code)
                contract_violation = self._runtime_contract_violation(code, language, state)
                if contract_violation:
                    return finish("blocked", f"<failure>Runtime contract violation: {contract_violation}</failure>", "authorization")
                missing = [key for key in ("sources", "version_requirements", "impact", "verification_commands")
                           if not request.get(key)]
                if missing:
                    return finish("blocked", "<failure>INSTALLATION_PROPOSAL_INCOMPLETE: " + ", ".join(missing) + "</failure>", "authorization")
                identity = self._command_identity(state, request, code, language)
                completed = next((row for row in receipt["execution"]
                                  if row.get("action_approval_identity") == identity
                                  and row.get("returncode") == 0), None)
                if completed:
                    observations.append("<observation>Installation command already completed for this request; reusing execution receipt.</observation>")
                    continue
                covered = False
                if approved_identity:
                    try:
                        validate_action_approval_identity(approved_identity, identity)
                        covered = True
                    except ValueError:
                        pass
                if not covered:
                    provenance = {"schema_version": 1, "sections": [
                        {"title": "A. INSTALLATION COMMAND", "risk_source": "MODEL_SCRIPT",
                         "source": "installer", "owner": "model", "scope": "current_action",
                         "hash": identity["generated_script_sha256"], "content": code},
                        {"title": "B. EXACT INSTALLATION SCOPE", "risk_source": "EXECUTION_POLICY",
                         "source": "installation_request", "owner": "parent", "scope": "current_action",
                         "hash": identity["runtime_env_manifest_hash"],
                         "content": {"request": request, "approval_scope": state.get("approval_scope") or {},
                                     "action_approval_identity": identity,
                                     "cwd": str(self.pm.source_root),
                                     "env": {"PATH_prefix": str(self.pm.get_shim_dir_str())}}},
                    ]}
                    action, final_code = self.middleware.review_code(code, language, provenance=provenance)
                    if action in {"reject", "quit"}:
                        receipt["decision"] = {"decision": action, "action_approval_identity": identity,
                                               "guidance": str(final_code or "No reason provided.")}
                        if action == "reject":
                            return finish("rejected", "<failure>User rejected execution. "
                                          f"Feedback: {str(final_code or 'No reason provided.')[:500]}</failure>", "authorization")
                        return finish("cancelled", "<failure>Installation cancelled by user.</failure>", "authorization")
                    if action not in {"approve", "edit"}:
                        return finish("blocked", "<failure>INSTALLATION_APPROVAL_NOT_GRANTED</failure>", "authorization")
                    final_code = normalize_script_code(final_code).strip()
                    # A replacement changes the complete proposal. Expose it for a
                    # new review instead of running it under the original identity.
                    if final_code != code:
                        return finish("proposal_changed", "<observation>Edited proposal requires a new exact review.\n"
                                      f"```{language.lower()}\n{final_code}\n```</observation>")
                    approved_identity = identity
                source_root = str(self.pm.source_root)
                shim_path = str(self.pm.get_shim_dir_str())
                row = {"command": code, "language": language, "cwd": source_root,
                       "env": {"PATH_prefix": shim_path}, "action_approval_identity": identity}
                try:
                    if language == "Python":
                        # The Python REPL has a different implicit cwd/state contract.
                        # Keep all installation execution in the reviewed Bash carrier.
                        return finish("blocked", "<failure>INSTALLATION_CARRIER_UNSUPPORTED: use an explicit Bash launcher.</failure>", "authorization")
                    full_code = (f"export PATH={shlex.quote(shim_path)}:\"$PATH\"\n"
                                 f"cd {shlex.quote(source_root)}\n{code}")
                    stdout, stderr, rc = await run_bash_script(full_code)
                except Exception as exc:
                    row.update(returncode=None, stdout="", stderr=str(exc), failure_stage="execution", cause="unknown")
                    receipt["execution"].append(row)
                    return finish("execution_failed", f"<failure>Installation execution failed: {exc}</failure>", "execution")
                row.update(returncode=rc, stdout=str(stdout), stderr=str(stderr))
                if rc != 0:
                    row["failure_category"] = self._execution_failure_category(stderr)
                    receipt["failure_category"] = row["failure_category"]
                receipt["execution"].append(row)
                observations.append(f"<observation>[{language}] {'✅' if rc == 0 else '❌'}\n"
                                    f"Command:\n{code}\nReturn code: {rc}\nCWD: {source_root}\n"
                                    f"STDOUT:\n{str(stdout)[:500]}\nSTDERR:\n{str(stderr)[:500]}</observation>")
                if rc != 0:
                    return finish("execution_failed", "\n".join(observations), "execution")

            # Registration is a parent-owned stage after all target-specific
            # healthchecks. Model tags are requests, never availability receipts.
            attr_pattern = re.compile(r"(\w+)=([\"'])(.*?)\2")
            registrations = []
            for kind, tag_attrs in re.findall(r"<register_(environment|tool)\b(.*?)/?>", content, re.DOTALL):
                attrs = {key: value for key, _, value in attr_pattern.findall(tag_attrs)}
                env = attrs.get("env") or attrs.get("name") or request["target_env"]
                if env != request["target_env"]:
                    return finish("blocked", "<failure>INSTALLATION_REGISTRATION_TARGET_MISMATCH</failure>", "registration")
                registrations.append({"kind": kind, "attributes": attrs})
            if registrations:
                receipt["registration_requests"] = registrations
            if registrations or receipt["execution"]:
                return finish("verification_required", "\n".join(observations + [
                    "<observation>Execution evidence recorded. Parent must verify the approved target and register it before capability availability.</observation>"
                ]))
            return finish("proposal_required", "<observation>❌ No executable code or registration tag found. Registration alone cannot certify availability.</observation>")

        # ------------------------------------
        # 3. Router
        # ------------------------------------
        async def inst_router(state: InstallerState):
            last_msg = state["messages"][-1]
            content = last_msg.content

            try:
                request = self._request_scope(state)
                current_receipt = self._execution_receipt(state, request)
            except ValueError:
                current_receipt = dict(state.get("installation_receipt") or {})
            receipt_status = str(current_receipt.get("status") or "")
            if receipt_status in {"cancelled", "rejected", "blocked", "execution_failed"}:
                return "end_failure"
            if receipt_status == "verification_required":
                return "end_success"
            
            if isinstance(last_msg, AIMessage):
                if self._installer_generate_turns(state) > self.MAX_INSTALLER_GENERATE_TURNS:
                    return "end_failure"
                return "execute"

            if "<failure>" in content: return "end_failure"
            if self._installer_generate_turns(state) >= self.MAX_INSTALLER_GENERATE_TURNS:
                return "end_failure"
            if self._repeated_installer_observation_count(state) >= self.MAX_REPEATED_OBSERVATIONS:
                return "end_failure"
                 
            return "generate"

        # ------------------------------------
        # 4. Finish Nodes
        # ------------------------------------
        async def end_success(state: InstallerState):
            return {"final_result": "Installation execution recorded; approved-target verification and registration pending."}
        async def end_failure(state: InstallerState):
            receipt = dict(state.get("installation_receipt") or {})
            return {"final_result": f"❌ Installation {receipt.get('status') or 'failed'}: {receipt.get('detail') or self._installer_failure_reason(state)}"}

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
