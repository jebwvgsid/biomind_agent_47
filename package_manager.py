from agent_core.cli_display import display_print
import os
import json
import re
import shutil
import shlex
import subprocess
import logging
import sys
import copy
import fcntl
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
import yaml  # pip install pyyaml

# =========================================================
# 1. 配置加载与路径修复
# =========================================================
# Metadata readers/writers must not import application configuration (which may
# load credentials), discover managers, scan environments, or repair shims.
BIO_SOFTWARE_ROOT = None
BIO_SOURCE_INSTALL_DIR = None
ANACONDA_ROOT = None


def _load_legacy_configuration():
    global BIO_SOFTWARE_ROOT, BIO_SOURCE_INSTALL_DIR, ANACONDA_ROOT
    try:
        from config import config as config_module
    except Exception:
        return
    BIO_SOFTWARE_ROOT = getattr(config_module, "BIO_SOFTWARE_ROOT", BIO_SOFTWARE_ROOT)
    BIO_SOURCE_INSTALL_DIR = getattr(config_module, "BIO_SOURCE_INSTALL_DIR", BIO_SOURCE_INSTALL_DIR)
    ANACONDA_ROOT = getattr(config_module, "ANACONDA_ROOT", ANACONDA_ROOT)


class RegistryError(ValueError):
    """A metadata error with a stable code and the actual authority path."""
    def __init__(self, code, path, detail=""):
        self.code, self.registry_path, self.detail = code, str(path), detail
        super().__init__(f"{code}: {path}: {detail}")


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

    @classmethod
    def for_registry(cls, registry_path, *, work_dir=None, conda_root=None, conda_exe=None):
        """Open the existing authority without initialization or software probes."""
        return cls(work_dir or str(Path(registry_path).parent), registry_path=registry_path,
                   metadata_only=True, conda_root=conda_root, conda_exe=conda_exe)

    def __init__(self, work_dir: str = "./", *, registry_path=None, metadata_only=False,
                 conda_root=None, conda_exe=None):
        if metadata_only:
            self.work_dir = Path(work_dir).absolute()
            self.registry_path = Path(registry_path).absolute()
            self.root_dir = self.registry_path.parent
            self.shim_dir = self.root_dir / "bin_shims"
            self.source_root = self.root_dir / "source_tools"
            self.conda_root, self.conda_exe = conda_root, conda_exe
            self.registry = self._load_registry()
            return
        _load_legacy_configuration()
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
        
        display_print(f"Software Repository: {self.root_dir}", purpose='details')

        # 3. 定义关键路径
        self.registry_path = self.root_dir / "software_registry.json"
        
        self.shim_dir = self.root_dir / "bin_shims"
        self.shim_dir.mkdir(parents=True, exist_ok=True)
        
        # [修改] 源码目录初始化逻辑
        if BIO_SOURCE_INSTALL_DIR:
            self.source_root = Path(BIO_SOURCE_INSTALL_DIR)
            display_print(f"Source Install Path (Config): {self.source_root}", purpose='details')
        else:
            self.source_root = self.root_dir / "source_tools"
            display_print(f"Source Install Path (Default): {self.source_root}", purpose='details')
            
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
        # `mamba run` takes the package-manager lock. A healthcheck is a
        # non-mutating execution request and must remain usable while another
        # environment is being solved or installed with mamba.
        if re.match(r"^(conda|mamba)\s+run\b", text):
            configured = Path(self.conda_exe)
            conda_sibling = configured.with_name("conda")
            runner = str(conda_sibling) if conda_sibling.is_file() else (shutil.which("conda") or self.conda_exe)
            return re.sub(
                r"^(conda|mamba)(?=\s|$)",
                lambda _match: self._quote_shell_path(runner),
                text,
                count=1,
            )
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
        # Prefer deterministic local path inference. `conda info` may wait on
        # package-manager/plugin locks for tens of seconds on shared clusters.
        if self.conda_exe:
            manager_path = Path(str(self.conda_exe)).expanduser()
            if manager_path.parent.name in {"bin", "Scripts"}:
                inferred_root = manager_path.parent.parent
                if (inferred_root / "conda-meta").is_dir():
                    return str(inferred_root)
        try:
            # Fall back to conda itself for wrappers and non-standard layouts.
            cmd = [self.conda_exe, "info", "--json"] if self.conda_exe else ["conda", "info", "--json"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                # root_prefix 是 conda 安装的绝对路径
                return data.get("root_prefix")
        except: 
            pass
        if self.conda_exe:
            manager_path = Path(str(self.conda_exe)).expanduser()
            if manager_path.parent.name in {"bin", "Scripts"}:
                inferred_root = manager_path.parent.parent
                if inferred_root.is_dir():
                    return str(inferred_root)
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

    def _read_registry(self):
        try:
            text = self.registry_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._default_registry(), False
        except (OSError, UnicodeError) as exc:
            raise RegistryError("REGISTRY_UNREADABLE", self.registry_path, str(exc)) from exc
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise RegistryError("REGISTRY_CORRUPT", self.registry_path, str(exc)) from exc
        self._validate_registry(data)
        return self._normalize_registry(data), True

    def _validate_registry(self, data):
        if not isinstance(data, dict):
            raise RegistryError("REGISTRY_SCHEMA_INCOMPATIBLE", self.registry_path, "expected object")
        if data.get("schema_version", 1) != 1:
            raise RegistryError("REGISTRY_SCHEMA_INCOMPATIBLE", self.registry_path, "unsupported schema_version")
        for section in ("tools", "environments"):
            records = data.get(section, {})
            if not isinstance(records, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                                   for k, v in records.items()):
                raise RegistryError("REGISTRY_SCHEMA_INCOMPATIBLE", self.registry_path, section)
            for name, record in records.items():
                declared = record.get("name") if section == "tools" else record.get("env")
                if declared and declared != name:
                    raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path, f"{section}.{name}")
                if section == "environments" and record.get("prefix") and record.get("path") and record["prefix"] != record["path"]:
                    raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path, f"environments.{name}.prefix/path")
                if section == "tools" and record.get("env") and record.get("env_name") and record["env"] != record["env_name"]:
                    raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path, f"tools.{name}.env_name")

    def _load_registry(self):
        data, initialized = self._read_registry()
        self._registry_baseline = copy.deepcopy(data)
        self.registry_initialized = initialized
        return data

    def _identity_conflicts(self, old, new):
        for section in ("environments", "tools"):
            for name, record in new.get(section, {}).items():
                previous = old.get(section, {}).get(name, {})
                fields = ("prefix", "path", "backend", "scope", "host", "execution_scope")
                if section == "tools":
                    fields += ("env", "env_name", "source_path", "source_executable")
                for field in fields:
                    before, after = previous.get(field), record.get(field)
                    if before not in (None, "", {}) and after not in (None, "", {}) and before != after:
                        raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path,
                                            f"{section}.{name}.{field}: {before!r} != {after!r}")

    def _atomic_write_registry(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.registry_path.parent,
                                             prefix=f".{self.registry_path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self.registry_path.exists():
                temporary.chmod(self.registry_path.stat().st_mode & 0o777)
            os.replace(temporary, self.registry_path)
            temporary = None
            # A directory fsync failure happens *after* commit: expose that fact
            # in the receipt instead of inviting a repeated software install.
            try:
                descriptor = os.open(self.registry_path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                return str(exc)
            return ""
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _update_registry(self, mutation):
        self.last_registry_receipt = {"committed": False, "registry_path": str(self.registry_path),
                                      "source_type": "software_registry"}
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        # This separate inode is never replaced by a writer.
        lock_path = self.registry_path.with_name(self.registry_path.name + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                latest, initialized = self._read_registry()
                updated = copy.deepcopy(latest)
                mutation(updated)
                self._validate_registry(updated)
                self._identity_conflicts(latest, updated)
                for name, record in updated.get("environments", {}).items():
                    old_status = latest.get("environments", {}).get(name, {}).get("status")
                    if record.get("status") in self.INSTALLED_STATUSES and old_status not in self.INSTALLED_STATUSES:
                        if not any(record.get(key) for key in ("prefix", "path", "env_runner", "runner", "runner_argv", "launcher")):
                            raise RegistryError("REGISTRY_LAUNCH_INCOMPLETE", self.registry_path, name)
                changed = updated != latest or not initialized
                durability_warning = self._atomic_write_registry(updated) if changed else ""
                self.registry = updated
                self._registry_baseline = copy.deepcopy(updated)
                self.registry_initialized = True
                self.last_registry_receipt = {"committed": True, "changed": changed,
                    "registry_path": str(self.registry_path), "source_type": "software_registry",
                    "durability_warning": durability_warning}
                return updated
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _merge_delta(self, baseline, desired, latest, trail=()):
        """Three-way merge for legacy callers that mutate registry then save.

        Only fields changed since this instance's read may be written. Independent
        mapping/list additions merge; conflicting scalar updates fail visibly.
        """
        missing = object()
        merged = copy.deepcopy(latest)
        for key in baseline.keys() | desired.keys():
            old, new, current = baseline.get(key, missing), desired.get(key, missing), latest.get(key, missing)
            if old == new:
                continue
            location = (*trail, str(key))
            if new is missing:
                # Replacing a partial legacy dictionary is not an explicit
                # deletion request. Use register_environment(remove_fields=...).
                continue
            elif isinstance(new, dict) and (old is missing or isinstance(old, dict)) and (current is missing or isinstance(current, dict)):
                merged[key] = self._merge_delta({} if old is missing else old, new,
                                                {} if current is missing else current, location)
            elif key in {"packages", "capabilities", "bound_tools", "skill_ids"} and isinstance(new, list):
                removed = set(old if isinstance(old, list) else []) - set(new)
                merged[key] = [item for item in self._merge_unique([] if current is missing else current, new) if item not in removed]
            elif current is missing or current == old or current == new:
                merged[key] = copy.deepcopy(new)
            else:
                raise RegistryError("REGISTRY_UPDATE_CONFLICT", self.registry_path, ".".join(location))
        return merged

    def _save_registry(self):
        desired = copy.deepcopy(self.registry)
        baseline = copy.deepcopy(getattr(self, "_registry_baseline", self._default_registry()))
        def apply(latest):
            merged = self._merge_delta(baseline, desired, latest)
            latest.clear()
            latest.update(merged)
        self._update_registry(apply)

    def _default_registry(self) -> Dict[str, Dict[str, Any]]:
        return {"tools": {}, "environments": {}}

    def _normalize_registry(self, data: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        registry = copy.deepcopy(data) if isinstance(data, dict) else self._default_registry()
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

    def _known_environment_prefixes(self) -> Dict[str, List[str]]:
        """Read path metadata only; never invoke Conda or health checks."""
        found: Dict[str, List[str]] = {}
        def include(raw):
            path = Path(str(raw or ""))
            if path.is_absolute() and path.is_dir():
                value = str(path.resolve())
                if value not in found.setdefault(path.name, []):
                    found[path.name].append(value)
        include(os.environ.get("CONDA_PREFIX"))
        for raw in getattr(self, "known_environment_prefixes", []) or []:
            include(raw)
        try:
            listing = Path.home() / ".conda" / "environments.txt"
            if listing.is_file():
                for raw in listing.read_text(encoding="utf-8").splitlines():
                    include(raw.strip())
        except (OSError, UnicodeError):
            pass
        root = Path(str(self.conda_root)) / "envs" if self.conda_root else None
        if root and root.is_dir():
            for path in sorted(root.iterdir())[:128]:
                include(path)
        return found

    def _registered_prefixes(self, env_name: str, record: Dict[str, Any], known=None) -> List[str]:
        explicit = str(record.get("prefix") or record.get("path") or
                       (record.get("resolved_runtime_paths") or {}).get("RESOLVED_ENV_PREFIX") or "")
        if explicit:
            return [str(Path(explicit).resolve())] if Path(explicit).is_absolute() else []
        return list((known if known is not None else self._known_environment_prefixes()).get(env_name, []))

    def _environment_runtime_available(self, env_name: str, info: Optional[Dict[str, Any]] = None) -> bool:
        """Eligibility is registration and unambiguous path metadata, not a probe."""
        record = dict(info or (self.registry.get("environments") or {}).get(env_name, {}) or {})
        if str(record.get("status") or "").lower() in {"broken", "disabled", "invalid", "failed"}:
            return False
        prefixes = self._registered_prefixes(str(env_name or ""), record)
        return len(prefixes) == 1 and Path(prefixes[0]).is_dir()

    def environment_runtime_available(self, env_name: str) -> bool:
        """Public read-only environment presence check used by runtime preflight."""
        return self._environment_runtime_available(env_name, self.get_environment(env_name))

    def _direct_environment_healthcheck(self, env_name: str, command: str) -> tuple:
        """Run a read-only healthcheck from an existing prefix without `conda run` startup."""
        if not self.conda_root:
            return str(command or ""), None
        prefix = Path(self.conda_root) / "envs" / str(env_name or "")
        bin_dir = prefix / ("Scripts" if os.name == "nt" else "bin")
        if not prefix.is_dir() or not bin_dir.is_dir():
            return str(command or ""), None
        env_pattern = re.compile(
            rf"(?<!\S)(?:(?:\S*/)?(?:conda|mamba|micromamba))\s+run\s+-n\s+{re.escape(str(env_name))}\s+"
        )
        direct_command = env_pattern.sub("", str(command or ""))
        if direct_command == str(command or ""):
            return direct_command, None
        process_env = os.environ.copy()
        process_env["CONDA_PREFIX"] = str(prefix)
        process_env["CONDA_DEFAULT_ENV"] = str(env_name)
        process_env["PATH"] = str(bin_dir) + os.pathsep + process_env.get("PATH", "")
        return direct_command, process_env

    def recent_environment_healthcheck(
        self,
        env_name: str,
        healthcheck: str,
        max_age_seconds: float = 3600.0,
    ) -> Optional[str]:
        """Return persisted evidence only when the same healthcheck passed recently."""
        record = self.get_environment(env_name)
        if str(record.get("status") or "").lower() not in self.INSTALLED_STATUSES:
            return None
        recorded = str(record.get("last_healthcheck") or "").strip()
        recorded, _recorded_env = self._direct_environment_healthcheck(env_name, recorded)
        expected, _process_env = self._direct_environment_healthcheck(env_name, healthcheck)
        normalize = lambda value: re.sub(r"\s+", " ", str(value or "")).strip()
        if not recorded or normalize(recorded) != normalize(expected):
            return None
        verified_at = str(record.get("last_verified_at") or "").strip()
        if not verified_at:
            return None
        try:
            age = (datetime.now() - datetime.fromisoformat(verified_at)).total_seconds()
        except (TypeError, ValueError):
            return None
        if age < 0 or age > max(0.0, float(max_age_seconds)):
            return None
        return str(record.get("notes") or "Cached runtime healthcheck passed.")

    def list_installed_tools(self):
        tools = []
        if "tools" in self.registry:
            for name, info in self.registry["tools"].items():
                env_name = str(info.get("env") or info.get("env_name") or "")
                env_record = (self.registry.get("environments") or {}).get(env_name, {})
                source_path = str(info.get("source_path") or "")
                source_available = bool(source_path and Path(source_path).exists())
                if env_name and not source_available and not self._environment_runtime_available(env_name, env_record):
                    continue
                env_info = f"(Env: {info.get('env', 'unknown')}; status: {info.get('status', 'registered')})"
                tools.append(f"- {name} {env_info}")
        if "environments" in self.registry:
            for name, info in self.registry["environments"].items():
                if not self._environment_runtime_available(name, info):
                    continue
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

    def _merge_record_fields(self, existing, patch):
        result = copy.deepcopy(existing)
        for key, value in patch.items():
            if key in {"packages", "capabilities", "skill_ids", "bound_tools"}:
                result[key] = self._merge_unique(existing.get(key), value)
            elif isinstance(value, dict) and isinstance(existing.get(key), dict):
                result[key] = self._merge_record_fields(existing[key], value)
            elif key == "evidence" and isinstance(value, list) and isinstance(existing.get(key), list):
                result[key] = copy.deepcopy(existing[key])
                for item in value:
                    if item not in result[key]:
                        result[key].append(copy.deepcopy(item))
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _environment_patch(self, registry, env_name, patch, remove_fields=()):
        if not isinstance(env_name, str) or not env_name.strip():
            raise RegistryError("REGISTRY_IDENTITY_INVALID", self.registry_path, "empty environment name")
        envs = registry.setdefault("environments", {})
        existing = envs.get(env_name, {})
        merged = self._merge_record_fields(existing, patch)
        # Extending an old verified environment does not make newly declared
        # packages/capabilities verified. Keep evidence at its actual scope.
        for field, kind in (("packages", "package"), ("capabilities", "capability")):
            for item in self._ensure_list(patch.get(field)):
                if item not in self._ensure_list(existing.get(field)):
                    merged.setdefault("capability_evidence", {}).setdefault(f"{kind}:{item}",
                        {"status": "unknown", "source": "registration_declaration"})
        if patch.get("status") == "registered" and existing.get("status") in self.INSTALLED_STATUSES:
            merged["status"] = existing["status"]
        for field in remove_fields:
            if field in {"env", "prefix", "backend", "scope", "host", "execution_scope"}:
                raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path, f"cannot remove identity field {field}")
            merged.pop(field, None)
        merged.setdefault("env", env_name)
        merged.setdefault("status", "registered")
        merged.setdefault("registered_at", datetime.now().isoformat())
        if patch.get("status") in self.INSTALLED_STATUSES:
            if not any(merged.get(key) for key in ("prefix", "path", "env_runner", "runner", "runner_argv", "launcher")):
                raise RegistryError("REGISTRY_LAUNCH_INCOMPLETE", self.registry_path, env_name)
            if merged.get("backend") in {"prefix", "conda-prefix"} and not merged.get("prefix"):
                raise RegistryError("REGISTRY_LAUNCH_INCOMPLETE", self.registry_path, "prefix backend needs prefix")
            if merged.get("backend") == "runner" and not any(merged.get(key) for key in ("env_runner", "runner", "runner_argv", "launcher")):
                raise RegistryError("REGISTRY_LAUNCH_INCOMPLETE", self.registry_path, "runner backend needs launcher")
        if merged.get("prefix") and not Path(str(merged["prefix"])).is_absolute():
            raise RegistryError("REGISTRY_IDENTITY_INVALID", self.registry_path, "prefix must be absolute")
        envs[env_name] = merged

    def register_environment(
        self, env_name: str, purpose: str = None, packages=None, skill_id: str = None,
        notes: str = None, language: str = None, capabilities=None, healthcheck: str = None,
        status: str = None, tool_name: str = None, last_verified_at: str = None,
        healthchecks_by_skill=None, *, prefix=None, env_runner=None, backend=None, scope=None,
        versions=None, evidence=None, record=None, remove_fields=(),
    ):
        """Commit an explicit metadata patch; omitted fields retain their values.

        ``record`` carries additional launch/evidence fields without dropping
        fields owned by other producers. ``remove_fields`` is the only implicit-
        default-free field deletion operation. No software is run here.
        """
        patch = copy.deepcopy(record or {})
        fields = {"purpose": purpose, "packages": packages, "notes": notes, "language": language,
                  "capabilities": capabilities, "healthcheck": healthcheck, "status": status,
                  "last_verified_at": last_verified_at, "healthchecks_by_skill": healthchecks_by_skill,
                  "prefix": prefix, "env_runner": env_runner, "backend": backend, "scope": scope,
                  "versions": versions, "evidence": evidence}
        for key, value in fields.items():
            if value is not None:
                if key in patch and patch[key] != value:
                    raise RegistryError("REGISTRY_UPDATE_CONFLICT", self.registry_path, key)
                patch[key] = value
        if skill_id:
            patch["skill_ids"] = self._merge_unique(patch.get("skill_ids"), [skill_id])
            if healthcheck:
                patch.setdefault("healthchecks_by_skill", {})[skill_id] = healthcheck
        if tool_name:
            patch["bound_tools"] = self._merge_unique(patch.get("bound_tools"), [tool_name])
        if language is not None:
            patch["language"] = language.lower()
        self._update_registry(lambda latest: self._environment_patch(latest, env_name, patch, remove_fields))
        return True

    def merge_registry_data(self, data: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """Merge explicit records in one transaction, including unknown fields."""
        if not isinstance(data, dict):
            return {"tools": 0, "environments": 0}
        self._validate_registry(data)
        stats = {"tools": len(data.get("tools", {})), "environments": len(data.get("environments", {}))}
        def apply(latest):
            for name, record in data.get("environments", {}).items():
                self._environment_patch(latest, name, record)
            for name, record in data.get("tools", {}).items():
                merged = self._merge_record_fields(latest.setdefault("tools", {}).get(name, {}), record)
                merged.setdefault("name", name)
                latest["tools"][name] = merged
                env_name = merged.get("env") or merged.get("env_name")
                if env_name:
                    self._bind_records(latest, name, env_name)
            for key, value in data.items():
                if key not in {"tools", "environments"}:
                    latest[key] = self._merge_record_fields(latest.get(key, {}), value) if isinstance(value, dict) else copy.deepcopy(value)
        self._update_registry(apply)
        return stats

    def discover_runtime_candidates(self, requirements: Optional[Dict[str, Any]], *,
                                    target_env: str = "", limit: int = 8) -> List[Dict[str, Any]]:
        """Bounded metadata discovery, preserving real prefixes and ambiguity."""
        requirements = requirements or {}
        declared = str(requirements.get("env") or requirements.get("default_env") or "").strip()
        target = str(target_env or "").strip()
        if target and (not re.fullmatch(r"[A-Za-z0-9_.-]+", target) or target in {".", ".."}):
            raise ValueError("RUNTIME_CANDIDATE_TARGET_INVALID")
        environments = self.registry.get("environments") or {}
        known = self._known_environment_prefixes()
        names = list(dict.fromkeys([target] if target else [declared, *environments, *known]))
        wanted = set(self._ensure_list(requirements.get("tools")))
        packages = set(self._ensure_list(requirements.get("packages")))
        caps = set(self._ensure_list(requirements.get("capabilities")))
        rows = []
        for name in filter(None, names):
            record = dict(environments.get(name) or {})
            prefixes = self._registered_prefixes(name, record, known)
            for prefix_text in prefixes or [""]:
                prefix = Path(prefix_text) if prefix_text else None
                present = bool(prefix and prefix.is_dir())
                found = {tool: str(prefix / "bin" / tool) for tool in wanted
                         if prefix and re.fullmatch(r"[A-Za-z0-9_.+-]+", tool)
                         and (prefix / "bin" / tool).is_file() and os.access(prefix / "bin" / tool, os.X_OK)}
                matched_packages = packages & set(self._ensure_list(record.get("packages")))
                matched_caps = caps & set(self._ensure_list(record.get("capabilities")))
                if not target and name != declared and not (found or matched_packages or matched_caps):
                    continue
                reason = "RUNTIME_CANDIDATE_LOCATION_AMBIGUOUS" if len(prefixes) > 1 else "RUNTIME_CANDIDATE_UNVERIFIED" if present else "RUNTIME_CANDIDATE_PREFIX_MISSING"
                rows.append({"env": name, "prefix": prefix_text,
                    "provenance": ["software_registry" if record else "known_environment_inventory"],
                    "registered_status": str(record.get("status") or "unregistered"),
                    "prefix_present": present, "tool_candidates": found,
                    "missing_tool_candidates": sorted(wanted - set(found)),
                    "matched_packages": sorted(matched_packages), "matched_capabilities": sorted(matched_caps),
                    "availability": "UNKNOWN", "reason_code": reason,
                    "location_ambiguous": len(prefixes) > 1,
                    "is_declared": name == declared, "explicit_target": bool(target),
                    "score": len(found) * 20 + len(matched_packages) * 10 + len(matched_caps) * 5})
        rows.sort(key=lambda row: (not row["is_declared"], not row["prefix_present"], -row["score"], row["env"], row["prefix"]))
        return rows[:max(1, min(16, int(limit)))]

    def registered_runtime_reuse(self, requirements: Optional[Dict[str, Any]], *,
                                 skill_record: Optional[Dict[str, Any]] = None,
                                 target_env: str = "") -> Dict[str, Any]:
        """Select covered successful registration without creating a health receipt.

        Skill declarations are never registration evidence. ``skill_record`` must
        be the persisted record, obtained with get_runtime_registration().
        """
        from agent_core.registered_runtime import registered_runtime_reuse
        return registered_runtime_reuse(self, requirements or {},
            skill_record=skill_record or {}, target_env=target_env)

    def find_matching_environment(self, requirements: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        requirements = requirements or {}
        req_language = str(requirements.get("language", "") or "").lower()
        req_packages = set(self._ensure_list(requirements.get("packages")))
        req_caps = set(self._ensure_list(requirements.get("capabilities")))

        best_match = None
        best_score = -1
        for env_name, info in (self.registry.get("environments") or {}).items():
            if not self._environment_runtime_available(env_name, info):
                continue
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

    def verify_environment(self, env_name: str, language: str = "", healthcheck: str = "", *, persist: bool = True) -> tuple:
        info = self.registry.get("environments", {}).get(env_name)
        if not persist:
            info = dict(info or {})
            if not healthcheck:
                return False, "An explicit target healthcheck is required before registration."
        elif not info:
            return False, f"Environment '{env_name}' is not registered."

        selected_language = str(language or info.get("language", "")).lower()
        command = self.build_environment_healthcheck(
            env_name=env_name,
            language=selected_language,
            packages=info.get("packages"),
            healthcheck=healthcheck or info.get("healthcheck", ""),
        )
        # This legacy synchronous verifier cannot obtain a human decision.
        # Repository metadata is not permission to run an arbitrary shell.
        # Runtime callers must supply a matching claimed execution context.
        from agent_core.execution_permissions import require_dispatch, PermissionDenied
        try:
            require_dispatch(command)
        except PermissionDenied:
            return False, "HEALTHCHECK_PERMISSION_REQUIRED: use the controlled Runtime execution path."
        info["healthcheck"] = command
        run_command, process_env = self._direct_environment_healthcheck(env_name, command)
        if process_env is None:
            run_command = self._command_with_configured_manager(command)
        try:
            proc = subprocess.run(
                run_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=180,
                env=process_env,
            )
            ok = proc.returncode == 0
            info["status"] = "verified" if ok else "broken"
            info["last_verified_at"] = datetime.now().isoformat()
            info["last_healthcheck"] = run_command
            combined = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part).strip()
            if combined:
                info["notes"] = combined[:1000]
            if persist:
                self._save_registry()
            return ok, combined or ("OK" if ok else "Healthcheck failed.")
        except Exception as e:
            info["status"] = "broken"
            info["last_verified_at"] = datetime.now().isoformat()
            info["notes"] = str(e)
            if persist:
                self._save_registry()
            return False, str(e)

    def _bind_records(self, registry, tool_name, env_name):
        tools = registry.setdefault("tools", {})
        existing = tools.get(tool_name, {})
        old_env = existing.get("env") or existing.get("env_name")
        if old_env and old_env != env_name:
            raise RegistryError("REGISTRY_IDENTITY_CONFLICT", self.registry_path, f"tools.{tool_name}.env")
        tool_record = copy.deepcopy(existing)
        tool_record.update(name=tool_name, env=env_name, env_name=env_name)
        tools[tool_name] = tool_record
        self._environment_patch(registry, env_name, {"bound_tools": [tool_name]})

    def bind_tool_to_environment(self, tool_name: str, env_name: str):
        self._update_registry(lambda latest: self._bind_records(latest, tool_name, env_name))
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
        """Legacy automatic installer is disabled; use Installer human approval."""
        return False, "LEGACY_INSTALL_DISABLED: use Installer with explicit human approval."

    # =========================================================
    # [核心修改] 注册现有工具 (支持 setup_script 和 路径优化)
    # =========================================================
    def register_existing_tool(self, tool_name: str, env_name: str, source_path: str = None,
                               setup_script: str = None, *, prefix=None, evidence=None,
                               record=None, generate_shim=True):
        """Register existing software; shim failures cannot discard exact launch facts.

        A setup-dependent launch requires its shim before metadata is committed.
        Plain prefix/absolute-entry launch may be committed with a separately
        reported derived-file error. Existing shared shims are never overwritten.
        """
        original_tool_name = str(tool_name or "").strip()
        if self._is_shim_like_path(original_tool_name):
            raise ValueError(f"Refusing to register shim path as tool command: {original_tool_name}. Use the real command name.")
        tool_name = original_tool_name.replace("\\", "/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", tool_name) or tool_name in {".", ".."}:
            raise RegistryError("REGISTRY_IDENTITY_INVALID", self.registry_path, "tool name")
        if self._is_shim_like_path(source_path):
            raise ValueError(f"Refusing to register tool '{tool_name}' from shim path: {source_path}.")
        source_executable = None
        if source_path and os.path.isfile(source_path):
            source_executable = os.path.abspath(source_path)
            source_path = os.path.dirname(source_executable)
        env_name = self._normalize_tool_registration_env(tool_name, env_name, source_path)
        patch = copy.deepcopy(record or {})
        patch.update(name=tool_name, env=env_name, env_name=env_name)
        if source_path is not None:
            patch["source_path"] = str(source_path)
        if source_executable:
            patch["source_executable"] = source_executable
        if evidence is not None:
            patch["evidence"] = evidence
        if setup_script is not None:
            patch["custom_setup"] = bool(setup_script)
        final_setup = ""
        if source_path and self.infer_conda_env_from_path(source_path) != env_name:
            abs_path = os.path.abspath(source_path)
            final_setup += f'export PATH={shlex.quote(abs_path + ":" + abs_path + "/bin")}:"$PATH"\n'
            final_setup += f'export PYTHONPATH={shlex.quote(abs_path)}:"${{PYTHONPATH:-}}"\n'
        if setup_script:
            final_setup += "\n# --- Custom Setup from Agent ---\n" + setup_script + "\n"
        derived_errors, derived_paths = [], []
        exact_prefix = prefix or self.get_environment(env_name).get("prefix")
        direct_launch = bool(exact_prefix or source_executable) and not setup_script
        if not generate_shim and not direct_launch:
            raise RegistryError("REGISTRY_LAUNCH_INCOMPLETE", self.registry_path, "shim-free tool needs prefix or absolute entry")
        if generate_shim:
            try:
                shim_path = self._create_shim(env_name, tool_name, extra_setup=final_setup,
                                              source_path=source_path, source_executable=source_executable)
                patch["shim_path"] = str(shim_path)
                derived_paths.append(str(shim_path))
            except Exception as exc:
                if not direct_launch:
                    raise
                derived_errors.append(str(exc))
        def apply(latest):
            previous = latest.setdefault("tools", {}).get(tool_name, {})
            merged = self._merge_record_fields(previous, patch)
            merged.setdefault("status", "registered")
            merged.setdefault("install_time", datetime.now().isoformat())
            merged.setdefault("category", "Manual Registered")
            latest["tools"][tool_name] = merged
            environment_patch = {"capabilities": [f"tool:{tool_name}"], "bound_tools": [tool_name]}
            if prefix is not None:
                environment_patch["prefix"] = prefix
            self._environment_patch(latest, env_name, environment_patch)
            self._bind_records(latest, tool_name, env_name)
        try:
            self._update_registry(apply)
        except Exception:
            self.last_registry_receipt = {"committed": False, "registry_path": str(self.registry_path),
                "derived_paths": derived_paths, "derived_errors": derived_errors,
                "unreferenced_derived_paths_possible": bool(derived_paths)}
            raise
        self.last_registry_receipt.update(derived_paths=derived_paths, derived_errors=derived_errors)
        return True

    def load_from_yaml(self, config_path):
        """加载 YAML 配置"""
        config_file = Path(config_path)
        if not config_file.exists(): return
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if not data or 'tools' not in data: return
            
            display_print(f"⚙️  Loading config: {config_file}")
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
            if count > 0: display_print(f"   Loaded {count} tools.")
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
        self.shim_dir.mkdir(parents=True, exist_ok=True)
        # Never replace a shared old shim. A changed derivation gets a stable,
        # content-addressed path; the registry records the actual selected file.
        payload = script.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:16]
        candidates = [shim_path, self.shim_dir / f".{tool_name}.{digest}.shim"]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.shim_dir, prefix=f".{tool_name}.", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o755)
            for candidate in candidates:
                try:
                    os.link(temporary, candidate)
                    return candidate
                except FileExistsError:
                    if candidate.read_bytes() == payload:
                        return candidate
            raise RegistryError("REGISTRY_SHIM_CONFLICT", self.registry_path, tool_name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
