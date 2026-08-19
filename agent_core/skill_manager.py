import json
import os
import re
import shlex
import shutil
import subprocess
import yaml
from copy import deepcopy
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

class SkillManager:
    """遵循 Agent Skills 标准的技能加载器"""
    MAX_ACTIVE_SKILLS = 3
    MAX_SELECTED_SKILLS = 12
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
    
    def __init__(self, skills_root: str):
        self.skills_root = Path(skills_root)
        self.runtime_registry_path = self.skills_root / ".skill_runtime_registry.json"
        self.discovered_skills: List[Dict[str, str]] =[]
        self._skill_lookup: Dict[str, Dict[str, str]] = {}
        self._skill_lookup_lower: Dict[str, str] = {}
        self._ambiguous_skill_keys = set()
        self._discover_skills()
        self.runtime_registry = self._load_runtime_registry()

    def _discover_skills(self):
        """1. 发现 (Discover)：仅扫描目录，轻量级加载所有技能的 YAML Frontmatter"""
        if not self.skills_root.is_dir():
            return

        for skill_md in sorted(self.skills_root.rglob("SKILL.md")):
            skill_dir = skill_md.parent
            try:
                rel_dir = skill_dir.relative_to(self.skills_root)
            except ValueError:
                continue

            if any(part.startswith(".") for part in rel_dir.parts):
                continue

            try:
                raw_content = skill_md.read_text(encoding="utf-8")
                # 使用正则提取 YAML 头部
                match = re.match(r'^---\s*\n(.*?)\n---\s*\n', raw_content, re.DOTALL)
                if match:
                    yaml_content = match.group(1)
                    meta = yaml.safe_load(yaml_content) or {}
                    if isinstance(meta.get("metadata"), dict):
                        for key in (
                            "version", "tags", "legacy_ids", "runtime",
                            "renderer",
                            "generate",
                            "task_types", "stage_tags", "inputs", "outputs",
                            "when_to_use", "when_not_to_use", "skill_type",
                        ):
                            if key not in meta and key in meta["metadata"]:
                                meta[key] = meta["metadata"][key]
                    if "name" in meta:
                        skill_id = self._normalize_skill_key(rel_dir)
                        legacy_ids = self._build_legacy_skill_ids(meta, rel_dir)
                        # 记录技能所在的相对目录（用作唯一标识）
                        meta["_skill_id"] = skill_id
                        meta["_skill_dir"] = str(skill_dir.resolve())
                        meta["_skill_md"] = str(skill_md.resolve())
                        meta["_skill_leaf"] = rel_dir.parts[-1]
                        meta["_skill_category"] = "/".join(rel_dir.parts[:-1]) or "root"
                        meta["_skill_path_tokens"] = list(rel_dir.parts)
                        meta["_legacy_skill_ids"] = legacy_ids
                        meta["_scripts"] = self._list_relative_files(skill_dir / "scripts")
                        meta["_references"] = self._list_relative_files(skill_dir / "references")
                        self.discovered_skills.append(meta)
            except Exception as e:
                print(f"Failed to discover skill in {skill_dir}: {e}")
        self._rebuild_skill_lookup()

    def _normalize_skill_key(self, value: Any) -> str:
        return str(value or "").replace("\\", "/").strip().strip("/")

    def _build_legacy_skill_ids(self, meta: Dict[str, Any], rel_dir: Path) -> List[str]:
        aliases = []
        aliases.extend(self._ensure_list(meta.get("legacy_ids")))
        if len(rel_dir.parts) > 1:
            aliases.append(rel_dir.parts[-1])
        normalized = []
        for alias in aliases:
            key = self._normalize_skill_key(alias)
            if key and key not in normalized and key != self._normalize_skill_key(rel_dir):
                normalized.append(key)
        return normalized

    def _rebuild_skill_lookup(self):
        self._skill_lookup = {}
        self._skill_lookup_lower = {}
        self._ambiguous_skill_keys = set()
        for skill in self.discovered_skills:
            for key in self._skill_identifier_candidates(skill):
                norm = self._normalize_skill_key(key)
                if not norm:
                    continue
                existing = self._skill_lookup.get(norm)
                if existing and existing.get("_skill_id") != skill.get("_skill_id"):
                    self._skill_lookup[norm] = {}
                    self._ambiguous_skill_keys.add(norm)
                elif norm not in self._skill_lookup:
                    self._skill_lookup[norm] = skill

                lower = norm.lower()
                existing_lower = self._skill_lookup_lower.get(lower)
                if existing_lower and existing_lower != skill.get("_skill_id"):
                    self._skill_lookup_lower.pop(lower, None)
                    self._ambiguous_skill_keys.add(lower)
                elif lower not in self._ambiguous_skill_keys:
                    self._skill_lookup_lower[lower] = skill.get("_skill_id", "")

    def _resolve_skill_id(self, skill_id: str) -> str:
        key = self._normalize_skill_key(skill_id)
        if not key:
            return ""
        direct = self._skill_lookup.get(key)
        if direct:
            return direct.get("_skill_id", "")
        return self._skill_lookup_lower.get(key.lower(), "")

    def _skill_identifier_candidates(self, skill: Dict[str, Any]) -> List[str]:
        return list(dict.fromkeys([
            skill.get("_skill_id", ""),
            skill.get("_skill_leaf", ""),
            *self._ensure_list(skill.get("_legacy_skill_ids")),
        ]))

    def _list_relative_files(self, folder: Path) -> List[str]:
        """List files below a skill subfolder, relative to the skill directory."""
        if not folder.is_dir():
            return []
        skill_dir = folder.parent
        return [
            str(path.relative_to(skill_dir)).replace("\\", "/")
            for path in sorted(folder.rglob("*"))
            if path.is_file()
            and "__pycache__" not in path.parts
            and not any(part.startswith(".") for part in path.relative_to(skill_dir).parts)
            and path.suffix.lower() not in {".pyc", ".pyo"}
        ]

    def _skill_tree_structure(self) -> Dict[str, Any]:
        tree: Dict[str, Any] = {}
        for skill in self.discovered_skills:
            node = tree
            for part in skill.get("_skill_path_tokens", []):
                node = node.setdefault(part, {})
        return tree

    def _render_tree_lines(self, node: Dict[str, Any], prefix: str = "") -> List[str]:
        lines: List[str] = []
        entries = sorted(node.items())
        for index, (name, child) in enumerate(entries):
            is_last = index == len(entries) - 1
            connector = "`-- " if is_last else "|-- "
            lines.append(f"{prefix}{connector}{name}")
            next_prefix = prefix + ("    " if is_last else "|   ")
            lines.extend(self._render_tree_lines(child, next_prefix))
        return lines

    def _catalog_bool(self, value: bool) -> str:
        return "yes" if value else "no"

    def _catalog_entry_lines(self, skill: Dict[str, Any]) -> List[str]:
        tags = ", ".join(map(str, skill.get("tags", []) or [])) or "none"
        task_types = ", ".join(self.metadata_list(skill, "task_types")) or "none"
        stage_tags = ", ".join(self.metadata_list(skill, "stage_tags")) or "none"
        inputs = ", ".join(self.metadata_list(skill, "inputs")) or "none"
        outputs = ", ".join(self.metadata_list(skill, "outputs")) or "none"
        when_to_use = "; ".join(self.metadata_list(skill, "when_to_use")) or "none"
        when_not_to_use = "; ".join(self.metadata_list(skill, "when_not_to_use")) or "none"
        skill_id = skill.get("_skill_id", "")
        return [
            f"- canonical_skill_id: {skill_id}",
            f"  skill_id: {skill_id}",
            f"  name: {skill.get('name', 'Unknown')}",
            f"  category: {skill.get('_skill_category', 'root')}",
            f"  tags: {tags}",
            f"  task_types: {task_types}",
            f"  stage_tags: {stage_tags}",
            f"  inputs: {inputs}",
            f"  outputs: {outputs}",
            f"  when_to_use: {when_to_use}",
            f"  when_not_to_use: {when_not_to_use}",
            f"  has_scripts: {self._catalog_bool(bool(skill.get('_scripts')))}",
            f"  has_references: {self._catalog_bool(bool(skill.get('_references')))}",
            f"  description: {skill.get('description', 'No description')}",
        ]

    def _load_runtime_registry(self) -> Dict:
        if not self.runtime_registry_path.exists():
            return {"skills": {}}
        try:
            data = json.loads(self.runtime_registry_path.read_text(encoding="utf-8"))
            return self._normalize_runtime_registry(data)
        except Exception:
            return {"skills": {}}

    def _save_runtime_registry(self):
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self.runtime_registry_path.write_text(
            json.dumps(self.runtime_registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_runtime_record(self, skill_id: str) -> Dict:
        """Return declared runtime metadata plus any persisted registration evidence.

        A skill document is the source of the runtime contract; the registry only
        records whether that contract has been installed or verified.  Returning
        an explicit missing record keeps callers from treating absent metadata as
        an exception or silently guessing an environment.
        """
        meta = self.get_skill_meta(skill_id)
        candidates = [skill_id]
        if meta:
            candidates = self._skill_identifier_candidates(meta)
        persisted: Dict[str, Any] = {}
        for candidate in candidates:
            record = self.runtime_registry.get("skills", {}).get(candidate, {})
            if record:
                persisted = dict(record)
                break

        runtime = dict((meta or {}).get("runtime") or {})
        canonical_id = str((meta or {}).get("_skill_id") or skill_id)
        if not runtime and not persisted:
            return {
                "skill_id": canonical_id,
                "status": "unknown",
                "metadata_present": False,
                "metadata_status": "missing",
            }

        declared_env = str(runtime.get("default_env") or "").strip()
        declared = {
            "default_env": declared_env,
            "env_runner": str(runtime.get("env_runner") or ""),
            "language": self._runtime_language(runtime),
            "tools": self._ensure_list(runtime.get("tools")),
            "packages": self._runtime_packages(runtime),
            "capabilities": self._runtime_capabilities(runtime, meta or {}),
            "install_tool": str(runtime.get("install_tool") or ""),
            "install_commands": self._runtime_command_list(runtime.get("install_command"))
            + self._runtime_command_list(runtime.get("install_commands")),
            "install_fallbacks": self._runtime_command_list(runtime.get("install_fallbacks")),
            "install_policy": str(runtime.get("install_policy") or ""),
            "healthcheck": self.build_runtime_healthcheck(declared_env, runtime, meta or {}),
            "env_file": self._runtime_asset_path(runtime, meta or {}, "env_file"),
        }
        merged = dict(persisted)
        merged.setdefault("skill_id", canonical_id)
        merged.setdefault("status", "declared" if runtime else "unknown")
        merged["metadata_present"] = bool(runtime)
        merged["metadata_status"] = "present" if runtime else "missing"
        merged["declared_runtime"] = declared if runtime else {}
        for key, value in declared.items():
            if value not in ("", [], {}, None):
                merged.setdefault(key, value)
        return merged

    def get_renderer_record(self, skill_id: str) -> Dict[str, Any]:
        """Return the declared deterministic renderer, if this skill owns one."""
        canonical_id = self._resolve_skill_id(skill_id) or self._normalize_skill_key(skill_id)
        meta = self.get_skill_meta(canonical_id) or {}
        renderer = meta.get("renderer")
        if not isinstance(renderer, dict) and isinstance(meta.get("metadata"), dict):
            renderer = meta["metadata"].get("renderer")
        if not isinstance(renderer, dict) or not renderer:
            return {
                "skill_id": canonical_id or str(skill_id or ""),
                "metadata_present": False,
                "status": "missing",
            }
        entrypoint = str(renderer.get("entrypoint") or renderer.get("script") or "").strip()
        resolved = ""
        if entrypoint:
            try:
                resolved = str(self._resolve_listed_skill_file(meta, entrypoint, "_scripts"))
            except (FileNotFoundError, ValueError):
                resolved = ""
        status = "declared" if resolved else "invalid"
        renderer_id = str(renderer.get("renderer_id") or renderer.get("id") or canonical_id)
        aliases = [
            renderer_id,
            canonical_id,
            entrypoint,
            Path(entrypoint).name if entrypoint else "",
            Path(entrypoint).stem if entrypoint else "",
            resolved,
            Path(resolved).name if resolved else "",
            Path(resolved).stem if resolved else "",
            *self._ensure_list(renderer.get("aliases")),
        ]
        raw_output_bindings = renderer.get("output_bindings") or {}
        if isinstance(raw_output_bindings, list):
            output_bindings = {
                str(item.get("logical_output") or item.get("output_id") or item.get("role") or "").strip(): dict(item)
                for item in raw_output_bindings
                if isinstance(item, dict)
                and str(item.get("logical_output") or item.get("output_id") or item.get("role") or "").strip()
            }
        else:
            output_bindings = {
                str(key): dict(value) if isinstance(value, dict) else {"mode": str(value)}
                for key, value in dict(raw_output_bindings or {}).items()
                if str(key).strip()
            }
        return {
            **renderer,
            "skill_id": canonical_id,
            "renderer_id": renderer_id,
            "renderer_aliases": list(dict.fromkeys(str(item).strip() for item in aliases if str(item).strip())),
            "invocation": dict(renderer.get("invocation") or {}),
            "owned_outputs": self._ensure_list(renderer.get("owned_outputs")),
            "output_bindings": output_bindings,
            "entrypoint": entrypoint,
            "resolved_entrypoint": resolved,
            "runner": str(renderer.get("runner") or "controller_python"),
            "manifest_filename": str(renderer.get("manifest_filename") or "manifest.json"),
            "manifest_type": str(renderer.get("manifest_type") or "renderer_artifact_contract"),
            "schema_version": int(renderer.get("schema_version") or 3),
            "composite_step": bool(renderer.get("composite_step", True)),
            "metadata_present": True,
            "status": status,
        }

    def has_renderer(self, skill_id: str) -> bool:
        return self.get_renderer_record(skill_id).get("status") == "declared"

    def _normalize_runtime_registry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        registry = {"skills": {}}
        if not isinstance(data, dict):
            return registry
        for skill_id, info in (data.get("skills") or {}).items():
            record = dict(info or {})
            record["packages"] = self._ensure_list(record.get("packages"))
            record["capabilities"] = self._ensure_list(record.get("capabilities"))
            record["language"] = str(record.get("language", "") or "").lower()
            record.setdefault("status", "installed")
            record.setdefault("env_runner", "")
            record.setdefault("healthcheck", "")
            record.setdefault("install_policy", "")
            record["install_commands"] = self._runtime_command_list(record.get("install_commands"))
            record["install_fallbacks"] = self._runtime_command_list(record.get("install_fallbacks"))
            record.setdefault("env_file", "")
            registry["skills"][skill_id] = record
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

    def _status_rank(self, status: str) -> int:
        return self.STATUS_RANK.get(str(status or "").lower(), 0)

    def _best_status(self, existing: str, incoming: str) -> str:
        existing = str(existing or "").lower()
        incoming = str(incoming or "").lower()
        return incoming if self._status_rank(incoming) > self._status_rank(existing) else (existing or incoming)

    def _merge_unique(self, existing, extra) -> List[str]:
        merged = []
        for item in self._ensure_list(existing) + self._ensure_list(extra):
            if item not in merged:
                merged.append(item)
        return merged

    def metadata_list(self, skill: Dict[str, Any], key: str) -> List[str]:
        """Return normalized list metadata from top-level or nested metadata."""
        if not isinstance(skill, dict):
            return []
        value = skill.get(key)
        if value is None and isinstance(skill.get("metadata"), dict):
            value = skill["metadata"].get(key)
        return self._ensure_list(value)

    def _runtime_packages(self, runtime: Dict[str, Any]) -> List[str]:
        return self._ensure_list(runtime.get("packages")) or self._ensure_list(runtime.get("python_packages"))

    def _runtime_tools(self, runtime: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
        tools = self._ensure_list(runtime.get("tools")) or self._ensure_list(record.get("tools"))
        return list(dict.fromkeys(str(tool).strip() for tool in tools if str(tool).strip()))

    def _runtime_capabilities(self, runtime: Dict[str, Any], meta: Dict[str, Any]) -> List[str]:
        caps = self._ensure_list(runtime.get("capabilities"))
        if caps:
            return caps
        return self._ensure_list(meta.get("tags"))

    def _runtime_language(self, runtime: Dict[str, Any]) -> str:
        language = str(runtime.get("language", "") or "").lower().strip()
        if language:
            return language
        if self._runtime_packages(runtime):
            return "python"
        return ""

    def _runtime_command_list(self, value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple) or isinstance(value, set):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    def _runtime_asset_path(self, runtime: Dict[str, Any], meta: Dict[str, Any], key: str) -> str:
        raw = str(runtime.get(key, "") or "").strip()
        if not raw:
            return ""
        expanded = raw.replace("{env}", str(runtime.get("default_env", "") or "").strip())
        path = Path(expanded)
        if not path.is_absolute():
            skill_dir = Path(meta.get("_skill_dir", ""))
            if skill_dir:
                path = skill_dir / path
        return str(path.resolve()).replace("\\", "/")

    def _runtime_install_commands(self, runtime: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
        commands: List[str] = []
        commands.extend(self._runtime_command_list(runtime.get("install_command")))
        commands.extend(self._runtime_command_list(runtime.get("install_commands")))
        commands.extend(self._runtime_command_list(record.get("install_command")))
        commands.extend(self._runtime_command_list(record.get("install_commands")))
        unique: List[str] = []
        for command in commands:
            if command not in unique:
                unique.append(command)
        return unique

    def _is_environment_bootstrap_skill(self, skill_id: str, runtime: Dict[str, Any], meta: Dict[str, Any]) -> bool:
        haystack = " ".join([
            skill_id,
            str(meta.get("name", "")),
            str(meta.get("description", "")),
            " ".join(self._ensure_list(runtime.get("capabilities"))),
            " ".join(self._ensure_list(meta.get("tags"))),
        ]).lower()
        markers = [
            "bootstrap",
            "environment",
            "runtime",
            "env",
            "setup",
            "install",
        ]
        return any(marker in haystack for marker in markers)

    @staticmethod
    def _healthcheck_already_enters_conda_env(command: str) -> bool:
        return bool(re.search(r"\b(?:conda|mamba)\s+run\s+-n\s+\S+", str(command or "")))

    @staticmethod
    def _healthcheck_needs_shell(command: str) -> bool:
        text = str(command or "")
        return bool(re.search(r"(\&\&|\|\||\||;|>|<|\$\(|`)", text))

    def _wrap_runtime_healthcheck(self, env: str, command: str) -> str:
        """Run declared healthchecks inside the skill's default conda env.

        Many bash-oriented skills naturally declare checks such as
        `bash -lc "tool --version"` or `samtools --version && ...`. Those are
        only valid after the runtime env is active. Keep explicit `conda run`
        healthchecks unchanged, but wrap bare checks generically.
        """
        command = str(command or "").strip()
        env = str(env or "").strip()
        if not command or not env or self._healthcheck_already_enters_conda_env(command):
            return command
        bash_login = re.match(r"^bash\s+-lc\s+(.+)$", command, re.DOTALL)
        if bash_login:
            # `conda run ... bash -lc` can reset PATH on some HPC login shells and
            # hide the conda env binaries. Keep the shell semantics, drop `-l`.
            return f"conda run -n {shlex.quote(env)} bash -c {bash_login.group(1).strip()}"
        if re.match(r"^(?:bash|sh|python|python\d+(?:\.\d+)?|Rscript|R)\b", command):
            return f"conda run -n {shlex.quote(env)} {command}"
        if self._healthcheck_needs_shell(command):
            return f"conda run -n {shlex.quote(env)} bash -lc {shlex.quote(command)}"
        return f"conda run -n {shlex.quote(env)} {command}"

    def build_runtime_healthcheck(self, env: str, runtime: Dict[str, Any], meta: Dict[str, Any] = None) -> str:
        if not env:
            return ""
        runtime = runtime or {}
        meta = meta or {}
        healthcheck = str(runtime.get("healthcheck", "") or "")
        if healthcheck:
            normalized = self._normalize_runtime_healthcheck_paths(
                healthcheck.replace("{env}", env),
                meta,
            )
            return self._wrap_runtime_healthcheck(env, normalized)
        packages = self._runtime_packages(runtime)
        language = self._runtime_language(runtime)
        if language == "r":
            if packages:
                quoted = ",".join(f"'{pkg}'" for pkg in packages)
                return (
                    f'conda run -n {env} Rscript -e "pkgs <- c({quoted}); '
                    "invisible(lapply(pkgs, function(p) suppressPackageStartupMessages(library(p, character.only=TRUE)))); "
                    'cat(\'OK\\n\')"'
                )
            return f'conda run -n {env} Rscript -e "cat(\'OK\\n\')"'
        if packages:
            imports = []
            for pkg in packages:
                token = re.split(r"[<>=!~]", str(pkg))[0].strip()
                if token:
                    normalized = token.replace("-", "_")
                    imports.append(self.PYTHON_IMPORT_ALIASES.get(token.lower(), self.PYTHON_IMPORT_ALIASES.get(normalized.lower(), normalized)))
            imports = list(dict.fromkeys(imports))
            if imports:
                imports_str = ", ".join(imports)
                return f'conda run -n {env} python -c "import {imports_str}; print(\'OK\')"'
        return f'conda run -n {env} python -c "print(\'OK\')"'

    def _normalize_runtime_healthcheck_paths(self, command: str, meta: Dict[str, Any]) -> str:
        """Make skill-script healthchecks independent of the task working directory."""
        if not command or not meta:
            return command

        skill_dir_raw = meta.get("_skill_dir") or ""
        if not skill_dir_raw:
            return command

        skill_dir = Path(skill_dir_raw).resolve()
        skills_root = self.skills_root.resolve()
        normalized = (
            command
            .replace("{skill_dir}", str(skill_dir).replace("\\", "/"))
            .replace("{skills_root}", str(skills_root).replace("\\", "/"))
        )

        try:
            skill_rel = skill_dir.relative_to(skills_root).as_posix()
        except ValueError:
            skill_rel = ""

        scripts = meta.get("_scripts", []) or []
        for script in scripts:
            rel_script = str(script).replace("\\", "/").lstrip("./")
            if not rel_script:
                continue
            abs_script = str((skill_dir / rel_script).resolve()).replace("\\", "/")
            candidates = [rel_script]
            if skill_rel:
                candidates.extend([
                    f"skills/{skill_rel}/{rel_script}",
                    f"./skills/{skill_rel}/{rel_script}",
                ])
            for candidate in sorted(set(candidates), key=len, reverse=True):
                pattern = rf"(?<![\w./:\\-]){re.escape(candidate)}(?![\w./:\\-])"
                normalized = re.sub(pattern, abs_script, normalized)
        return normalized

    def get_runtime_requirements(self, skill_id: str) -> Dict[str, Any]:
        meta = self.get_skill_meta(skill_id)
        runtime = meta.get("runtime") or {}
        canonical_id = meta.get("_skill_id", skill_id)
        record = self.get_runtime_record(canonical_id)
        default_env = str(runtime.get("default_env", "") or "").strip()
        env = default_env or self.get_runtime_env(canonical_id)
        runtime_packages = self._runtime_packages(runtime)
        packages = runtime_packages or self._ensure_list(record.get("packages"))
        language = self._runtime_language(runtime)
        if not language:
            language = str(record.get("language", "") or "").lower()
        runtime_capabilities = self._runtime_capabilities(runtime, meta)
        capabilities = runtime_capabilities or self._ensure_list(record.get("capabilities"))
        env_runner = str(runtime.get("env_runner", "") or record.get("env_runner") or "")
        runtime_healthcheck = self.build_runtime_healthcheck(env or runtime.get("default_env", ""), runtime, meta)
        healthcheck = runtime_healthcheck or record.get("healthcheck", "")
        install_commands = self._runtime_install_commands(runtime, record)
        install_command = install_commands[0] if install_commands else ""
        install_policy = str(
            runtime.get("install_policy")
            or record.get("install_policy")
            or ("prefer-declared" if install_commands or runtime.get("env_file") else "")
        ).strip()
        install_fallbacks = self._runtime_command_list(runtime.get("install_fallbacks")) or self._runtime_command_list(record.get("install_fallbacks"))
        return {
            "skill_id": canonical_id,
            "default_env": runtime.get("default_env", ""),
            "env": env,
            "env_runner": env_runner,
            "language": language,
            "tools": self._runtime_tools(runtime, record),
            "packages": packages,
            "capabilities": capabilities,
            "install_tool": record.get("install_tool") or runtime.get("install_tool", ""),
            "install_command": install_command,
            "install_commands": install_commands,
            "install_fallbacks": install_fallbacks,
            "install_policy": install_policy,
            "env_file": self._runtime_asset_path(runtime, meta, "env_file"),
            "healthcheck": healthcheck,
            "mode_healthchecks": runtime.get("mode_healthchecks", {}) or record.get("mode_healthchecks", {}) or {},
            "metadata_present": bool(runtime or record.get("metadata_present")),
            "metadata_status": "present" if runtime or record.get("metadata_present") else "missing",
        }

    def controlled_rg_hints(
        self,
        text: str,
        search_roots: Optional[List[str]] = None,
        max_results: int = 24,
        timeout_seconds: float = 3.0,
    ) -> Dict[str, Any]:
        """Return bounded discovery hints; never treat text matches as runtime readiness."""

        result: Dict[str, Any] = {
            "status": "no_hints",
            "candidate_skills": [],
            "candidate_envs": [],
            "candidate_tools": [],
            "candidate_paths": [],
            "evidence_files": [],
        }
        rg = shutil.which("rg")
        if not rg:
            result["status"] = "rg_unavailable"
            return result

        raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}|[\u4e00-\u9fff]{2,8}", str(text or ""))
        ignored = {
            "analysis", "benchmark", "biomind", "data", "file", "files", "input", "output",
            "skill", "skills", "task", "test", "using", "with", "分析", "文件", "输入", "输出",
        }
        terms: List[str] = []
        for term in raw_terms:
            normalized = term.strip()
            if normalized.lower() in ignored or normalized in terms:
                continue
            terms.append(normalized[:80])
            if len(terms) >= 8:
                break
        if not terms:
            result["status"] = "no_search_terms"
            return result

        roots: List[str] = []
        for raw_root in [str(self.skills_root), *(search_roots or [])]:
            path = Path(str(raw_root or "")).expanduser()
            if path.exists():
                resolved = str(path.resolve())
                if resolved not in roots:
                    roots.append(resolved)
        if not roots:
            result["status"] = "no_search_roots"
            return result

        command = [
            rg, "-n", "-i", "-F", "--max-filesize", "1M", "--max-count", "2",
            "--glob", "SKILL.md", "--glob", "*.json", "--glob", "*.yaml", "--glob", "*.yml",
            "--glob", "*.md", "--glob", "*.sh", "--glob", "*.py", "--glob", "*.txt",
            "--glob", "!**/expected_answers/**", "--glob", "!**/answer_key/**",
            "--glob", "!**/hidden/**", "--glob", "!**/solutions/**", "--glob", "!**/tests/**",
        ]
        for term in terms:
            command.extend(["-e", term])
        command.extend(["--", *roots])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(0.1, float(timeout_seconds)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["status"] = "rg_failed"
            result["reason"] = type(exc).__name__
            return result

        lines = [line for line in completed.stdout.splitlines() if line.strip()][:max_results]
        for line in lines:
            path_text, _sep, evidence = line.partition(":")
            path = Path(path_text)
            if not path.is_file():
                continue
            path_str = str(path.resolve())
            if path_str not in result["evidence_files"]:
                result["evidence_files"].append(path_str)
            if path_str not in result["candidate_paths"]:
                result["candidate_paths"].append(path_str)
            try:
                skill_id = path.parent.relative_to(self.skills_root.resolve()).as_posix()
            except ValueError:
                skill_id = ""
            if path.name == "SKILL.md" and self.get_skill_meta(skill_id):
                if skill_id not in result["candidate_skills"]:
                    result["candidate_skills"].append(skill_id)
            haystack = f"{path_str} {evidence}"
            for env in re.findall(r"\b(?:bio|env)_[A-Za-z0-9_.-]+\b", haystack):
                if env not in result["candidate_envs"]:
                    result["candidate_envs"].append(env)
            for tool in re.findall(r"\b(?:command\s+-v|which)\s+([A-Za-z][A-Za-z0-9_.+-]*)", evidence):
                if tool not in result["candidate_tools"]:
                    result["candidate_tools"].append(tool)

        result["status"] = "hints_found" if lines else "no_hints"
        result["candidate_skills"] = result["candidate_skills"][: self.MAX_SELECTED_SKILLS]
        for key in ("candidate_envs", "candidate_tools", "candidate_paths", "evidence_files"):
            result[key] = result[key][:max_results]
        return result

    def get_discovery_prompt(self) -> str:
        """生成供 Planner [选择] 使用的元数据列表 (低 Token 开销)"""
        if not self.discovered_skills:
            return "No external skills available."

        lines = ["<available_skills>"]
        categories: Dict[str, List[str]] = {}
        for skill in self.discovered_skills:
            categories.setdefault(skill.get("_skill_category", "root"), []).append(skill.get("_skill_id", ""))
        lines.append("Directory categories:")
        for category in sorted(categories):
            members = ", ".join(sorted(categories[category]))
            lines.append(f"- {category}: {members}")
        lines.append("Skill entries:")
        for skill in self.discovered_skills:
            name = skill.get("name", "Unknown")
            skill_id = skill.get("_skill_id")
            desc = skill.get("description", "No description")
            skill_dir = skill.get("_skill_dir", "")
            category = skill.get("_skill_category", "root")
            aliases = ", ".join(self._ensure_list(skill.get("_legacy_skill_ids"))) or "none"
            scripts = ", ".join(skill.get("_scripts", [])) or "none"
            refs = ", ".join(skill.get("_references", [])) or "none"
            task_types = ", ".join(self.metadata_list(skill, "task_types")) or "none"
            stage_tags = ", ".join(self.metadata_list(skill, "stage_tags")) or "none"
            inputs = ", ".join(self.metadata_list(skill, "inputs")) or "none"
            outputs = ", ".join(self.metadata_list(skill, "outputs")) or "none"
            runtime = skill.get("runtime", {}) or {}
            record = self.get_runtime_record(skill_id)
            runtime_text = ""
            if runtime:
                packages = ", ".join(self._runtime_packages(runtime)) or "none"
                capabilities = ", ".join(self._runtime_capabilities(runtime, skill)) or "none"
                runtime_text = (
                    f"\n  default_env: {runtime.get('default_env', 'unspecified')}"
                    f"\n  env_runner: {runtime.get('env_runner', 'unspecified')}"
                    f"\n  language: {self._runtime_language(runtime) or 'unspecified'}"
                    f"\n  packages: {packages}"
                    f"\n  capabilities: {capabilities}"
                    f"\n  install_policy: {runtime.get('install_policy', 'unspecified')}"
                )
                if runtime.get("env_file"):
                    runtime_text += f"\n  env_file: {runtime.get('env_file')}"
            if record:
                runtime_text += (
                    f"\n  remembered_env: {record.get('env', 'unknown')}"
                    f"\n  environment_status: {record.get('status', 'unknown')}"
                    f"\n  last_verified_at: {record.get('last_verified_at', 'unknown')}"
                )
            lines.append(
                f"- canonical_skill_id: {skill_id}\n"
                f"  name: {name}\n"
                f"  description: {desc}\n"
                f"  category: {category}\n"
                f"  aliases: {aliases}\n"
                f"  task_types: {task_types}\n"
                f"  stage_tags: {stage_tags}\n"
                f"  inputs: {inputs}\n"
                f"  outputs: {outputs}\n"
                f"  absolute_dir: {skill_dir}\n"
                f"  scripts: {scripts}\n"
                f"  references: {refs}"
                f"{runtime_text}"
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    def get_skill_tree_prompt(self, max_chars: Optional[int] = None) -> str:
        """Compact tree view for LLM-first skill selection."""
        if not self.discovered_skills:
            return "<skill_tree>\nNo external skills available.\n</skill_tree>"
        lines = ["<skill_tree>", "skills/"]
        lines.extend(self._render_tree_lines(self._skill_tree_structure()))
        lines.append("</skill_tree>")
        if max_chars and len("\n".join(lines)) > max_chars:
            limit = max(240, int(max_chars))
            closing = [
                "... additional canonical IDs are available through skill retrieval ...",
                "</skill_tree>",
            ]
            kept = [lines[0], lines[1]]
            for line in lines[2:-1]:
                candidate = "\n".join(kept + [line] + closing)
                if len(candidate) > limit:
                    break
                kept.append(line)
            lines = kept + closing
        return "\n".join(lines)

    def get_skill_catalog_prompt(self, skill_ids: Optional[List[str]] = None) -> str:
        """Shallow metadata catalog without expanding scripts or references."""
        if not self.discovered_skills:
            return "<skill_catalog>\nNo external skills available.\n</skill_catalog>"
        chosen: List[Dict[str, Any]]
        if skill_ids:
            chosen = [self.get_skill_meta(sid) for sid in skill_ids]
            chosen = [skill for skill in chosen if skill]
        else:
            chosen = list(self.discovered_skills)
        lines = [
            "<skill_catalog>",
            "Selection rule: copy only canonical_skill_id values into [SELECTED_SKILLS: ...]; do not use skill names, plugin names, descriptions, or natural language.",
        ]
        for skill in chosen:
            lines.extend(self._catalog_entry_lines(skill))
        lines.append("</skill_catalog>")
        return "\n".join(lines)

    def candidate_skill_ids_for_prompt(
        self,
        text: str,
        max_results: int = None,
    ) -> List[str]:
        """Return a bounded retrieval set without deciding final skill use.

        Explicit canonical/legacy IDs are sufficient evidence for this
        retrieval context, so do not pad them with unrelated metadata matches.
        Without explicit IDs, return bounded shallow metadata matches. The
        Consultant still decides which skills to select and may load any other
        ID visible in the compact skill tree.
        """
        limit = max_results or self.MAX_SELECTED_SKILLS
        path_like_ids = re.findall(
            r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)+)(?![\w./-])",
            str(text or ""),
        )
        explicit = self.normalize_selected_skills(path_like_ids, max_results=limit)
        if explicit:
            return explicit
        matched = self.match_skills(text, max_results=limit)
        ordered: List[str] = []
        for skill_id in explicit + matched:
            if skill_id not in ordered:
                ordered.append(skill_id)
            if len(ordered) >= limit:
                break
        return ordered

    def get_skill_reference_index(self, skill_id: str) -> List[str]:
        """Return only reference filenames; caller can load individual files later."""
        meta = self.get_skill_meta(skill_id)
        return list(meta.get("_references", []) or [])

    def get_skill_reference_index_prompt(self, skill_ids: List[str]) -> str:
        if not skill_ids:
            return ""
        lines = ["<skill_reference_index>"]
        for sid in self.normalize_selected_skills(skill_ids, max_results=self.MAX_SELECTED_SKILLS):
            refs = self.get_skill_reference_index(sid)
            refs_text = ", ".join(refs) if refs else "none"
            lines.append(f"- {sid}: {refs_text}")
        lines.append("</skill_reference_index>")
        return "\n".join(lines)

    def _resolve_listed_skill_file(self, meta: Dict[str, Any], requested_path: str, meta_key: str) -> Optional[Path]:
        """Resolve one listed script/reference by relative or absolute path."""
        if not meta:
            return None
        raw_path = str(requested_path or "").strip()
        if not raw_path:
            return None
        skill_dir = Path(meta.get("_skill_dir", ""))
        valid_rel_paths = set(meta.get(meta_key, []) or [])
        if not valid_rel_paths:
            return None

        normalized = raw_path.replace("\\", "/").strip().strip("/")
        if normalized in valid_rel_paths:
            candidate = skill_dir / normalized
            return candidate if candidate.is_file() else None

        try:
            requested_abs = Path(raw_path).resolve()
        except Exception:
            return None

        for rel_path in valid_rel_paths:
            candidate = (skill_dir / rel_path).resolve()
            if candidate == requested_abs and candidate.is_file():
                return candidate
        return None

    def load_reference(self, skill_id: str, relative_ref_path: str) -> str:
        """Load a single selected reference file on demand."""
        meta = self.get_skill_meta(skill_id)
        ref_path = self._resolve_listed_skill_file(meta, relative_ref_path, "_references")
        if not ref_path:
            return ""
        try:
            return ref_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ref_path.read_text(encoding="utf-8", errors="replace")

    def load_script(self, skill_id: str, relative_script_path: str) -> str:
        """Load a single selected script file on demand."""
        meta = self.get_skill_meta(skill_id)
        script_path = self._resolve_listed_skill_file(meta, relative_script_path, "_scripts")
        if not script_path:
            return ""
        try:
            return script_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return script_path.read_text(encoding="utf-8", errors="replace")

    def get_skill_dir(self, skill_id: str) -> str:
        """Return the absolute directory for a known skill id."""
        meta = self.get_skill_meta(skill_id)
        return meta.get("_skill_dir", "")

    def get_skill_meta(self, skill_id: str) -> Dict:
        resolved = self._resolve_skill_id(skill_id)
        return dict(self._skill_lookup.get(resolved) or {})

    def is_workflow_router_skill(self, skill_id: str) -> bool:
        """Return True for skills that only route/decompose work, not execute runtimes."""
        meta = self.get_skill_meta(skill_id)
        if not meta:
            return False
        skill_type = str(meta.get("skill_type") or "").strip().lower()
        tags = {item.lower() for item in self.metadata_list(meta, "tags")}
        stage_tags = {item.lower() for item in self.metadata_list(meta, "stage_tags")}
        outputs = {item.lower() for item in self.metadata_list(meta, "outputs")}
        runtime = meta.get("runtime") or {}
        if skill_type in {"workflow_router", "workflow-router", "router"}:
            return True
        if "workflow-router" in tags or "workflow_router" in tags:
            return True
        routing_tags = {"workflow_routing", "input_triage"}
        routing_outputs = {"selected_skills", "stage_plan", "output_contract", "blocked_reason"}
        if stage_tags and stage_tags <= routing_tags and outputs & routing_outputs and not runtime:
            return True
        return False

    def is_executable_runtime_skill(self, skill_id: str) -> bool:
        """Return True when a skill has a concrete runtime and is not a router."""
        if self.is_workflow_router_skill(skill_id):
            return False
        requirements = self.get_runtime_requirements(skill_id)
        return bool(
            requirements.get("env")
            or requirements.get("default_env")
            or requirements.get("healthcheck")
            or requirements.get("packages")
            or requirements.get("capabilities")
        )

    @staticmethod
    def _command_terms_from_text(text: str) -> List[str]:
        ignored = {
            "bash", "sh", "python", "python3", "r", "rscript", "conda", "mamba",
            "run", "env", "which", "echo", "cat", "grep", "head", "tail", "sed",
            "awk", "sort", "uniq", "cut", "tr", "test", "ls", "find", "mkdir",
            "chmod", "cd", "true", "false", "printf",
        }
        terms: List[str] = []
        for token in re.findall(r"(?<![-\w./])([A-Za-z][A-Za-z0-9_.+-]*)(?=\s|$)", str(text or "")):
            lower = token.lower()
            if lower in ignored or lower.startswith("-"):
                continue
            if lower not in terms:
                terms.append(lower)
        return terms

    def runtime_tool_terms(self, skill_id: str) -> List[str]:
        """Extract executable/tool terms declared by a skill runtime."""
        meta = self.get_skill_meta(skill_id)
        if not meta or self.is_workflow_router_skill(skill_id):
            return []
        requirements = self.get_runtime_requirements(skill_id)
        terms: List[str] = []
        for source in (
            requirements.get("healthcheck", ""),
            requirements.get("install_tool", ""),
            " ".join(map(str, requirements.get("packages") or [])),
            " ".join(map(str, requirements.get("capabilities") or [])),
        ):
            for term in self._command_terms_from_text(str(source or "")):
                if term not in terms:
                    terms.append(term)
        return terms

    def find_runtime_skills_for_tool(
        self,
        tool: str,
        selected_skill_ids: Optional[List[str]] = None,
        max_results: int = 5,
    ) -> List[str]:
        """Find executable runtime skills that declare or healthcheck a tool."""
        requested = str(tool or "").strip().split("/")[-1].lower()
        if not requested or requested.startswith("-"):
            return []
        selected = set(self.normalize_selected_skills(selected_skill_ids or [], max_results=self.MAX_SELECTED_SKILLS))
        selected_executable = {
            sid for sid in selected
            if self.is_executable_runtime_skill(sid)
        }
        scored: List[tuple[int, str]] = []
        for meta in self.discovered_skills:
            sid = meta.get("_skill_id", "")
            if not sid or not self.is_executable_runtime_skill(sid):
                continue
            # If the current plan already selected concrete executable skills,
            # runtime inference must stay inside that pool. Otherwise a generic
            # token such as "ONT"/"nanopore" or a hallucinated command can pull
            # an unrelated executable skill into the active task and trigger an
            # installer for the wrong environment. Workflow-router-only context
            # is still allowed to resolve to concrete runtime skills.
            if selected_executable and sid not in selected_executable:
                continue
            requirements = self.get_runtime_requirements(sid)
            healthcheck_terms = set(self._command_terms_from_text(requirements.get("healthcheck", "")))
            package_terms = {str(item).lower() for item in requirements.get("packages", []) or []}
            capability_terms = {str(item).lower() for item in requirements.get("capabilities", []) or []}
            install_tool = str(requirements.get("install_tool") or "").lower()
            score = 0
            if requested in healthcheck_terms:
                score += 80
            if requested in package_terms:
                score += 25
            if requested in capability_terms:
                score += 20
            if requested and requested == install_tool:
                score += 30
            if score <= 0:
                continue
            if sid in selected:
                score += 15
            tags = {str(item).lower() for item in self.metadata_list(meta, "tags")}
            description = str(meta.get("description") or "").lower()
            if "primary-caller" in tags:
                score += 5
            if "optional-caller" in tags:
                score -= 3
            if "legacy" in tags or "legacy" in description or "fallback" in description:
                score -= 5
            if score:
                scored.append((score, sid))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [sid for _score, sid in scored[:max_results]]

    def normalize_selected_skills(self, skill_ids: List[str], max_results: int = None) -> List[str]:
        """Resolve aliases, drop unknown IDs, dedupe, and keep a bounded list."""
        if max_results is None:
            max_results = self.MAX_SELECTED_SKILLS
        resolved: List[str] = []
        for skill_id in skill_ids or []:
            canonical = self._resolve_skill_id(skill_id)
            if canonical and canonical not in resolved:
                resolved.append(canonical)
        return resolved[:max_results]

    def validate_selected_skills(self, skill_ids: List[str], max_results: int = None) -> List[str]:
        """Return human-readable issues for a proposed skill selection."""
        if max_results is None:
            max_results = self.MAX_SELECTED_SKILLS
        issues: List[str] = []
        raw = [sid for sid in (skill_ids or []) if str(sid).strip()]
        normalized = self.normalize_selected_skills(raw, max_results=max_results)
        for sid in raw:
            if not self._resolve_skill_id(sid):
                issues.append(f"Unknown skill id: {sid}")
        if not normalized:
            issues.append("No valid skill IDs were selected.")
            return issues
        if len(raw) > max_results:
            issues.append(f"Too many selected skills: {len(raw)} > {max_results}")
        return issues

    @staticmethod
    def _normalized_skill_id_for_repair(skill_id: Any) -> str:
        """Normalize ID spelling only; this is not a semantic matcher."""
        parts = []
        for part in str(skill_id or "").strip().lower().split("/"):
            normalized = re.sub(r"[-_]+", "-", part).strip("-")
            if normalized:
                parts.append(normalized)
        return "/".join(parts)

    @staticmethod
    def _skill_id_is_single_edit_neighbor(left: str, right: str) -> bool:
        """Return true for one insertion, deletion, or substitution in an ID."""
        if left == right:
            return True
        if abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) == 1
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        index_short = index_long = differences = 0
        while index_short < len(shorter) and index_long < len(longer):
            if shorter[index_short] == longer[index_long]:
                index_short += 1
                index_long += 1
                continue
            differences += 1
            index_long += 1
            if differences > 1:
                return False
        return True

    def repair_selected_skill_ids(
        self,
        raw_ids: List[str],
        candidate_ids: List[str],
        preserved_ids: List[str] = None,
        max_results: int = None,
    ) -> Dict[str, List[str]]:
        """Repair misspelled IDs inside one fixed Consultant candidate allowlist.

        Valid registry selections are preserved. Unknown IDs can map only to a
        unique spelling neighbor among the original candidates; token overlap or
        global skill relevance never authorizes a replacement.
        """
        limit = max_results or self.MAX_SELECTED_SKILLS
        candidate_allowlist = self.normalize_selected_skills(candidate_ids or [], max_results=limit)
        valid_selected = [
            skill_id
            for skill_id in self.normalize_selected_skills(preserved_ids or [], max_results=limit)
            if skill_id in candidate_allowlist
        ]
        invalid_ids: List[str] = []
        for raw_id in raw_ids or []:
            value = str(raw_id or "").strip()
            if not value:
                continue
            canonical = self._resolve_skill_id(value)
            if canonical and canonical in candidate_allowlist:
                if canonical not in valid_selected:
                    valid_selected.append(canonical)
            elif value not in invalid_ids:
                invalid_ids.append(value)

        repair_keys: Dict[str, set[str]] = {}
        candidate_spellings: Dict[str, set[str]] = {}
        for candidate in candidate_allowlist:
            meta = self.get_skill_meta(candidate)
            identifiers = self._skill_identifier_candidates(meta) if meta else [candidate]
            identifiers.append(candidate)
            normalized_spellings = {
                self._normalized_skill_id_for_repair(identifier)
                for identifier in identifiers
                if str(identifier or "").strip()
            }
            candidate_spellings[candidate] = normalized_spellings
            for spelling in normalized_spellings:
                repair_keys.setdefault(spelling, set()).add(candidate)

        repaired_ids: List[str] = []
        unresolved: List[str] = []
        for invalid_id in invalid_ids:
            invalid_key = self._normalized_skill_id_for_repair(invalid_id)
            matches = set(repair_keys.get(invalid_key, set()))
            if not matches and invalid_key:
                matches = {
                    candidate
                    for candidate, spellings in candidate_spellings.items()
                    if any(self._skill_id_is_single_edit_neighbor(invalid_key, spelling) for spelling in spellings)
                }
            if len(matches) == 1:
                replacement = next(iter(matches))
                if replacement not in valid_selected and replacement not in repaired_ids:
                    repaired_ids.append(replacement)
            else:
                unresolved.append(invalid_id)

        selected = list(dict.fromkeys([*valid_selected, *repaired_ids]))[:limit]
        return {
            "candidate_ids": candidate_allowlist,
            "valid_selected": valid_selected[:limit],
            "invalid_ids": invalid_ids,
            "repaired_ids": repaired_ids[:limit],
            "unresolved_invalid_ids": unresolved,
            "selected": selected,
        }

    def validate_selected_skills_for_analysis(self, analysis_type: str, skill_ids: List[str]) -> List[str]:
        """Check selected skills against inferred analysis type using declared stage tags."""
        expected = str(analysis_type or "").strip().lower().replace("-", "_")
        if not expected or expected == "general_analysis":
            return []
        issues: List[str] = []
        for sid in self.normalize_selected_skills(skill_ids or [], max_results=self.MAX_SELECTED_SKILLS):
            meta = self.get_skill_meta(sid)
            stage_tags = [
                str(tag).strip().lower().replace("-", "_")
                for tag in self.metadata_list(meta, "stage_tags")
                if str(tag).strip()
            ]
            if stage_tags and expected not in stage_tags:
                issues.append(
                    f"Selected skill '{sid}' declares stage_tags {stage_tags}, "
                    f"which do not match inferred analysis_type '{expected}'."
                )
        return issues

    def get_runtime_env(self, skill_id: str) -> str:
        runtime = (self.get_skill_meta(skill_id).get("runtime") or {})
        default_env = str(runtime.get("default_env", "") or "").strip()
        if default_env:
            return default_env
        record = self.get_runtime_record(skill_id)
        if record.get("env") and record.get("status") in {"installed", "verified", "success"}:
            return record["env"]
        return ""

    def get_execution_cards(
        self,
        skill_ids: List[str],
        *,
        legacy_renderer_mode: bool = False,
    ) -> str:
        """Concise, high-priority execution facts for Planner/Generator."""
        cards = []
        for sid in skill_ids:
            meta = self.get_skill_meta(sid)
            if not meta:
                continue
            skill_dir = Path(meta.get("_skill_dir") or (self.skills_root / sid)).resolve()
            runtime = meta.get("runtime") or {}
            record = self.get_runtime_record(sid)
            scripts = meta.get("_scripts", []) or []
            requirements = self.get_runtime_requirements(sid)
            renderer = self.get_renderer_record(sid)
            cards.append(f"### Skill Execution Card: {sid}")
            cards.append(f"- absolute_dir: {skill_dir}")
            cards.append("- action_mode: managed_generate")
            output_capabilities = [
                dict(item) for item in meta.get("outputs", []) or []
                if isinstance(item, Mapping)
            ]
            if output_capabilities:
                cards.append(
                    "- canonical_output_capabilities: "
                    + json.dumps(output_capabilities, ensure_ascii=False, sort_keys=True)
                )
            generate = meta.get("generate") if isinstance(meta.get("generate"), Mapping) else {}
            guidance = [str(item) for item in generate.get("execution_guidance", []) or [] if str(item)]
            if guidance:
                cards.append("- generate_execution_guidance:")
                cards.extend(f"  - {item}" for item in guidance)
            if legacy_renderer_mode and renderer.get("status") == "declared":
                cards.extend([
                    "- execution_contract: legacy_renderer_v1",
                    f"- renderer_id: {renderer.get('renderer_id')}",
                    f"- renderer_entrypoint: {renderer.get('resolved_entrypoint')}",
                    f"- renderer_runner: {renderer.get('runner')}",
                    f"- renderer_manifest: {renderer.get('manifest_filename')} ({renderer.get('manifest_type')} v{renderer.get('schema_version')})",
                    "- renderer_plan_policy: one composite Plan step; manifest actions own internal execution order",
                ])
                owned_outputs = [
                    str(item.get("path_or_name") or item.get("path") or item.get("filename") or item.get("output_id") or "")
                    if isinstance(item, dict) else str(item)
                    for item in renderer.get("owned_outputs", []) or []
                    if str(item).strip()
                ]
                if owned_outputs:
                    cards.append("- renderer_owned_output_capabilities: " + ", ".join(owned_outputs))
                output_bindings = dict(renderer.get("output_bindings") or {})
                if output_bindings:
                    compact_bindings = {
                        str(logical): {
                            key: value
                            for key, value in dict(binding or {}).items()
                            if key in {"mode", "argument", "path", "filename", "logical_output"}
                            and value not in (None, "", [], {})
                        }
                        for logical, binding in output_bindings.items()
                    }
                    cards.append(
                        "- renderer_output_binding_contract: "
                        + json.dumps(compact_bindings, ensure_ascii=False, sort_keys=True)
                    )
                invocation = dict(renderer.get("invocation") or {})
                argument_specs = invocation.get("arguments") or {}
                if isinstance(argument_specs, dict) and argument_specs:
                    required = [
                        str(name)
                        for name, spec in argument_specs.items()
                        if isinstance(spec, dict) and bool(spec.get("required"))
                    ]
                    aliases = {
                        str(name): [str(item) for item in spec.get("aliases") or []]
                        for name, spec in argument_specs.items()
                        if isinstance(spec, dict) and spec.get("aliases")
                    }
                    cards.append("- renderer_argument_names: " + ", ".join(str(name) for name in argument_specs))
                    if required:
                        cards.append("- renderer_required_arguments: " + ", ".join(required))
                    if aliases:
                        cards.append("- renderer_argument_aliases: " + json.dumps(aliases, ensure_ascii=False, sort_keys=True))
                routed = invocation.get("routed_arguments") or {}
                if isinstance(routed, dict) and routed:
                    cards.append("- renderer_routed_arguments: " + json.dumps(routed, ensure_ascii=False, sort_keys=True))
            if record:
                remembered_env = str(record.get("env", "") or "")
                effective_env = str(requirements.get("env", "") or requirements.get("default_env", "") or "")
                if remembered_env and remembered_env == effective_env:
                    cards.append(f"- remembered_env: {remembered_env}")
                elif remembered_env:
                    cards.append("- remembered_env_status: ignored because skill runtime metadata defines a different environment")
                cards.append(f"- environment_status: {record.get('status', 'unknown')}")
                cards.append(f"- last_verified_at: {record.get('last_verified_at', 'unknown')}")
                remembered_runner = str(record.get("env_runner", "") or "")
                effective_runner = str(requirements.get("env_runner", "") or "")
                if remembered_runner and remembered_runner == effective_runner:
                    cards.append(f"- remembered_env_runner: {record.get('env_runner')}")
                elif remembered_runner:
                    cards.append("- remembered_env_runner_status: ignored because skill runtime metadata defines a different runner")
                remembered_healthcheck = str(record.get("healthcheck", "") or "")
                effective_healthcheck = str(requirements.get("healthcheck", "") or "")
                if remembered_healthcheck and remembered_healthcheck == effective_healthcheck:
                    cards.append(f"- remembered_healthcheck: {record.get('healthcheck')}")
                elif remembered_healthcheck:
                    cards.append("- remembered_healthcheck_status: ignored because skill runtime metadata defines a different healthcheck")
                if record.get("last_success_command"):
                    cards.append(
                        "- historical_success_command: omitted from execution context; "
                        "re-resolve current paths from the runtime audit and active skill docs"
                    )
                cards.append("- install_policy: verify registered env first; install only if healthcheck fails")
            if requirements.get("default_env"):
                cards.append(f"- default_env: {requirements.get('default_env')}")
            if requirements.get("env_runner"):
                cards.append(f"- env_runner: {requirements.get('env_runner')}")
            if requirements.get("language"):
                cards.append(f"- language: {requirements.get('language')}")
            if requirements.get("packages"):
                cards.append(f"- packages: {', '.join(map(str, requirements.get('packages')))}")
            if requirements.get("capabilities"):
                cards.append(f"- capabilities: {', '.join(map(str, requirements.get('capabilities')))}")
            stage_tags = self.metadata_list(meta, "stage_tags")
            if stage_tags:
                cards.append(f"- stage_tags: {', '.join(stage_tags)}")
            task_types = self.metadata_list(meta, "task_types")
            if task_types:
                cards.append(f"- task_types: {', '.join(task_types)}")
            if requirements.get("healthcheck"):
                cards.append(f"- healthcheck: {requirements.get('healthcheck')}")
            if requirements.get("install_tool"):
                cards.append(f"- install_tool: {requirements.get('install_tool')}")
            if requirements.get("install_policy"):
                cards.append(f"- install_policy: {requirements.get('install_policy')}")
            if requirements.get("env_file"):
                cards.append(f"- env_file: {requirements.get('env_file')}")
            if requirements.get("install_commands"):
                cards.append("- install_commands:")
                for command in requirements.get("install_commands", []):
                    cards.append(f"  - {command}")
            if requirements.get("install_fallbacks"):
                cards.append("- install_fallbacks:")
                for command in requirements.get("install_fallbacks", []):
                    cards.append(f"  - {command}")
            if self._is_environment_bootstrap_skill(sid, runtime, meta):
                cards.append("- install_strategy: verify first; if verification fails, delegate installation via <install_tool>; then re-verify")
                cards.append("- planner_restriction: do not inline conda/pip/R package installation commands in the plan for this skill")
            legacy_renderer_entrypoint = str(renderer.get("entrypoint") or "")
            for script in scripts:
                if not legacy_renderer_mode and str(script) == legacy_renderer_entrypoint:
                    continue
                script_path = skill_dir / script
                cards.append(f"- script: {script_path}")
                if script_path.suffix.lower() == ".py":
                    try:
                        script_source = script_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        script_source = ""
                    declared_flags = sorted(set(
                        re.findall(r"['\"](--[A-Za-z0-9][A-Za-z0-9-]*)['\"]", script_source)
                    ))
                    if declared_flags:
                        cards.append(f"  - declared_cli_flags: {', '.join(declared_flags[:60])}")
                    first_line = script_source.splitlines()[0] if script_source else ""
                    if str(requirements.get("env_runner") or "").strip() == "bash" and "python" in first_line:
                        cards.append(
                            "  - script_runner: controller_python "
                            "(controller-side helper; do not inject a Conda Python)"
                        )
            cards.extend([
                "- plan_rules:",
                "  - The planner must use only scripts and command schemas documented in this skill.",
                "  - The planner must not call the skill name as a command.",
                "  - The planner must not invent wrapper commands, script names, flags, or placeholders.",
                "  - If exact CLI details are needed, load and follow this skill's SKILL.md body.",
            ])
        return "\n".join(cards)

    def get_semantic_execution_capabilities(
        self,
        skill_ids: List[str],
    ) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        """Project execution meaning without contract IDs, hashes or paths.

        Operation handles are invocation vocabulary.  The returned controller
        map is never serialized to the model and resolves each handle to the
        pinned script path from the selected skill contract.
        """
        capabilities: List[Dict[str, Any]] = []
        operation_bindings: Dict[str, str] = {}
        for capability_index, sid in enumerate(
            self.normalize_selected_skills(skill_ids, max_results=self.MAX_SELECTED_SKILLS),
            start=1,
        ):
            meta = self.get_skill_meta(sid)
            if not meta:
                continue
            capability_handle = f"capability.C{capability_index}"
            renderer = self.get_renderer_record(sid)
            requirements = self.get_runtime_requirements(sid)
            skill_dir = Path(meta.get("_skill_dir") or (self.skills_root / sid)).resolve()
            operations: List[Dict[str, Any]] = []
            legacy_renderer_entrypoint = str(renderer.get("entrypoint") or "")
            for operation_index, relative in enumerate(meta.get("_scripts", []) or [], start=1):
                if str(relative) == legacy_renderer_entrypoint:
                    continue
                path = (skill_dir / str(relative)).resolve()
                if not path.is_file():
                    continue
                handle = f"operation.C{capability_index}.O{operation_index}"
                try:
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    source = ""
                flags = sorted(set(
                    re.findall(r"['\"](--[A-Za-z0-9][A-Za-z0-9-]*)['\"]", source)
                ))
                operations.append({
                    "handle": handle,
                    "operation": path.stem,
                    "placeholder": "{" + f"operation:{handle}" + "}",
                    "declared_cli_flags": flags[:60],
                })
                operation_bindings[handle] = str(path)
            row: Dict[str, Any] = {
                "handle": capability_handle,
                "skill_id": sid,
                "purpose": str(meta.get("description") or meta.get("summary") or "").strip(),
                "task_types": self.metadata_list(meta, "task_types"),
                "stage_tags": self.metadata_list(meta, "stage_tags"),
                "action_mode": "managed_generate",
                "operations": operations,
                "output_capabilities": deepcopy(list(meta.get("outputs") or [])),
                "execution_guidance": deepcopy(
                    list((meta.get("generate") or {}).get("execution_guidance") or [])
                    if isinstance(meta.get("generate"), Mapping)
                    else []
                ),
                "runtime": {
                    key: deepcopy(value)
                    for key, value in requirements.items()
                    if key in {"language", "capabilities"}
                    and value not in (None, "", [], {})
                },
            }
            required_bindings = (
                list((meta.get("generate") or {}).get("required_bindings") or [])
                if isinstance(meta.get("generate"), Mapping)
                else []
            )
            row["required_bindings"] = [
                deepcopy(dict(item)) for item in required_bindings
                if isinstance(item, Mapping) and str(item.get("name") or "")
            ]
            capabilities.append(row)
        return capabilities, operation_bindings

    def validate_plan(
        self,
        plan: str,
        selected_skill_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Validate catalog-backed skill facts without interpreting Plan prose.

        Skill assignment is represented by structured ``step.skill_ids`` and
        is validated by the Plan controller.  Free-text phrase, identifier
        presence, and script-name scanners are therefore not authoritative
        Plan validators.  Until a typed operation/script binding exists, a
        ``*.py`` token in prose is deliberately neither accepted nor rejected
        here; execution and provenance contracts remain responsible for what
        can actually run or materialize.
        """
        del plan  # Compatibility parameter; prose is not validation evidence.
        violations: List[Dict[str, Any]] = []
        for index, skill_id in enumerate(selected_skill_ids or []):
            sid = str(skill_id or "").strip()
            if sid and not self.get_skill_meta(sid):
                violations.append({
                    "code": "PLAN_SELECTED_SKILL_UNAVAILABLE",
                    "owner": "planner",
                    "validator_id": "skill_catalog",
                    "field_path": f"selected_skill_ids[{index}]",
                    "repair_target": f"selected_skill_ids[{index}]",
                    "message": f"Selected skill ID is not available in the catalog: {sid}",
                    "selected_skill_id": sid,
                })
        return violations

    def plan_review_telemetry(
        self,
        plan: str,
        selected_skill_ids: List[str],
    ) -> Dict[str, Any]:
        """Return non-authoritative review signals for untyped script mentions.

        There is not yet a complete typed operation-to-script binding for every
        skill.  Preserve observability of the former script-name heuristic
        without turning a prose token into a validation decision.
        """
        known_script_names = {
            Path(path).name
            for skill_id in selected_skill_ids or []
            for path in (self.get_skill_meta(skill_id).get("_scripts", []) or [])
        }
        mentioned = re.findall(
            r"\b[\w.-]+(?:skill|plot|pipeline|workflow)[\w.-]*\.py\b",
            str(plan or ""),
            re.IGNORECASE,
        )
        return {
            "authoritative": False,
            "review_reason": "typed_operation_script_binding_unavailable",
            "undocumented_script_mentions": sorted({
                name for name in mentioned if name not in known_script_names
            }),
        }

    def _runtime_record_from_info(self, skill_id: str, info: Dict[str, Any], env_override: str = "") -> Dict[str, Any]:
        requirements = self.get_runtime_requirements(skill_id)
        env = str(requirements.get("default_env") or requirements.get("env") or env_override or info.get("env") or info.get("default_env") or "").strip()
        language = str(requirements.get("language") or info.get("language") or "").lower()
        env_runner = str(requirements.get("env_runner") or info.get("env_runner") or "").strip()
        if env and not env_runner and language:
            env_runner = f"conda run -n {env} {'Rscript' if language == 'r' else 'python'}"
        return {
            "skill_id": skill_id,
            "env": env,
            "env_runner": env_runner,
            "language": language,
            "packages": self._merge_unique(requirements.get("packages"), info.get("packages")),
            "capabilities": self._merge_unique(requirements.get("capabilities"), info.get("capabilities")),
            "healthcheck": str(requirements.get("healthcheck") or info.get("healthcheck") or "").strip(),
            "install_tool": str(info.get("install_tool") or requirements.get("install_tool") or "").strip(),
            "install_command": str(requirements.get("install_command") or info.get("install_command") or "").strip(),
            "install_commands": self._merge_unique(requirements.get("install_commands"), info.get("install_commands")),
            "install_fallbacks": self._merge_unique(requirements.get("install_fallbacks"), info.get("install_fallbacks")),
            "install_policy": str(requirements.get("install_policy") or info.get("install_policy") or "").strip(),
            "env_file": str(requirements.get("env_file") or info.get("env_file") or "").strip(),
            "status": str(info.get("status") or "registered").lower(),
            "notes": str(info.get("notes") or "").strip(),
            "last_verified_at": str(info.get("last_verified_at") or "").strip(),
        }

    def _merge_runtime_record(self, skill_id: str, info: Dict[str, Any], env_override: str = "") -> bool:
        canonical_id = self._resolve_skill_id(skill_id) or self._normalize_skill_key(skill_id)
        if not canonical_id or not isinstance(info, dict):
            return False
        incoming = self._runtime_record_from_info(canonical_id, info, env_override=env_override)
        if not incoming.get("env"):
            return False
        if incoming.get("status") not in self.INSTALLED_STATUSES:
            return False

        requirements = self.get_runtime_requirements(canonical_id)
        record = self.runtime_registry.setdefault("skills", {}).setdefault(canonical_id, {})
        selected_status = self._best_status(record.get("status", "registered"), incoming.get("status", "registered"))
        record.update({
            "skill_id": canonical_id,
            "env": requirements.get("env") or incoming["env"] or record.get("env", ""),
            "env_runner": requirements.get("env_runner") or incoming["env_runner"] or record.get("env_runner", ""),
            "language": requirements.get("language") or incoming["language"] or record.get("language", ""),
            "packages": self._merge_unique(record.get("packages"), incoming.get("packages")),
            "capabilities": self._merge_unique(record.get("capabilities"), incoming.get("capabilities")),
            "healthcheck": requirements.get("healthcheck") or incoming["healthcheck"] or record.get("healthcheck", ""),
            "install_tool": incoming["install_tool"] or record.get("install_tool", ""),
            "install_command": requirements.get("install_command") or incoming["install_command"] or record.get("install_command", ""),
            "install_commands": self._merge_unique(record.get("install_commands"), incoming.get("install_commands")),
            "install_fallbacks": self._merge_unique(record.get("install_fallbacks"), incoming.get("install_fallbacks")),
            "install_policy": requirements.get("install_policy") or incoming.get("install_policy") or record.get("install_policy", ""),
            "env_file": requirements.get("env_file") or incoming.get("env_file") or record.get("env_file", ""),
            "status": selected_status,
            "notes": incoming["notes"] or record.get("notes", ""),
            "last_verified_at": incoming["last_verified_at"] or record.get("last_verified_at", ""),
        })
        return True

    def _sync_record_to_package_manager(self, package_manager, skill_id: str, record: Dict[str, Any]):
        if not package_manager or not isinstance(record, dict):
            return
        requirements = self.get_runtime_requirements(skill_id)
        env = str(requirements.get("env") or record.get("env") or "").strip()
        status = str(record.get("status") or "").lower()
        if not env or status not in self.INSTALLED_STATUSES:
            return
        package_manager.register_environment(
            env_name=env,
            purpose=f"Runtime for skill {skill_id}",
            packages=requirements.get("packages") or record.get("packages"),
            skill_id=skill_id,
            notes=record.get("notes", ""),
            language=requirements.get("language") or record.get("language", ""),
            capabilities=requirements.get("capabilities") or record.get("capabilities"),
            healthcheck=requirements.get("healthcheck") or record.get("healthcheck", ""),
            status=status,
            last_verified_at=record.get("last_verified_at", ""),
        )

    def merge_runtime_registry_data(
        self,
        data: Dict[str, Any],
        package_manager=None,
        source: str = "",
    ) -> Dict[str, int]:
        """Import skill/environment registry JSON and sync reusable envs to PackageManager."""
        stats = {"skills": 0, "environments": 0}
        if not isinstance(data, dict):
            return stats

        if package_manager and hasattr(package_manager, "merge_registry_data"):
            package_manager.merge_registry_data(data)

        for raw_skill_id, info in (data.get("skills") or {}).items():
            if self._merge_runtime_record(str(raw_skill_id), info if isinstance(info, dict) else {}):
                canonical_id = self._resolve_skill_id(str(raw_skill_id)) or self._normalize_skill_key(raw_skill_id)
                self._sync_record_to_package_manager(
                    package_manager,
                    canonical_id,
                    self.runtime_registry.get("skills", {}).get(canonical_id, {}),
                )
                stats["skills"] += 1

        for env_name, info in (data.get("environments") or {}).items():
            if not isinstance(info, dict):
                continue
            status = str(info.get("status") or "").lower()
            env = str(info.get("env") or env_name or "").strip()
            if not env or status not in self.INSTALLED_STATUSES:
                continue
            for skill_id in self._ensure_list(info.get("skill_ids") or info.get("skill_id")):
                env_info = dict(info)
                env_info["env"] = env
                if self._merge_runtime_record(skill_id, env_info, env_override=env):
                    canonical_id = self._resolve_skill_id(skill_id) or self._normalize_skill_key(skill_id)
                    self._sync_record_to_package_manager(
                        package_manager,
                        canonical_id,
                        self.runtime_registry.get("skills", {}).get(canonical_id, {}),
                    )
                    stats["skills"] += 1
            stats["environments"] += 1

        if stats["skills"]:
            self._save_runtime_registry()
        return stats

    def sync_with_package_manager(self, package_manager) -> Dict[str, int]:
        """Make skill runtime registry and software registry agree on reusable env records."""
        stats = {"skills_to_package": 0, "package_to_skills": 0}
        if not package_manager:
            return stats

        for skill_id, record in list((self.runtime_registry.get("skills") or {}).items()):
            if str(record.get("status") or "").lower() in self.INSTALLED_STATUSES and record.get("env"):
                self._sync_record_to_package_manager(package_manager, skill_id, record)
                stats["skills_to_package"] += 1

        for env_name, info in (getattr(package_manager, "registry", {}).get("environments") or {}).items():
            if not isinstance(info, dict):
                continue
            status = str(info.get("status") or "").lower()
            if status not in self.INSTALLED_STATUSES:
                continue
            for skill_id in self._ensure_list(info.get("skill_ids") or info.get("skill_id")):
                env_info = dict(info)
                env_info["env"] = info.get("env") or env_name
                if self._merge_runtime_record(skill_id, env_info, env_override=str(env_info["env"])):
                    stats["package_to_skills"] += 1
        if stats["package_to_skills"]:
            self._save_runtime_registry()
        return stats

    def import_runtime_registry_files(self, paths: List[str], package_manager=None) -> Dict[str, int]:
        stats = {"files": 0, "skills": 0, "environments": 0}
        seen = set()
        for raw_path in paths or []:
            if not str(raw_path or "").strip():
                continue
            path = Path(str(raw_path)).expanduser()
            try:
                path = path.resolve()
            except Exception:
                pass
            if str(path) in seen or not path.is_file():
                continue
            seen.add(str(path))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            merged = self.merge_runtime_registry_data(data, package_manager=package_manager, source=str(path))
            stats["files"] += 1
            stats["skills"] += merged.get("skills", 0)
            stats["environments"] += merged.get("environments", 0)
        return stats

    def runtime_registry_file_candidates(self, package_manager=None) -> List[str]:
        paths = [str(self.runtime_registry_path)]
        registry_path = getattr(package_manager, "registry_path", None)
        if registry_path:
            paths.append(str(registry_path))
        env_paths = os.getenv("BIOMIND_RUNTIME_REGISTRY_FILES", "")
        if env_paths:
            paths.extend(part.strip() for part in re.split(r"[,;]", env_paths) if part.strip())
        return paths

    def record_environment(
        self,
        skill_id: str,
        env: str,
        packages: List[str] = None,
        install_command: str = "",
        status: str = "installed",
        notes: str = "",
        language: str = "",
        capabilities: List[str] = None,
        healthcheck: str = "",
    ):
        canonical_id = self._resolve_skill_id(skill_id) or self._normalize_skill_key(skill_id)
        requirements = self.get_runtime_requirements(canonical_id)
        record = self.runtime_registry.setdefault("skills", {}).setdefault(canonical_id, {})
        packages = self._ensure_list(packages) or requirements.get("packages", [])
        capabilities = self._ensure_list(capabilities) or requirements.get("capabilities", [])
        selected_language = str(language or requirements.get("language", "")).lower()
        env_runner = requirements.get("env_runner") or (
            f"conda run -n {env} {'Rscript' if selected_language == 'r' else 'python'}" if env else ""
        )
        incoming_status = str(status or "registered").lower()
        selected_status = "broken" if incoming_status == "broken" else self._best_status(record.get("status", "registered"), incoming_status)
        record.update({
            "skill_id": canonical_id,
            "env": env,
            "env_runner": env_runner,
            "language": selected_language,
            "packages": packages,
            "capabilities": capabilities,
            "healthcheck": healthcheck or requirements.get("healthcheck", ""),
            "install_command": install_command or requirements.get("install_command", ""),
            "status": selected_status,
            "notes": notes,
            "last_verified_at": datetime.now().isoformat(),
        })
        self._save_runtime_registry()

    def record_successful_run(
        self,
        skill_id: str,
        env: str,
        command: str,
        outputs: List[str] = None,
        notes: str = "",
    ):
        canonical_id = self._resolve_skill_id(skill_id) or self._normalize_skill_key(skill_id)
        record = self.runtime_registry.setdefault("skills", {}).setdefault(canonical_id, {})
        requirements = self.get_runtime_requirements(canonical_id)
        if env:
            record["env"] = env
            if not record.get("env_runner"):
                language = record.get("language") or requirements.get("language", "")
                record["env_runner"] = f"conda run -n {env} {'Rscript' if language == 'r' else 'python'}"
        if requirements.get("language") and not record.get("language"):
            record["language"] = requirements.get("language")
        if requirements.get("packages") and not record.get("packages"):
            record["packages"] = requirements.get("packages")
        if requirements.get("capabilities") and not record.get("capabilities"):
            record["capabilities"] = requirements.get("capabilities")
        if requirements.get("healthcheck") and not record.get("healthcheck"):
            record["healthcheck"] = requirements.get("healthcheck")
        record["status"] = "success"
        record["last_success_at"] = datetime.now().isoformat()
        record["last_verified_at"] = record["last_success_at"]
        record["last_success_command"] = command
        record["last_outputs"] = outputs or []
        if notes:
            record["notes"] = notes
        self._save_runtime_registry()

    def match_skills(self, text: str, max_results: int = 3) -> List[str]:
        """Retrieve metadata candidates; callers must not treat them as selected."""
        text_l = (text or "").lower()
        scored: List[tuple] = []
        for skill in self.discovered_skills:
            skill_id = skill.get("_skill_id", "")
            if self._is_calder2_skill(skill) and self._plain_compartment_request_without_calder(text_l):
                continue
            if (
                skill_id == "3d-dna/compartment-calling"
                and self._explicit_calder2_request(text_l)
                and not self._explicit_plain_pc1_request(text_l)
            ):
                continue
            seen_hits = set()
            score = 0
            if self._matches_known_alias(skill, text_l):
                score += 8
            if self._matches_declared_runtime_tool(skill, text_l):
                score += 8
            for term, allow_token_match in self._skill_match_term_entries(skill):
                term_l = str(term).lower().strip()
                if not term_l:
                    continue
                if len(term_l) >= 3 and term_l in text_l and term_l not in seen_hits:
                    score += 4 if "/" in term_l else 2
                    seen_hits.add(term_l)
                if allow_token_match:
                    for token in re.split(r"[\s,;:/|()\[\]{}<>，。；、_-]+", term_l):
                        if len(token) >= 2 and token in text_l and token not in seen_hits:
                            score += 1
                            seen_hits.add(token)
            if skill_id == "3d-dna/compartment-calling" and self._plain_compartment_request_without_calder(text_l):
                score += 20
            if score > 0:
                scored.append((score, skill_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [skill_id for _, skill_id in scored[:max_results]]

    def _matches_declared_runtime_tool(self, skill: Dict[str, Any], text_l: str) -> bool:
        """Use exact declared tool names as high-confidence retrieval evidence."""
        runtime = skill.get("runtime") or {}
        for tool in self._ensure_list(runtime.get("tools")):
            token = str(tool or "").strip().lower()
            if len(token) < 3:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text_l):
                return True
        return False

    def score_skill_for_stage(self, skill_id: str, stage: Dict[str, Any], workflow_task_types: List[str] = None) -> Dict[str, Any]:
        """Score one skill against a workflow stage using structured metadata first."""
        meta = self.get_skill_meta(skill_id)
        if not meta:
            return {"skill_id": skill_id, "score": 0, "role": "ignore", "forbidden": False, "reasons": ["unknown skill"]}

        workflow_task_types = self._ensure_list(workflow_task_types)
        stage_name = str(stage.get("stage") or stage.get("stage_id") or "").strip()
        stage_inputs = self._ensure_list(stage.get("inputs"))
        stage_outputs = self._ensure_list(stage.get("outputs"))
        stage_text = " ".join([
            stage_name,
            str(stage.get("title", "")),
            str(stage.get("description", "")),
            " ".join(stage_inputs),
            " ".join(stage_outputs),
            " ".join(workflow_task_types),
        ]).lower()

        score = 0
        reasons: List[str] = []
        forbidden = False
        stage_tags = self.metadata_list(meta, "stage_tags")
        task_types = self.metadata_list(meta, "task_types")
        inputs = self.metadata_list(meta, "inputs")
        outputs = self.metadata_list(meta, "outputs")
        when_not_to_use = self.metadata_list(meta, "when_not_to_use")

        if stage_name and stage_name in stage_tags:
            score += 40
            reasons.append("stage_tags match")
        elif stage_tags:
            reasons.append("stage_tags do not match")

        task_overlap = set(map(str.lower, workflow_task_types)) & set(map(str.lower, task_types))
        if task_overlap:
            score += 25
            reasons.append("task_types match")

        input_overlap = set(map(str.lower, stage_inputs)) & set(map(str.lower, inputs))
        if input_overlap:
            score += 15
            reasons.append("inputs match")

        output_overlap = set(map(str.lower, stage_outputs)) & set(map(str.lower, outputs))
        if output_overlap:
            score += 10
            reasons.append("outputs match")

        description_terms = [
            str(meta.get("_skill_id", "")),
            str(meta.get("name", "")),
            str(meta.get("description", "")),
            " ".join(map(str, meta.get("tags", []) or [])),
        ]
        description_text = " ".join(description_terms).lower()
        if stage_name and stage_name.replace("_", " ") in description_text:
            score += 10
            reasons.append("description match")
        elif stage_name and any(token and token in description_text for token in re.split(r"[_\s-]+", stage_name)):
            score += 5
            reasons.append("description token match")

        for blocker in when_not_to_use:
            blocker_l = blocker.lower()
            if blocker_l and (blocker_l in stage_text or blocker_l in description_text and blocker_l in stage_text):
                forbidden = True
                score = -100
                reasons.append(f"when_not_to_use hit: {blocker}")
                break

        role = "ignore"
        if forbidden:
            role = "forbidden"
        elif score >= 70:
            role = "required"
        elif score >= 40:
            role = "candidate"
        return {"skill_id": meta.get("_skill_id", skill_id), "score": score, "role": role, "forbidden": forbidden, "reasons": reasons}

    def match_skills_for_stage(self, stage: Dict[str, Any], workflow_task_types: List[str] = None, max_results: int = 5) -> List[Dict[str, Any]]:
        scored = [
            self.score_skill_for_stage(skill.get("_skill_id", ""), stage, workflow_task_types)
            for skill in self.discovered_skills
        ]
        scored = [item for item in scored if item["score"] > 0 or item["forbidden"]]
        scored.sort(key=lambda item: (-item["score"], item["skill_id"]))
        return scored[:max_results]

    def _skill_match_terms(self, skill: Dict[str, Any]) -> List[str]:
        return [term for term, _ in self._skill_match_term_entries(skill)]

    def _skill_match_term_entries(self, skill: Dict[str, Any]) -> List[tuple]:
        runtime = skill.get("runtime") or {}
        structured_terms = [
            skill.get("_skill_id", ""),
            skill.get("_skill_leaf", ""),
            skill.get("_skill_category", ""),
            " ".join(map(str, skill.get("_skill_path_tokens", []) or [])),
            " ".join(self._ensure_list(skill.get("_legacy_skill_ids"))),
            skill.get("name", ""),
            " ".join(map(str, skill.get("tags", []) or [])),
            " ".join(self.metadata_list(skill, "task_types")),
            " ".join(self.metadata_list(skill, "stage_tags")),
            " ".join(self.metadata_list(skill, "inputs")),
            " ".join(self.metadata_list(skill, "outputs")),
            " ".join(self.metadata_list(skill, "when_to_use")),
            " ".join(self._ensure_list(runtime.get("capabilities"))),
        ]
        phrase_only_terms = [
            skill.get("description", ""),
            " ".join(self.metadata_list(skill, "when_not_to_use")),
        ]
        return [(term, True) for term in structured_terms] + [(term, False) for term in phrase_only_terms]

    def _matches_known_alias(self, skill: Dict[str, Any], text_l: str) -> bool:
        """Domain aliases catch common Chinese abbreviations not present in YAML tags."""
        aliases = {
            "甲基化基因绘图": ["甲基化", "甲基化图", "甲基化绘图", "bw文件", "bigwig", "tpm"],
            "methylation_profile_plot": ["甲基化", "methylation profile", "bigwig", "cg/chg/chh"],
            "assembly": ["hifiasm", "hifi", "基因组组装", "de novo assembly"],
            "reseq-variant-workflow": ["重测序变异检测", "reseq variant", "resequencing variant"],
            "wgs-short-read-mapping": ["wgs fastq", "双端wgs", "bwa比对", "短读长比对"],
            "wgs-gatk-snp-indel": ["gatk snp", "gatk indel", "gatk snp和indel", "gvcf", "haplotypecaller"],
            "wgs-bcftools-snp-indel": ["bcftools", "mpileup", "bcftools snp"],
            "wgs-manta-sv": ["manta", "短读长sv", "短读长结构变异", "paired-end sv", "short-read sv"],
            "wgs-lumpy-sv": ["lumpy", "lumpy sv", "split read", "discordant"],
            "wgs-cnvnator-cnv": ["cnvnator", "拷贝数变异", "cnv分析", "read-depth cnv"],
            "rnaseq-variant-calling": ["rna-seq variant", "rnaseq variant", "表达区域snp", "rna-seq数据想检测"],
            "long-read-sv-sniffles2": ["sniffles2", "sniffles", "长读长sv", "ont长读长", "pacbio sv", "nanopore sv"],
            "long-read-sv-cutesv": ["cutesv", "cute sv", "cuteSV"],
            "pacbio-sv-pbsv": ["pbsv", "pbmm2", "pacbio官方", "pacbio official"],
            "long-read-sv-sniffles-survivor": ["survivor", "survivor merge", "legacy sniffles"],
            "assembly-variant-syri": ["syri", "syri比较", "组装比较syri", "组装比较", "组装结构变异", "染色体级组装", "assembly variant"],
            "assembly-variant-assemblytics": ["assemblytics"],
            "assembly-variant-svim-asm": ["svim-asm", "svim asm"],
            "popgen-gwas-workflow": ["群体遗传gwas", "gwas流程", "emmax还是tassel", "population gwas"],
            "gwas-vcf-prep-qc": ["gwas输入准备", "样本顺序检查", "表型排序", "vcf和表型"],
            "gwas-emmax-association": ["emmax", "emmax gwas", "emmax关联分析"],
            "gwas-tassel-glm-mlm": ["tassel", "tassel mlm", "tassel glm", "kinship做关联"],
            "gwas-manhattan-qq-plot": ["manhattan", "qq图", "曼哈顿图", "显著位点"],
            "gwas-ldblock-candidate-region": ["ldblockshow", "ldblock", "候选基因", "显著snp附近"],
            "tad_calling": ["tad", "hicfindtads", "拓扑结构域"],
            "tad-calling": ["tad", "hicfindtads", "拓扑结构域"],
            "differential-tad": ["差异tad", "差异 tads", "differential tad", "hicdifferentialtad"],
            "scrna/de/contrast": [
                "差异表达",
                "differential expression",
                "de analysis",
                "ctrl vs stim",
                "condition contrast",
            ],
            "scrna-de-contrast": [
                "差异表达",
                "differential expression",
                "de analysis",
                "ctrl vs stim",
                "condition contrast",
            ],
            "tad-boundary-change": [
                "tad边界改变",
                "tad边界变化",
                "边界改变统计",
                "边界变化统计",
                "boundary change",
                "boundary shift",
                "boundary gain",
                "boundary loss",
            ],
        }
        keys = self._skill_identifier_candidates(skill)
        return any(alias.lower() in text_l for key in keys for alias in aliases.get(key, []))

    def _is_calder2_skill(self, skill: Dict[str, Any]) -> bool:
        keys = {str(key).lower() for key in self._skill_identifier_candidates(skill)}
        return "3d-dna/calder2-subcompartment" in keys or any("calder" in key for key in keys)

    @staticmethod
    def _plain_compartment_request_without_calder(text_l: str) -> bool:
        if not text_l:
            return False
        if SkillManager._explicit_calder2_request(text_l):
            return False
        plain_terms = [
            "区室鉴定",
            "鉴定区室",
            "ab区室",
            "a/b区室",
            "a/b compartment",
            "a/b compartments",
            "ab compartment",
            "ab compartments",
            "compartment calling",
            "call compartments",
            "identify compartments",
            "pc1",
            "eigenvector",
        ]
        return any(term in text_l for term in plain_terms)

    @staticmethod
    def _explicit_calder2_request(text_l: str) -> bool:
        explicit_calder_terms = [
            "calder",
            "calder2",
            "sub-compartment",
            "subcompartment",
            "sub compartment",
            "sub区室",
            "亚区室",
            "层级区室",
            "hierarchical compartment",
            "hierarchical compartments",
            "a.1",
            "a.2",
            "b.1",
            "b.2",
            "compartment4",
            "compartment8",
        ]
        return any(term in text_l for term in explicit_calder_terms)

    @staticmethod
    def _explicit_plain_pc1_request(text_l: str) -> bool:
        plain_terms = [
            "普通",
            "默认",
            "pc1",
            "eigenvector",
            "a/b",
            "ab区室",
            "ordinary",
        ]
        return any(term in text_l for term in plain_terms)

    def load_skill(self, skill_id: str) -> str:
        """3. 加载 (Load)：根据选中的 skill_id，读取完整的正文内容供大模型使用"""
        meta = self.get_skill_meta(skill_id)
        skill_md = Path(meta.get("_skill_md", ""))
        if not skill_md.is_file():
            return ""
            
        raw_content = skill_md.read_text(encoding="utf-8")
        # 剥离 YAML 头部，只保留正文部分 (Body)
        match = re.match(r'^---\s*\n.*?\n---\s*\n(.*)', raw_content, re.DOTALL)
        header = self._build_skill_header(meta.get("_skill_id", skill_id))
        if match:
            return f"{header}\n\n{match.group(1).strip()}"
        return f"{header}\n\n{raw_content.strip()}"

    def _build_skill_header(self, skill_id: str) -> str:
        """Add concrete local paths above SKILL.md so agents do not invent commands."""
        meta = self.get_skill_meta(skill_id)
        skill_dir = Path(meta.get("_skill_dir", self.skills_root / skill_id))
        scripts = self._list_relative_files(skill_dir / "scripts")
        refs = self._list_relative_files(skill_dir / "references")
        aliases = ", ".join(self._ensure_list(meta.get("_legacy_skill_ids"))) or "none"
        lines = [
            "## Local Skill Runtime Metadata",
            f"- skill_id: {meta.get('_skill_id', skill_id)}",
            f"- category: {meta.get('_skill_category', 'root')}",
            f"- aliases: {aliases}",
            f"- absolute_dir: {skill_dir.resolve()}",
        ]
        runtime = meta.get("runtime") or {}
        if runtime:
            lines.append("- runtime:")
            for key, value in runtime.items():
                if key == "install_command":
                    continue
                if isinstance(value, list):
                    value = ", ".join(map(str, value))
                lines.append(f"  - {key}: {value}")
        if scripts:
            lines.append("- executable_scripts:")
            lines.extend(f"  - {skill_dir.resolve() / script}" for script in scripts)
        else:
            lines.append("- executable_scripts: none")
        if refs:
            lines.append("- references:")
            lines.extend(f"  - {skill_dir.resolve() / ref}" for ref in refs)
        else:
            lines.append("- references: none")
        lines.extend([
            "",
            "Rules:",
            "- Use only the scripts and invocation forms documented below.",
            "- Do not call the skill name as a shell command.",
            "- Do not invent flags, filenames, or wrapper commands.",
        ])
        return "\n".join(lines)

    def load_skills_for_prompt(self, skill_ids: List[str]) -> str:
        """Load one or more selected skills with full instructions."""
        bodies = []
        for sid in skill_ids:
            body = self.load_skill(sid)
            if body:
                bodies.append(f"### Skill: {sid}\n{body}")
        if not bodies:
            return ""
        return "<active_skill_docs>\n" + "\n\n".join(bodies) + "\n</active_skill_docs>"

    def _clean_selected_skill_items(self, selected: List[Any]) -> List[str]:
        cleaned = []
        for item in selected:
            normalized = self._clean_selected_skill_item(item)
            if normalized:
                cleaned.append(normalized)
        return cleaned

    def parse_skill_selection_response_details(self, text: str) -> Dict[str, Any]:
        """Parse skill selection responses and record which parser matched."""
        payload = str(text or "").strip()
        if not payload:
            return {"raw_selected": [], "parser": "none"}

        json_candidates = [payload]
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", payload, re.DOTALL)
        if fence_match:
            json_candidates.insert(0, fence_match.group(1).strip())
        brace_match = re.search(r"(\{.*?\})", payload, re.DOTALL)
        if brace_match:
            json_candidates.append(brace_match.group(1).strip())

        seen_candidates = set()

        for candidate in json_candidates:
            if candidate in seen_candidates:
                continue
            seen_candidates.add(candidate)
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                selected = data.get("selected_skills")
                if isinstance(selected, list):
                    return {
                        "raw_selected": self._clean_selected_skill_items(selected),
                        "parser": "json",
                    }
        legacy = self.parse_selected_skill_ids(payload)
        return {
            "raw_selected": legacy,
            "parser": "legacy_tag" if legacy else "none",
        }

    def parse_skill_selection_response(self, text: str) -> List[str]:
        """Parse skill selection responses: JSON first, legacy tag as fallback."""
        return list(self.parse_skill_selection_response_details(text).get("raw_selected", []) or [])

    def parse_selected_skill_ids(self, text: str) -> List[str]:
        """Extract raw skill IDs from the legacy selection marker."""
        match = re.search(r'\[SELECTED_SKILLS\s*:\s*(.*?)\]', text or "", re.DOTALL)
        if not match:
            return []
        ignored = {"none", "n/a", "na", "null"}
        selected: List[str] = []
        raw_items = re.split(r"\s*(?:,|，|;|；|\band\b|和)\s*", match.group(1))
        for item in raw_items:
            cleaned = self._clean_selected_skill_item(item)
            if cleaned and cleaned.lower() not in ignored and cleaned not in selected:
                selected.append(cleaned)
        return selected

    def _clean_selected_skill_item(self, item: Any) -> str:
        """Normalize one parsed skill marker item without resolving aliases."""
        text = str(item or "").strip()
        if not text:
            return ""

        text = re.sub(r"^\s*(?:[-*+•]\s*)+", "", text)
        text = re.sub(r"^\s*\d+[\.)]\s*", "", text)
        text = re.sub(r"^(?:canonical_)?skill_id\s*[:：]\s*", "", text, flags=re.IGNORECASE)

        wrappers = "`'\"“”‘’()[]{}<>"
        previous = None
        while text and text != previous:
            previous = text
            text = text.strip().strip(wrappers).strip()
            text = re.sub(r"^\s*(?:[-*+•]\s*)+", "", text)
            text = re.sub(r"^\s*\d+[\.)]\s*", "", text)
            text = re.sub(r"^(?:canonical_)?skill_id\s*[:：]\s*", "", text, flags=re.IGNORECASE)

        return text.strip()

    def extract_skills_from_plan(self, plan_text: str) -> List[str]:
        """工具函数：从 Planner 的输出中提取它决定使用的 skill_id"""
        active = self.extract_active_skills_from_plan(plan_text)
        if active:
            return active
        # Legacy fallback: Planner outputs [SELECTED_SKILLS: skill1, skill2]
        requested = self.parse_selected_skill_ids(plan_text)
        if requested:
            return self.normalize_selected_skills(requested, max_results=self.MAX_SELECTED_SKILLS)
        return[]

    def extract_active_skills_from_plan(self, plan_text: str) -> List[str]:
        return self.extract_skills_from_block(
            plan_text,
            "ACTIVE_SKILLS",
            max_results=self.MAX_ACTIVE_SKILLS,
        )

    def extract_future_skills_from_plan(self, plan_text: str) -> List[str]:
        return self.extract_skills_from_block(plan_text, "FUTURE_SKILLS")

    def extract_skills_from_block(self, plan_text: str, block_name: str, max_results: int = None) -> List[str]:
        match = re.search(rf"\[{re.escape(block_name)}\]\s*(.*?)\s*\[/{re.escape(block_name)}\]", plan_text or "", re.DOTALL | re.IGNORECASE)
        if not match:
            return []
        raw_items = re.split(r"[,\n;，；]+", match.group(1))
        selected = []
        for item in raw_items:
            cleaned = self._clean_selected_skill_item(item)
            if cleaned and cleaned.lower() not in {"none", "n/a", "na", "null"}:
                selected.append(cleaned)
        return self.normalize_selected_skills(
            selected,
            max_results=max_results or self.MAX_SELECTED_SKILLS,
        )
