"""Explicit execution approval and read-only risk/provenance presentation.

Pi owns recovery reasoning and model calls.  The legacy Recovery provider,
follow-up loop and automatic retry middleware are retired.  The existing human
approval parser and interrupt hook remain for execution permission consumers.
"""
import re
import json
import logging
from copy import deepcopy
from typing import Tuple, Dict, Any, List, Mapping, Optional

from langgraph.types import interrupt
from execution.confirmation import RiskAnalyzer, RiskLevel

logger = logging.getLogger("BioMind.Middleware")

_CODE_REVIEW_SECTION_SPECS = (
    ("A. MODEL-GENERATED ACTION", "MODEL_SCRIPT", "generate", "model"),
    ("B. MANAGED ARTIFACT BINDINGS", "ARTIFACT_ENV", "managed_artifact_controller", "controller"),
    ("C. CONTROLLER-PROVIDED RUNTIME BINDINGS", "RUNTIME_ENV", "runtime_resolver", "controller"),
    ("D. EXECUTION POLICY", "EXECUTION_POLICY", "execution_controller", "controller"),
)


def build_code_review_provenance(
    model_script: str,
    *,
    generated_script_sha256: str,
    artifact_env_manifest: Mapping[str, Any],
    runtime_env_manifest: Mapping[str, Any],
    execution_policy_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the four-source review ABI without materializing environment values."""
    inputs = (
        str(model_script or ""),
        deepcopy(dict(artifact_env_manifest or {})),
        deepcopy(dict(runtime_env_manifest or {})),
        deepcopy(dict(execution_policy_manifest or {})),
    )
    hashes = (
        str(generated_script_sha256 or ""),
        str(artifact_env_manifest.get("manifest_hash") or ""),
        str(runtime_env_manifest.get("manifest_hash") or ""),
        str(execution_policy_manifest.get("manifest_hash") or ""),
    )
    sections: List[Dict[str, Any]] = []
    for spec, content, content_hash in zip(_CODE_REVIEW_SECTION_SPECS, inputs, hashes):
        title, risk_source, source, owner = spec
        sections.append({
            "title": title,
            "risk_source": risk_source,
            "source": source,
            "hash": content_hash,
            "scope": "current_action",
            "owner": owner,
            "content": content,
        })
    return {"schema_version": 1, "sections": sections}


def render_code_review_provenance(value: Mapping[str, Any]) -> str:
    rendered: List[str] = []
    guard = value.get("output_write_guard") or {}
    if guard.get("targets"):
        rendered.append("文件写入保护（本次审批范围）")
        for row in guard["targets"]:
            rendered.append(("已存在：" if row["before"]["exists"] else "新建：") + row["path"])
            if row.get("backup_path"):
                rendered.append("明确批准后先备份至：" + row["backup_path"])
        rendered.append("不得直接覆盖原文件；已有文件仅在明确批准备份后替换时允许写入。")
    for raw in value.get("sections", []) or []:
        if not isinstance(raw, Mapping):
            continue
        rendered.extend([
            str(raw.get("title") or "SECTION"),
            (
                "source={source} hash={hash} scope={scope} owner={owner}".format(
                    source=str(raw.get("source") or ""),
                    hash=str(raw.get("hash") or ""),
                    scope=str(raw.get("scope") or ""),
                    owner=str(raw.get("owner") or ""),
                )
            ),
        ])
        content = raw.get("content")
        rendered.append(
            json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True)
            if isinstance(content, Mapping)
            else str(content or "")
        )
        rendered.append("")
    return "\n".join(rendered).rstrip()


def _review_risk_by_provenance(
    code: str,
    language: str,
    provenance: Mapping[str, Any],
) -> Dict[str, Any]:
    analyses: List[tuple[str, Dict[str, Any]]] = []
    for raw in provenance.get("sections", []) or []:
        if not isinstance(raw, Mapping):
            continue
        source = str(raw.get("risk_source") or "MODEL_SCRIPT")
        content = raw.get("content")
        text = (
            str(code or "")
            if source == "MODEL_SCRIPT"
            else json.dumps(content, ensure_ascii=False, sort_keys=True)
        )
        analysis = RiskAnalyzer.analyze(
            text,
            language if source == "MODEL_SCRIPT" else "manifest",
        )
        # A valid Controller manifest containing no risky operation is data,
        # not an unknown executable command. It is therefore safe while still
        # participating in integrity/risk analysis.
        if source != "MODEL_SCRIPT" and not analysis.get("risky_operations"):
            analysis = {
                **analysis,
                "risk_level": RiskLevel.SAFE,
                "risk_score": 0,
                "safe_operations": ["manifest metadata only"],
            }
        analyses.append((source, analysis))
    if not analyses:
        analyses.append(("MODEL_SCRIPT", RiskAnalyzer.analyze(code, language)))
    order = list(RiskLevel)
    highest = max(
        (item[1]["risk_level"] for item in analyses),
        key=lambda level: order.index(level),
    )
    return {
        "risk_level": highest,
        "risk_score": max(float(item[1].get("risk_score") or 0) for item in analyses),
        "risky_operations": [
            f"{source}: {reason}"
            for source, item in analyses
            for reason in item.get("risky_operations", []) or []
        ],
        "safe_operations": [
            f"{source}: {reason}"
            for source, item in analyses
            for reason in item.get("safe_operations", []) or []
        ],
        "recommendations": [
            f"{source}: {reason}"
            for source, item in analyses
            for reason in item.get("recommendations", []) or []
        ],
        "risk_sources": [source for source, _item in analyses],
    }


class HITLMiddleware:
    def __init__(self, auto_approve_safe: bool = True):
        # Compatibility parameter only; risk heuristics no longer grant permission.
        self.auto_approve_safe = auto_approve_safe

    def review_code(
        self,
        code: str,
        language: str,
        *,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        代码审查核心逻辑：
        1. 先进行风险分析（使用您提供的稳健逻辑）
        2. 风险只用于展示，不授予执行权限
        3. 明确的人工输入决定批准；空白/编辑继续等待
        """
        
        conflicts = [row for row in (dict(provenance or {}).get("output_write_guard") or {}).get("targets", [])
                     if row.get("before", {}).get("exists")]
        # --- A. 移植您的动态风险分析逻辑 (包含异常处理) ---
        try:
            risk_analysis = (
                _review_risk_by_provenance(code, language, provenance)
                if provenance
                else RiskAnalyzer.analyze(code, language)
            )
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

        # Risk descriptions never grant execution permission. Runtime owns it.

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
            "msg": f"⚠️ Code Execution Request ({language}) | y=run, n=reject, q=quit, e:/edit:/code:=replacement; text=feedback, not code",
            "content": (
                render_code_review_provenance(provenance)
                if provenance else code
            ),
            "editable_content": code,
            "provenance": deepcopy(dict(provenance or {})),
            "language": language,
            "risk": risk_info, # 传入风险详情
            "options": ["(y)es", "(n)o", "(e)dit"],
            "instructions": "Plain text is feedback, not executable code. Replacement code must start with e:, edit:, or code:."
        }
        if conflicts:
            payload["msg"] = ("目标文件已存在。输入 backup 明确批准先保留旧版本、再执行替换；"
                              "n 拒绝，或输入新位置等反馈。y/Enter 不授权覆盖。")
            payload["options"] = ["backup：备份后替换", "n：保留并拒绝", "文字：修改方案"]
            payload["instructions"] = payload["msg"]
            payload["output_conflicts"] = deepcopy(conflicts)

        # --- D. 触发中断 ---
        while True:
            user_response = interrupt(payload)
            decision, content = self._parse_code_response(user_response, original_code=code)
            if decision == "pending":
                payload = {**payload, "msg": "仍待明确批准：y 执行，n 拒绝；空白不是决定。"}
                continue
            if decision == "edit":
                code = content
                payload = {**payload, "content": code, "editable_content": code,
                           "msg": "提案已更新，尚未批准。请核对实际内容后明确输入 y 或 n。"}
                continue
            if conflicts:
                if isinstance(user_response, str) and user_response.strip().lower() == "backup":
                    return "approve_backup_replace", code
                decision, _ = self._parse_code_response(user_response, original_code=code)
                if decision == "approve":
                    # Keep this same approval pending. A mistaken Enter/y is
                    # neither overwrite consent nor a reason to call Generate.
                    payload = {**payload, "msg": "尚未授权替换。输入 backup 批准备份后替换，n 拒绝，或输入修改方案。"}
                    continue
            break

        # --- E. 解析用户反馈 (支持编辑) ---
        decision, content = self._parse_code_response(user_response, original_code=code)
        if conflicts and decision == "approve":
            return "reject", "普通批准不授权覆盖已有文件；需要明确选择备份后替换或修改输出位置。"
        return decision, content

    def _parse_code_response(self, response: Any, original_code: str) -> Tuple[str, str]:
        """解析用户输入：支持带理由的拒绝"""
        
        # 1. 处理结构化反馈 (来自交互界面的高级输入)
        if isinstance(response, dict):
            if response.get("action") == "reject":
                return "reject", response.get("reason", "No reason provided.")
            if response.get("action") in {"paste", "edit"}:
                replacement = response.get("code")
                return ("edit", replacement) if isinstance(replacement, str) and replacement.strip() else ("pending", original_code)
            return "pending", original_code

        if not isinstance(response, str) or not response.strip():
            return "pending", original_code
        resp_str = response.strip()
        
        # 2. 批准
        if resp_str.lower() in ['y', 'yes', 'approve', 'ok']:
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
            return "pending", original_code

        # 7. Other text is usually a parameter answer or guidance typed into the
        # review prompt by mistake. Do not run it as shell code.
        else:
            return "reject", (
                "User provided feedback at code review instead of approval: "
                f"{resp_str}. Regenerate the next command using this feedback if relevant."
            )


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
