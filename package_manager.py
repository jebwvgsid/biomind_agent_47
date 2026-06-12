import os
import json
import re
import shutil
import shlex
import subprocess
import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
import yaml  # pip install pyyaml

# =========================================================
# 1. 配置加载与路径修复
# =========================================================
try:
    # 1. 获取当前文件所在目录 (通常是 agent_core)
    current_dir = Path(__file__).resolve().parent
    # 2. 获取 BioMind 项目根目录
    project_root = current_dir.parent
    
    # 3. 将项目根目录加入 sys.path
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    # 4. [修改] 使用统一的导入方式 (防止文件夹/文件冲突)
    from config import config as config_module

    # 读取变量
    BIO_SOFTWARE_ROOT = getattr(config_module, 'BIO_SOFTWARE_ROOT', None)
    # [新增] 读取源码安装专用目录
    BIO_SOURCE_INSTALL_DIR = getattr(config_module, 'BIO_SOURCE_INSTALL_DIR', None)
    ANACONDA_ROOT = getattr(config_module, 'ANACONDA_ROOT', None)

except Exception as e:
    # 兜底默认值
    BIO_SOFTWARE_ROOT = None
    BIO_SOURCE_INSTALL_DIR = None
    ANACONDA_ROOT = None

logger = logging.getLogger("PackageManager")

class PackageManager:
    INSTALLED_STATUSES = {"installed", "verified", "success"}
    PYTHON_IMPORT_ALIASES = {
        "pytorch": "torch",
        "scikit-learn": "sklearn",
        "scikit_learn": "sklearn",
        "pyyaml": "yaml",
        "opencv-python": "cv2",
        "opencv_python": "cv2",
        "pillow": "PIL",
    }
    STATUS_RANK = {
        "": 0,
        "registered": 0,
        "broken": 0,
        "installed": 1,
        "verified": 2,
        "success": 3,
    }
    SHIM_PATH_PARTS = {"agent_shim", "bin_shims", "shims"}

    def __init__(self, work_dir: str = "./"):
        # 1. work_dir 用于存放临时文件或项目特定数据
        self.work_dir = Path(work_dir).absolute()
        
        # 2. 确定软件仓库根目录
        if BIO_SOFTWARE_ROOT:
            self.root_dir = Path(BIO_SOFTWARE_ROOT)
        else:
            self.root_dir = self.work_dir
            
        # 确保目录存在
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        except: pass
        
        print(f"🏭 Software Repository: {self.root_dir}")

        # 3. 定义关键路径
        self.registry_path = self.root_dir / "software_registry.json"
        
        self.shim_dir = self.root_dir / "bin_shims"
        self.shim_dir.mkdir(parents=True, exist_ok=True)
        
        # [修改] 源码目录初始化逻辑
        if BIO_SOURCE_INSTALL_DIR:
            self.source_root = Path(BIO_SOURCE_INSTALL_DIR)
            print(f"📂 Source Install Path (Config): {self.source_root}")
        else:
            self.source_root = self.root_dir / "source_tools"
            print(f"📂 Source Install Path (Default): {self.source_root}")
            
        self.source_root.mkdir(parents=True, exist_ok=True)

        # 4. 初始化数据
        self.conda_exe = self._detect_manager()
        self.conda_root = self._get_conda_root()
        self.registry = self._load_registry()
        
        # 5. 加载配置
        self.load_from_yaml(self.root_dir / "tools_setup.yaml")
        self.repair_registered_shims()
        

    # ==========================================
    # 辅助方法
    # ==========================================
    
    def _detect_manager(self):
        """探测 Conda/Mamba"""
        for executable in ("mamba", "conda"):
            configured = self._configured_manager_path(executable)
            if configured:
                return configured
        if shutil.which("mamba"): return "mamba"
        elif shutil.which("conda"): return "conda"
        return None

    def _configured_manager_path(self, executable: str) -> Optional[str]:
        if not ANACONDA_ROOT:
            return None
        root = Path(str(ANACONDA_ROOT)).expanduser()
        candidates = [
            root / "bin" / executable,
            root / "Scripts" / f"{executable}.exe",
            root / f"{executable}.exe",
            root / executable,
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    @staticmethod
    def _quote_shell_path(path: str) -> str:
        text = str(path or "")
        if not text:
            return text
        if os.name == "nt":
            return f'"{text}"' if re.search(r"\s", text) else text
        return shlex.quote(text)

    def _command_with_configured_manager(self, command: str) -> str:
        text = str(command or "").strip()
        if not text or not self.conda_exe:
            return text
        if re.match(r"^(conda|mamba)(?:\s|$)", text):
            return re.sub(
                r"^(conda|mamba)(?=\s|$)",
                lambda _match: self._quote_shell_path(self.conda_exe),
                text,
                count=1,
            )
        return text

    def _get_conda_root(self):
        """
        获取真实的 Conda 安装根目录 (用于在 shim 中 source conda.sh)
        """
        try:
            # 1. 尝试通过 conda info 获取 root_prefix
            cmd = [self.conda_exe, "info", "--json"] if self.conda_exe else ["conda", "info", "--json"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                # root_prefix 是 conda 安装的绝对路径
                return data.get("root_prefix")
        except: 
            pass
        if ANACONDA_ROOT:
            return str(Path(str(ANACONDA_ROOT)).expanduser())
            
        # 2. 兜底：尝试从当前环境推断 (如果当前是在 base 环境下)
        prefix = os.environ.get("CONDA_PREFIX")
        if prefix:
            # 如果路径包含 /envs/，说明是子环境，需要截取
            if "/envs/" in prefix:
                return prefix.split("/envs/")[0]
            # 否则假设当前就是 root (base)
            return prefix
            
        return None

    def _load_registry(self):
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text(encoding='utf-8'))
                return self._normalize_registry(data)
            except Exception:
                return self._default_registry()
        return self._default_registry()

    def _save_registry(self):
        self.registry_path.write_text(json.dumps(self.registry, indent=2), encoding='utf-8')

    def _default_registry(self) -> Dict[str, Dict[str, Any]]:
        return {"tools": {}, "environments": {}}

    def _normalize_registry(self, data: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        registry = self._default_registry()
        if isinstance(data, dict):
            registry["tools"] = dict(data.get("tools") or {})
            registry["environments"] = dict(data.get("environments") or {})

        normalized_tools = {}
        for tool_name, info in registry["tools"].items():
            record = dict(info or {})
            record.setdefault("name", tool_name)
            record.setdefault("env", record.get("env_name", ""))
            record.setdefault("env_name", record.get("env", ""))
            record.setdefault("capabilities", [])
            record.setdefault("status", record.get("status", "registered"))
            normalized_tools[tool_name] = record
        registry["tools"] = normalized_tools

        normalized_envs = {}
        for env_name, info in registry["environments"].items():
            record = dict(info or {})
            record["env"] = record.get("env") or env_name
            record["packages"] = self._ensure_list(record.get("packages"))
            record["capabilities"] = self._ensure_list(record.get("capabilities"))
            record["bound_tools"] = self._ensure_list(record.get("bound_tools"))
            record["skill_ids"] = self._ensure_list(record.get("skill_ids") or record.get("skill_id"))
            record["language"] = str(record.get("language", "") or "").lower()
            record["healthcheck"] = str(record.get("healthcheck", "") or "")
            record["status"] = record.get("status", "registered")
            normalized_envs[env_name] = record
        registry["environments"] = normalized_envs
        return registry

    def _ensure_list(self, value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple) or isinstance(value, set):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            parts = re.split(r"[,\n;]+", value)
            return [part.strip() for part in parts if part.strip()]
        return [str(value).strip()] if str(value).strip() else []

    def _merge_unique(self, existing, extra) -> List[str]:
        merged = []
        for item in self._ensure_list(existing) + self._ensure_list(extra):
            if item not in merged:
                merged.append(item)
        return merged

    def _status_rank(self, status: str) -> int:
        return self.STATUS_RANK.get(str(status or "").lower(), 0)

    def _best_status(self, existing: str, incoming: str) -> str:
        existing = str(existing or "").lower()
        incoming = str(incoming or "").lower()
        return incoming if self._status_rank(incoming) > self._status_rank(existing) else (existing or incoming)

    def _format_python_imports(self, packages: List[str]) -> str:
        imports = []
        for pkg in packages:
            token = re.split(r"[<>=!~]", str(pkg))[0].strip()
            if not token:
                continue
            normalized = token.replace("-", "_")
            imports.append(self.PYTHON_IMPORT_ALIASES.get(token.lower(), self.PYTHON_IMPORT_ALIASES.get(normalized.lower(), normalized)))
        return ", ".join(dict.fromkeys(imports))

    def infer_conda_env_from_path(self, path: Optional[str]) -> str:
        """Infer conda env name from paths like /conda/envs/<env>/bin/tool."""
        if not path:
            return ""
        text = str(path).replace("\\", "/")
        match = re.search(r"(?:^|/)envs/([^/]+)(?:/|$)", text)
        return match.group(1) if match else ""

    def _default_tool_env(self, tool_name: str) -> str:
        return f"bio_{str(tool_name).lower().replace('-', '_')}"

    def _is_default_tool_env(self, env_name: str, tool_name: str) -> bool:
        return str(env_name or "") == self._default_tool_env(tool_name)

    def _is_shim_like_path(self, path: Optional[str]) -> bool:
        if not path:
            return False
        parts = [part for part in str(path).replace("\\", "/").lower().split("/") if part]
        return any(part in self.SHIM_PATH_PARTS for part in parts)

    def _is_posix_absolute_path(self, path: Optional[str]) -> bool:
        text = str(path or "")
        return text.startswith("/") and not re.match(r"^/[A-Za-z]/", text)

    def _absolute_path_for_runtime(self, path: str) -> str:
        if self._is_posix_absolute_path(path):
            return str(path).rstrip("/") or "/"
        return os.path.abspath(path)

    def _join_runtime_path(self, base: str, *parts: str) -> str:
        if self._is_posix_absolute_path(base):
            return "/".join([str(base).rstrip("/"), *[str(part).strip("/") for part in parts if str(part)]])
        return os.path.join(base, *parts)

    def _candidate_tool_paths(
        self,
        env_name: str,
        tool_name: str,
        source_path: Optional[str] = None,
        source_executable: Optional[str] = None,
    ) -> List[str]:
        candidates: List[str] = []
        if source_executable:
            candidates.append(self._absolute_path_for_runtime(source_executable))
        if source_path:
            abs_path = self._absolute_path_for_runtime(source_path)
            candidates.append(self._join_runtime_path(abs_path, tool_name))
            if str(abs_path).replace("\\", "/").rstrip("/").split("/")[-1] != "bin":
                candidates.append(self._join_runtime_path(abs_path, "bin", tool_name))
        if self.conda_root:
            candidates.append(os.path.join(str(self.conda_root), "envs", env_name, "bin", tool_name))
        prefix = os.environ.get("CONDA_PREFIX", "")
        if prefix and Path(prefix).name == env_name:
            candidates.append(os.path.join(prefix, "bin", tool_name))

        unique: List[str] = []
        for candidate in candidates:
            if candidate and candidate not in unique and not self._is_shim_like_path(candidate):
                unique.append(candidate)
        return unique

    def _normalize_tool_registration_env(self, tool_name: str, env_name: str, source_path: Optional[str]) -> str:
        if self._is_shim_like_path(source_path):
            raise ValueError(
                f"Refusing to register tool '{tool_name}' from shim path: {source_path}. "
                "Register the real executable path, for example /path/to/conda/envs/ENV/bin/tool."
            )
        inferred_env = self.infer_conda_env_from_path(source_path)
        requested_env = str(env_name or "").strip()
        if inferred_env and requested_env and requested_env != inferred_env:
            if self._is_default_tool_env(requested_env, tool_name):
                logger.warning(
                    "Registering %s: replacing default env %s with env inferred from path: %s",
                    tool_name,
                    requested_env,
                    inferred_env,
                )
                return inferred_env
            raise ValueError(
                f"Refusing to register tool '{tool_name}' with env '{requested_env}' because "
                f"source_path belongs to conda env '{inferred_env}': {source_path}. "
                f"Use env='{inferred_env}' or remove the mismatched path."
            )
        return requested_env or inferred_env or self._default_tool_env(tool_name)

    def build_environment_healthcheck(
        self,
        env_name: str,
        language: str = "",
        packages=None,
        healthcheck: str = "",
    ) -> str:
        if healthcheck:
            return str(healthcheck).replace("{env}", env_name)

        language = (language or "").strip().lower()
        package_list = self._ensure_list(packages)
        if language == "r":
            if package_list:
                quoted = ",".join(f"'{pkg}'" for pkg in package_list)
                return (
                    f'conda run -n {env_name} Rscript -e "pkgs <- c({quoted}); '
                    "invisible(lapply(pkgs, function(p) suppressPackageStartupMessages(library(p, character.only=TRUE)))); "
                    'cat(\'OK\\n\')"'
                )
            return f'conda run -n {env_name} Rscript -e "cat(\'OK\\n\')"'

        if package_list:
            imports = self._format_python_imports(package_list)
            if imports:
                return f'conda run -n {env_name} python -c "import {imports}; print(\'OK\')"'
        return f'conda run -n {env_name} python -c "print(\'OK\')"'

    # ==========================================
    # 对外接口
    # ==========================================

    def get_shim_dir_str(self):
        return str(self.shim_dir)

    def list_installed_tools(self):
        tools = []
        if "tools" in self.registry:
            for name, info in self.registry["tools"].items():
                env_info = f"(Env: {info.get('env', 'unknown')}; status: {info.get('status', 'registered')})"
                tools.append(f"- {name} {env_info}")
        if "environments" in self.registry:
            for name, info in self.registry["environments"].items():
                purpose = info.get("purpose", "environment")
                status = info.get("status", "registered")
                packages = ", ".join(info.get("packages", []) or [])
                caps = ", ".join(info.get("capabilities", []) or [])
                bound_tools = ", ".join(info.get("bound_tools", []) or [])
                suffix = f"; packages: {packages}" if packages else ""
                if caps:
                    suffix += f"; capabilities: {caps}"
                if bound_tools:
                    suffix += f"; tools: {bound_tools}"
                tools.append(f"- env:{name} ({purpose}; status: {status}{suffix})")
        return "\n".join(tools) if tools else "None"

    def get_environment(self, env_name: str) -> Dict[str, Any]:
        return dict(self.registry.get("environments", {}).get(env_name, {}))

    def register_environment(
        self,
        env_name: str,
        purpose: str = "",
        packages=None,
        skill_id: str = None,
        notes: str = "",
        language: str = "",
        capabilities=None,
        healthcheck: str = "",
        status: str = "registered",
        tool_name: str = "",
        last_verified_at: str = "",
        healthchecks_by_skill=None,
    ):
        """Register a reusable environment even when it does not expose a command shim."""
        envs = self.registry.setdefault("environments", {})
        existing = dict(envs.get(env_name, {}))
        merged_packages = self._merge_unique(existing.get("packages"), packages)
        merged_caps = self._merge_unique(existing.get("capabilities"), capabilities)
        merged_skills = self._merge_unique(existing.get("skill_ids"), [skill_id] if skill_id else [])
        merged_tools = self._merge_unique(existing.get("bound_tools"), [tool_name] if tool_name else [])
        selected_language = str(language or existing.get("language", "")).lower()
        merged_healthchecks_by_skill = dict(existing.get("healthchecks_by_skill") or {})
        if isinstance(healthchecks_by_skill, dict):
            for key, value in healthchecks_by_skill.items():
                if str(key or "").strip() and str(value or "").strip():
                    merged_healthchecks_by_skill[str(key).strip()] = str(value).strip()
        skill_healthcheck = ""
        if skill_id:
            skill_healthcheck = str(healthcheck or merged_healthchecks_by_skill.get(skill_id, "") or "").strip()
        selected_healthcheck = self.build_environment_healthcheck(
            env_name=env_name,
            language=selected_language,
            packages=merged_packages,
            healthcheck=skill_healthcheck or healthcheck or existing.get("healthcheck", ""),
        )
        if skill_id and selected_healthcheck:
            merged_healthchecks_by_skill[skill_id] = selected_healthcheck
        envs[env_name] = {
            "env": env_name,
            "purpose": purpose or existing.get("purpose") or "Reusable environment",
            "language": selected_language,
            "packages": merged_packages,
            "capabilities": merged_caps,
            "healthcheck": selected_healthcheck,
            "healthchecks_by_skill": merged_healthchecks_by_skill,
            "status": "broken" if str(status or "").lower() == "broken" else self._best_status(existing.get("status", "registered"), status or "registered"),
            "notes": notes or existing.get("notes", ""),
            "skill_ids": merged_skills,
            "bound_tools": merged_tools,
            "registered_at": existing.get("registered_at", datetime.now().isoformat()),
            "last_verified_at": last_verified_at or existing.get("last_verified_at", ""),
        }
        self._save_registry()
        return True

    def merge_registry_data(self, data: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """Merge external software registry JSON without downgrading verified records."""
        stats = {"tools": 0, "environments": 0}
        if not isinstance(data, dict):
            return stats

        for env_name, info in (data.get("environments") or {}).items():
            if not env_name or not isinstance(info, dict):
                continue
            self.register_environment(
                env_name=str(info.get("env") or env_name),
                purpose=str(info.get("purpose") or ""),
                packages=info.get("packages"),
                skill_id=None,
                notes=str(info.get("notes") or ""),
                language=str(info.get("language") or ""),
                capabilities=info.get("capabilities"),
                healthcheck=str(info.get("healthcheck") or ""),
                healthchecks_by_skill=info.get("healthchecks_by_skill"),
                status=str(info.get("status") or "registered"),
                tool_name="",
                last_verified_at=str(info.get("last_verified_at") or ""),
            )
            record = self.registry.setdefault("environments", {}).setdefault(str(info.get("env") or env_name), {})
            record["skill_ids"] = self._merge_unique(record.get("skill_ids"), info.get("skill_ids") or info.get("skill_id"))
            record["bound_tools"] = self._merge_unique(record.get("bound_tools"), info.get("bound_tools"))
            self._save_registry()
            stats["environments"] += 1

        for tool_name, info in (data.get("tools") or {}).items():
            if not tool_name or not isinstance(info, dict):
                continue
            tools = self.registry.setdefault("tools", {})
            existing = dict(tools.get(tool_name, {}))
            merged = dict(existing)
            merged.update(info)
            merged["name"] = str(merged.get("name") or tool_name)
            merged["env"] = str(merged.get("env") or merged.get("env_name") or existing.get("env") or "")
            merged["env_name"] = str(merged.get("env_name") or merged.get("env") or "")
            merged["status"] = self._best_status(existing.get("status", "registered"), info.get("status", "registered"))
            tools[tool_name] = merged
            if merged.get("env"):
                self.bind_tool_to_environment(str(tool_name), str(merged["env"]))
            else:
                self._save_registry()
            stats["tools"] += 1
        return stats

    def find_matching_environment(self, requirements: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        requirements = requirements or {}
        req_language = str(requirements.get("language", "") or "").lower()
        req_packages = set(self._ensure_list(requirements.get("packages")))
        req_caps = set(self._ensure_list(requirements.get("capabilities")))

        best_match = None
        best_score = -1
        for env_name, info in (self.registry.get("environments") or {}).items():
            env_language = str(info.get("language", "") or "").lower()
            if req_language and env_language and req_language != env_language:
                continue

            env_packages = set(self._ensure_list(info.get("packages")))
            env_caps = set(self._ensure_list(info.get("capabilities")))
            matched_packages = req_packages & env_packages
            matched_caps = req_caps & env_caps
            missing_packages = sorted(req_packages - env_packages)
            missing_caps = sorted(req_caps - env_caps)
            is_exact = not missing_packages and not missing_caps
            score = len(matched_packages) * 10 + len(matched_caps) * 5
            if is_exact:
                score += 100
            if req_language and env_language == req_language:
                score += 3
            if score <= best_score:
                continue
            best_score = score
            best_match = {
                "env": env_name,
                "match_type": "exact" if is_exact else "partial",
                "matched_packages": sorted(matched_packages),
                "matched_capabilities": sorted(matched_caps),
                "missing_packages": missing_packages,
                "missing_capabilities": missing_caps,
                "record": dict(info),
            }
        return best_match

    def verify_environment(self, env_name: str, language: str = "", healthcheck: str = "") -> tuple:
        info = self.registry.setdefault("environments", {}).get(env_name)
        if not info:
            return False, f"Environment '{env_name}' is not registered."

        selected_language = str(language or info.get("language", "")).lower()
        command = self.build_environment_healthcheck(
            env_name=env_name,
            language=selected_language,
            packages=info.get("packages"),
            healthcheck=healthcheck or info.get("healthcheck", ""),
        )
        info["healthcheck"] = command
        run_command = self._command_with_configured_manager(command)
        try:
            proc = subprocess.run(run_command, shell=True, capture_output=True, text=True, timeout=180)
            ok = proc.returncode == 0
            info["status"] = "verified" if ok else "broken"
            info["last_verified_at"] = datetime.now().isoformat()
            info["last_healthcheck"] = run_command
            combined = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part).strip()
            if combined:
                info["notes"] = combined[:1000]
            self._save_registry()
            return ok, combined or ("OK" if ok else "Healthcheck failed.")
        except Exception as e:
            info["status"] = "broken"
            info["last_verified_at"] = datetime.now().isoformat()
            info["notes"] = str(e)
            self._save_registry()
            return False, str(e)

    def bind_tool_to_environment(self, tool_name: str, env_name: str):
        tools = self.registry.setdefault("tools", {})
        envs = self.registry.setdefault("environments", {})
        tool_record = dict(tools.get(tool_name, {}))
        tool_record["name"] = tool_name
        tool_record["env"] = env_name
        tool_record["env_name"] = env_name
        tools[tool_name] = tool_record

        env_record = dict(envs.get(env_name, {}))
        env_record["env"] = env_name
        env_record["bound_tools"] = self._merge_unique(env_record.get("bound_tools"), [tool_name])
        env_record.setdefault("packages", [])
        env_record.setdefault("capabilities", [])
        env_record.setdefault("status", "registered")
        envs[env_name] = env_record
        self._save_registry()
        return True

    def _extract_extra_setup_from_shim(self, text: str) -> str:
        marker = "# 4. Inject Extra Configs"
        execute_marker = "\n# 5. Execute"
        start = text.find(marker)
        end = text.find(execute_marker)
        if start < 0 or end < 0 or end <= start:
            return ""
        setup = text[start:end].split("\n", 1)
        return setup[1].strip("\n") if len(setup) == 2 else ""

    def _infer_shim_env_from_text(self, tool_name: str, text: str) -> str:
        """Infer the real conda env used by a shim's executable candidates."""
        env_counts: Dict[str, int] = {}
        for match in re.finditer(
            rf"(?:^|[\s\"'])/(?P<path>[^\"'\s]*?/envs/(?P<env>[^/]+)/bin/{re.escape(tool_name)})(?:$|[\s\"'])",
            str(text or ""),
        ):
            env = match.group("env")
            env_counts[env] = env_counts.get(env, 0) + 2
        for match in re.finditer(r"/envs/(?P<env>[^/]+)/bin(?=$|[/:\"'\s])", str(text or "")):
            env = match.group("env")
            env_counts[env] = env_counts.get(env, 0) + 1
        if not env_counts:
            return ""
        return sorted(env_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    def _registered_shim_repair_env(self, tool_name: str, record: Dict[str, Any], shim_path: Path) -> str:
        env_name = str(record.get("env") or record.get("env_name") or "").strip()
        if not env_name or not self._is_default_tool_env(env_name, tool_name):
            return env_name

        inferred = self.infer_conda_env_from_path(record.get("source_path"))
        if not inferred and shim_path.exists():
            try:
                inferred = self._infer_shim_env_from_text(tool_name, shim_path.read_text(encoding="utf-8"))
            except Exception:
                inferred = ""
        if inferred and inferred != env_name:
            logger.warning(
                "Repairing stale shim for %s: replacing default env %s with env inferred from executable path: %s",
                tool_name,
                env_name,
                inferred,
            )
            record["env"] = inferred
            record["env_name"] = inferred
            return inferred
        return env_name

    def _shim_needs_repair(self, tool_name: str, shim_path: Path, env_name: str = "", record: Optional[Dict[str, Any]] = None) -> bool:
        if not shim_path.exists():
            return True
        try:
            text = shim_path.read_text(encoding="utf-8")
        except Exception:
            return True
        if env_name and self._is_default_tool_env(env_name, tool_name):
            inferred = self.infer_conda_env_from_path((record or {}).get("source_path"))
            inferred = inferred or self._infer_shim_env_from_text(tool_name, text)
            if inferred and inferred != env_name:
                return True
        if "Resolve the real executable explicitly" in text and 'exec "$real_tool" "$@"' in text:
            return False
        return f'exec {tool_name} "$@"' in text or f"exec {tool_name} " in text

    def repair_registered_shims(self) -> int:
        """Rewrite stale recursive shims from the software registry."""
        repaired = 0
        for tool_name, record in list((self.registry.get("tools") or {}).items()):
            env_name = str(record.get("env") or record.get("env_name") or "").strip()
            if not tool_name or not env_name:
                continue
            shim_path = Path(record.get("shim_path") or (self.shim_dir / tool_name))
            original_env_name = env_name
            env_name = self._registered_shim_repair_env(str(tool_name), record, shim_path)
            env_changed = env_name != original_env_name
            if not env_changed and not self._shim_needs_repair(str(tool_name), shim_path, env_name=env_name, record=record):
                continue
            extra_setup = ""
            if shim_path.exists():
                try:
                    extra_setup = self._extract_extra_setup_from_shim(shim_path.read_text(encoding="utf-8"))
                except Exception:
                    extra_setup = ""
            source_path = record.get("source_path")
            if self._is_shim_like_path(source_path):
                source_path = None
            if self.infer_conda_env_from_path(source_path) == env_name:
                extra_setup = ""
            new_shim = self._create_shim(
                env_name,
                str(tool_name),
                extra_setup=extra_setup,
                source_path=source_path,
            )
            record["shim_path"] = str(new_shim)
            record["shim_version"] = "realpath-v2"
            repaired += 1
        if repaired:
            self._save_registry()
            logger.info("Repaired %s stale BioMind shim(s).", repaired)
        return repaired

    def install_package(self, tool_name: str, specific_cmd: str = None) -> tuple:
        """自动安装 (Conda)"""
        if not self.conda_exe: return False, "Conda/Mamba not found."
        tool_name = tool_name.lower()
        env_name = self._default_tool_env(tool_name)
        logger.info(f"📦 Installing {tool_name} to {env_name}...")
        
        cmd = specific_cmd if specific_cmd else f"{self.conda_exe} create -n {env_name} -c bioconda -c conda-forge {tool_name} -y"
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if proc.returncode != 0: return False, f"Install failed: {proc.stderr}"
            
            shim_path = self._create_shim(env_name, tool_name)
            self.registry.setdefault("tools", {})[tool_name] = {
                "name": tool_name, "env": env_name, "shim_path": str(shim_path),
                "install_time": datetime.now().isoformat(), "category": "Auto Installed"
            }
            self.register_environment(
                env_name=env_name,
                purpose=f"Runtime for tool {tool_name}",
                capabilities=[f"tool:{tool_name}"],
                status="installed",
                tool_name=tool_name,
            )
            self.bind_tool_to_environment(tool_name, env_name)
            self._save_registry()
            return True, f"Installed {tool_name} successfully."
        except Exception as e: return False, str(e)

    # =========================================================
    # [核心修改] 注册现有工具 (支持 setup_script 和 路径优化)
    # =========================================================
    def register_existing_tool(self, tool_name: str, env_name: str, source_path: str = None, setup_script: str = None):
        """
        注册工具并生成 Shim
        :param tool_name: 工具名称/命令名
        :param env_name: 依赖的 Conda 环境名
        :param source_path: 源码所在目录 (会自动添加到 PATH)
        :param setup_script: [新增] 特异性环境配置脚本 (如 export PERL5LIB=...)
        """
        original_tool_name = str(tool_name or "").strip()
        if self._is_shim_like_path(original_tool_name):
            raise ValueError(
                f"Refusing to register shim path as tool command: {original_tool_name}. "
                "Use the command name plus the real executable directory instead."
            )
        tool_name = original_tool_name
        
        # 自动清洗：如果传入路径，只取文件名
        if "/" in tool_name: tool_name = os.path.basename(tool_name)
        if "\\" in tool_name: tool_name = os.path.basename(tool_name)
        
        # 自动清洗：如果 source_path 指向文件，自动转为目录
        source_executable = None
        if self._is_shim_like_path(source_path):
            raise ValueError(
                f"Refusing to register tool '{tool_name}' from shim path: {source_path}. "
                "Use the real executable path instead of agent_shim/bin_shims."
            )
        if source_path and os.path.isfile(source_path):
            source_executable = os.path.abspath(source_path)
            source_path = os.path.dirname(source_path)
        env_name = self._normalize_tool_registration_env(tool_name, env_name, source_path)

        try:
            final_setup = ""
            
            # 1. 处理路径 (同时添加 root 和 bin)
            if source_path:
                abs_path = os.path.abspath(source_path)
                inferred_env = self.infer_conda_env_from_path(abs_path)
                if inferred_env and inferred_env == env_name:
                    logger.info(
                        "Registering %s from conda env %s; skipping redundant PATH/PYTHONPATH injection.",
                        tool_name,
                        env_name,
                    )
                else:
                    # 这里的修改解决了 3d-dna 等脚本在根目录的问题
                    final_setup += f'export PATH="{abs_path}:{abs_path}/bin:$PATH"\n'
                    final_setup += f'export PYTHONPATH="{abs_path}:$PYTHONPATH"\n'

            # 2. [新增] 注入自定义 Setup (来自 Agent/知识库)
            if setup_script:
                final_setup += "\n# --- Custom Setup from Agent ---\n"
                final_setup += setup_script + "\n"
                final_setup += "# -----------------------------\n"

            # 3. 创建 Shim
            shim_path = self._create_shim(
                env_name,
                tool_name,
                extra_setup=final_setup,
                source_path=source_path,
                source_executable=source_executable,
            )
            
            # 4. 更新注册表
            self.registry.setdefault("tools", {})[tool_name] = {
                "status": "registered", 
                "env": env_name, 
                "env_name": env_name,
                "shim_path": str(shim_path),
                "source_path": source_path,
                "custom_setup": bool(setup_script),
                "install_time": datetime.now().isoformat(), 
                "category": "Manual Registered"
            }
            self.register_environment(
                env_name=env_name,
                purpose=f"Runtime for tool {tool_name}",
                capabilities=[f"tool:{tool_name}"],
                status="registered",
                tool_name=tool_name,
            )
            self.bind_tool_to_environment(tool_name, env_name)
            self._save_registry()
            print(f"✅ Registered tool: {tool_name}")
            return True
        except Exception as e:
            logger.error(f"Registration failed: {e}")
            raise e # 抛出异常让 Agent 知道

    def load_from_yaml(self, config_path):
        """加载 YAML 配置"""
        config_file = Path(config_path)
        if not config_file.exists(): return
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if not data or 'tools' not in data: return
            
            print(f"⚙️  Loading config: {config_file}")
            count = 0
            for tool, conf in data['tools'].items():
                if conf.get('env'):
                    self.register_existing_tool(
                        tool, 
                        conf.get('env'), 
                        conf.get('path'), 
                        conf.get('setup')
                    )
                    count += 1
            if count > 0: print(f"   Loaded {count} tools.")
        except Exception as e: logger.error(f"YAML load failed: {e}")

    # =========================================================
    # [核心修改] 生成 Shim (注入 extra_setup)
    # =========================================================
    def _create_shim(
        self,
        env_name,
        tool_name,
        extra_setup="",
        source_path: Optional[str] = None,
        source_executable: Optional[str] = None,
    ):
        """生成 Shim 启动脚本"""
        shim_path = self.shim_dir / tool_name
        
        # 获取真实的 conda 安装根目录
        real_conda_root = self.conda_root or os.environ.get("CONDA_PREFIX", "")
        # 如果获取到的是子环境路径，尝试截取根目录
        if "/envs/" in real_conda_root:
            real_conda_root = real_conda_root.split("/envs/")[0]

        tool_name_literal = shlex.quote(str(tool_name))
        candidate_lines = [
            f'    "$CONDA_PREFIX/bin/{tool_name}"',
        ]
        for candidate in self._candidate_tool_paths(env_name, tool_name, source_path, source_executable):
            candidate_lines.append(f"    {shlex.quote(candidate)}")
        candidate_block = "\n".join(candidate_lines)

        script = f"""#!/bin/bash
# BioMind Shim for {tool_name}
# Env: {env_name}

# 1. Clean Environment (防止父进程变量污染)
unset PYTHONPATH
unset PYTHONHOME
unset PERL5LIB

# 2. Init Conda Hook (指向真实的 Conda 位置)
# 尝试使用 conda shell.bash hook
__conda_setup="$('{self.conda_exe or 'conda'}' 'shell.bash' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    # 回退方案：直接 source 真实路径下的 conda.sh
    if [ -f "{real_conda_root}/etc/profile.d/conda.sh" ]; then
        . "{real_conda_root}/etc/profile.d/conda.sh"
    fi
fi
unset __conda_setup

# 3. Activate Environment
conda activate {env_name}
activate_rc=$?
if [ $activate_rc -ne 0 ]; then
    echo "BioMind shim error: failed to activate conda env '{env_name}' for tool '{tool_name}'." >&2
    echo "This shim is stale or the environment was not registered correctly." >&2
    echo "Verify the real tool path and re-register it to an existing environment." >&2
    exit $activate_rc
fi

# 4. Inject Extra Configs (注入 Agent 自动配置的路径)
{extra_setup}

# 5. Execute
# Resolve the real executable explicitly. Do not run bare `{tool_name}` here:
# if this shim directory is first in PATH, a bare exec can recursively call
# this shim and make the agent look stuck.
shim_self="$(readlink -f "$0" 2>/dev/null || printf '%s\\n' "$0")"
real_tool=""
real_tool_candidates=(
{candidate_block}
)
for candidate in "${{real_tool_candidates[@]}}"; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate" ] || continue
    candidate_real="$(readlink -f "$candidate" 2>/dev/null || printf '%s\\n' "$candidate")"
    [ "$candidate_real" = "$shim_self" ] && continue
    real_tool="$candidate"
    break
done

if [ -z "$real_tool" ]; then
    shim_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    search_path=":$PATH:"
    search_path="${{search_path//:$shim_dir:/:}}"
    search_path="${{search_path#:}}"
    search_path="${{search_path%:}}"
    real_tool="$(PATH="$search_path" command -v {tool_name_literal} 2>/dev/null || true)"
    if [ -n "$real_tool" ]; then
        real_tool_real="$(readlink -f "$real_tool" 2>/dev/null || printf '%s\\n' "$real_tool")"
        [ "$real_tool_real" = "$shim_self" ] && real_tool=""
    fi
fi

if [ -z "$real_tool" ]; then
    echo "BioMind shim error: could not resolve real executable for tool '{tool_name}' in env '{env_name}'." >&2
    echo "The shim refused to call itself recursively. Re-register the tool with its real executable path." >&2
    exit 127
fi

exec "$real_tool" "$@"
"""
        shim_path.write_text(script, encoding='utf-8')
        shim_path.chmod(0o755)
        return shim_path
