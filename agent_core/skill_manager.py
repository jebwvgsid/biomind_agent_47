import json
import os
import re
import yaml
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

class SkillManager:
    """遵循 Agent Skills 标准的技能加载器"""
    MAX_SELECTED_SKILLS = 3
    INSTALLED_STATUSES = {"installed", "verified", "success"}
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
                            "task_types", "stage_tags", "inputs", "outputs",
                            "when_to_use", "when_not_to_use",
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
        meta = self.get_skill_meta(skill_id)
        candidates = [skill_id]
        if meta:
            candidates = self._skill_identifier_candidates(meta)
        for candidate in candidates:
            record = self.runtime_registry.get("skills", {}).get(candidate, {})
            if record:
                return record
        return {}

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

    def build_runtime_healthcheck(self, env: str, runtime: Dict[str, Any], meta: Dict[str, Any] = None) -> str:
        if not env:
            return ""
        runtime = runtime or {}
        meta = meta or {}
        healthcheck = str(runtime.get("healthcheck", "") or "")
        if healthcheck:
            return healthcheck.replace("{env}", env)
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
                    imports.append(token.replace("-", "_"))
            imports = list(dict.fromkeys(imports))
            if imports:
                imports_str = ", ".join(imports)
                return f'conda run -n {env} python -c "import {imports_str}; print(\'OK\')"'
        return f'conda run -n {env} python -c "print(\'OK\')"'

    def get_runtime_requirements(self, skill_id: str) -> Dict[str, Any]:
        meta = self.get_skill_meta(skill_id)
        runtime = meta.get("runtime") or {}
        canonical_id = meta.get("_skill_id", skill_id)
        record = self.get_runtime_record(canonical_id)
        env = self.get_runtime_env(canonical_id) or runtime.get("default_env", "")
        packages = self._merge_unique(self._runtime_packages(runtime), record.get("packages"))
        language = self._runtime_language(runtime)
        if not language:
            language = str(record.get("language", "") or "").lower()
        capabilities = self._merge_unique(self._runtime_capabilities(runtime, meta), record.get("capabilities"))
        env_runner = record.get("env_runner") or runtime.get("env_runner", "")
        healthcheck = record.get("healthcheck") or self.build_runtime_healthcheck(env or runtime.get("default_env", ""), runtime, meta)
        return {
            "skill_id": canonical_id,
            "default_env": runtime.get("default_env", ""),
            "env": env,
            "env_runner": env_runner,
            "language": language,
            "packages": packages,
            "capabilities": capabilities,
            "install_tool": record.get("install_tool") or runtime.get("install_tool", ""),
            "install_command": record.get("install_command") or runtime.get("install_command", ""),
            "healthcheck": healthcheck,
        }

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
                )
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

    def get_skill_tree_prompt(self) -> str:
        """Compact tree view for LLM-first skill selection."""
        if not self.discovered_skills:
            return "<skill_tree>\nNo external skills available.\n</skill_tree>"
        lines = ["<skill_tree>", "skills/"]
        lines.extend(self._render_tree_lines(self._skill_tree_structure()))
        lines.append("</skill_tree>")
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

    def get_runtime_env(self, skill_id: str) -> str:
        record = self.get_runtime_record(skill_id)
        if record.get("env") and record.get("status") in {"installed", "verified", "success"}:
            return record["env"]
        runtime = (self.get_skill_meta(skill_id).get("runtime") or {})
        return runtime.get("default_env", "")

    def get_execution_cards(self, skill_ids: List[str]) -> str:
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
            cards.append(f"### Skill Execution Card: {sid}")
            cards.append(f"- absolute_dir: {skill_dir}")
            if record:
                cards.append(f"- remembered_env: {record.get('env', 'unknown')}")
                cards.append(f"- environment_status: {record.get('status', 'unknown')}")
                cards.append(f"- last_verified_at: {record.get('last_verified_at', 'unknown')}")
                if record.get("env_runner"):
                    cards.append(f"- remembered_env_runner: {record.get('env_runner')}")
                if record.get("healthcheck"):
                    cards.append(f"- remembered_healthcheck: {record.get('healthcheck')}")
                if record.get("last_success_command"):
                    cards.append(f"- last_success_command: {record.get('last_success_command')}")
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
            if self._is_environment_bootstrap_skill(sid, runtime, meta):
                cards.append("- install_strategy: verify first; if verification fails, delegate installation via <install_tool>; then re-verify")
                cards.append("- planner_restriction: do not inline conda/pip/R package installation commands in the plan for this skill")
            for script in scripts:
                cards.append(f"- script: {skill_dir / script}")
            cards.extend([
                "- plan_rules:",
                "  - The planner must use only scripts and command schemas documented in this skill.",
                "  - The planner must not call the skill name as a command.",
                "  - The planner must not invent wrapper commands, script names, flags, or placeholders.",
                "  - If exact CLI details are needed, load and follow this skill's SKILL.md body.",
            ])
        return "\n".join(cards)

    def validate_plan(self, plan: str, selected_skill_ids: List[str]) -> List[str]:
        """Generic skill-plan checks. Returns human-readable violations."""
        if not selected_skill_ids:
            return []
        violations = []
        lower_plan = plan.lower()
        generic_banned = [
            "command template",
            "placeholder",
            "abstracted",
            "internal skill module",
            "assumed python-based pipeline",
            "reconstruct it using standard",
        ]
        for phrase in generic_banned:
            if phrase in lower_plan:
                violations.append(f"Plan contains forbidden phrase for selected skill: '{phrase}'")

        for sid in selected_skill_ids:
            meta = self.get_skill_meta(sid)
            if not meta:
                violations.append(f"Selected skill id is not available: {sid}")
                continue
            scripts = [Path(p).name for p in meta.get("_scripts", []) or []]
            runtime = self.get_runtime_requirements(sid)
            identifiers = self._skill_identifier_candidates(meta)
            if not any(identifier and identifier in plan for identifier in identifiers):
                violations.append(f"Plan selected skill '{sid}' but does not mention its skill id.")
            if scripts and not any(script in plan for script in scripts):
                violations.append(
                    f"Plan selected skill '{sid}' but does not mention any documented script: {', '.join(scripts)}"
                )
            if runtime.get("default_env") and runtime.get("default_env") not in plan:
                violations.append(
                    f"Plan selected skill '{sid}' but does not mention default_env '{runtime.get('default_env')}'."
                )
            known_script_names = set(scripts)
            invented = re.findall(r"\b[\w.-]+(?:skill|plot|pipeline|workflow)[\w.-]*\.py\b", plan, re.IGNORECASE)
            for name in invented:
                if name not in known_script_names:
                    violations.append(f"Plan appears to invent an undocumented script name: {name}")
        return violations

    def _runtime_record_from_info(self, skill_id: str, info: Dict[str, Any], env_override: str = "") -> Dict[str, Any]:
        requirements = self.get_runtime_requirements(skill_id)
        env = str(env_override or info.get("env") or info.get("default_env") or requirements.get("env") or "").strip()
        language = str(info.get("language") or requirements.get("language") or "").lower()
        env_runner = str(info.get("env_runner") or requirements.get("env_runner") or "").strip()
        if env and not env_runner and language:
            env_runner = f"conda run -n {env} {'Rscript' if language == 'r' else 'python'}"
        return {
            "skill_id": skill_id,
            "env": env,
            "env_runner": env_runner,
            "language": language,
            "packages": self._merge_unique(requirements.get("packages"), info.get("packages")),
            "capabilities": self._merge_unique(requirements.get("capabilities"), info.get("capabilities")),
            "healthcheck": str(info.get("healthcheck") or requirements.get("healthcheck") or "").strip(),
            "install_tool": str(info.get("install_tool") or requirements.get("install_tool") or "").strip(),
            "install_command": str(info.get("install_command") or requirements.get("install_command") or "").strip(),
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

        record = self.runtime_registry.setdefault("skills", {}).setdefault(canonical_id, {})
        selected_status = self._best_status(record.get("status", "registered"), incoming.get("status", "registered"))
        record.update({
            "skill_id": canonical_id,
            "env": incoming["env"] or record.get("env", ""),
            "env_runner": incoming["env_runner"] or record.get("env_runner", ""),
            "language": incoming["language"] or record.get("language", ""),
            "packages": self._merge_unique(record.get("packages"), incoming.get("packages")),
            "capabilities": self._merge_unique(record.get("capabilities"), incoming.get("capabilities")),
            "healthcheck": incoming["healthcheck"] or record.get("healthcheck", ""),
            "install_tool": incoming["install_tool"] or record.get("install_tool", ""),
            "install_command": incoming["install_command"] or record.get("install_command", ""),
            "status": selected_status,
            "notes": incoming["notes"] or record.get("notes", ""),
            "last_verified_at": incoming["last_verified_at"] or record.get("last_verified_at", ""),
        })
        return True

    def _sync_record_to_package_manager(self, package_manager, skill_id: str, record: Dict[str, Any]):
        if not package_manager or not isinstance(record, dict):
            return
        env = str(record.get("env") or "").strip()
        status = str(record.get("status") or "").lower()
        if not env or status not in self.INSTALLED_STATUSES:
            return
        package_manager.register_environment(
            env_name=env,
            purpose=f"Runtime for skill {skill_id}",
            packages=record.get("packages"),
            skill_id=skill_id,
            notes=record.get("notes", ""),
            language=record.get("language", ""),
            capabilities=record.get("capabilities"),
            healthcheck=record.get("healthcheck", ""),
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
        """Fallback deterministic matcher when LLM-side selection is missing or invalid."""
        text_l = (text or "").lower()
        scored: List[tuple] = []
        for skill in self.discovered_skills:
            skill_id = skill.get("_skill_id", "")
            terms = self._skill_match_terms(skill)
            seen_hits = set()
            score = 0
            if self._matches_known_alias(skill, text_l):
                score += 8
            for term in terms:
                term_l = str(term).lower().strip()
                if not term_l:
                    continue
                if len(term_l) >= 3 and term_l in text_l and term_l not in seen_hits:
                    score += 4 if "/" in term_l else 2
                    seen_hits.add(term_l)
                for token in re.split(r"[\s,;:/|()\[\]{}<>，。；、_-]+", term_l):
                    if len(token) >= 2 and token in text_l and token not in seen_hits:
                        score += 1
                        seen_hits.add(token)
            if score > 0:
                scored.append((score, skill_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [skill_id for _, skill_id in scored[:max_results]]

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
        runtime = skill.get("runtime") or {}
        return [
            skill.get("_skill_id", ""),
            skill.get("_skill_leaf", ""),
            skill.get("_skill_category", ""),
            " ".join(map(str, skill.get("_skill_path_tokens", []) or [])),
            " ".join(self._ensure_list(skill.get("_legacy_skill_ids"))),
            skill.get("name", ""),
            skill.get("description", ""),
            " ".join(map(str, skill.get("tags", []) or [])),
            " ".join(self.metadata_list(skill, "task_types")),
            " ".join(self.metadata_list(skill, "stage_tags")),
            " ".join(self.metadata_list(skill, "inputs")),
            " ".join(self.metadata_list(skill, "outputs")),
            " ".join(self.metadata_list(skill, "when_to_use")),
            " ".join(self._ensure_list(runtime.get("capabilities"))),
        ]

    def _matches_known_alias(self, skill: Dict[str, Any], text_l: str) -> bool:
        """Domain aliases catch common Chinese abbreviations not present in YAML tags."""
        aliases = {
            "甲基化基因绘图": ["甲基化", "甲基化图", "甲基化绘图", "bw文件", "bigwig", "tpm"],
            "methylation_profile_plot": ["甲基化", "methylation profile", "bigwig", "cg/chg/chh"],
            "assembly": ["组装", "hifiasm", "hifi"],
            "tad_calling": ["tad", "hicfindtads", "拓扑结构域"],
            "tad-calling": ["tad", "hicfindtads", "拓扑结构域"],
            "differential-tad": ["差异tad", "差异 tads", "differential tad", "hicdifferentialtad"],
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
        return self.extract_skills_from_block(plan_text, "ACTIVE_SKILLS")

    def extract_future_skills_from_plan(self, plan_text: str) -> List[str]:
        return self.extract_skills_from_block(plan_text, "FUTURE_SKILLS")

    def extract_skills_from_block(self, plan_text: str, block_name: str) -> List[str]:
        match = re.search(rf"\[{re.escape(block_name)}\]\s*(.*?)\s*\[/{re.escape(block_name)}\]", plan_text or "", re.DOTALL | re.IGNORECASE)
        if not match:
            return []
        raw_items = re.split(r"[,\n;，；]+", match.group(1))
        selected = []
        for item in raw_items:
            cleaned = self._clean_selected_skill_item(item)
            if cleaned and cleaned.lower() not in {"none", "n/a", "na", "null"}:
                selected.append(cleaned)
        return self.normalize_selected_skills(selected, max_results=self.MAX_SELECTED_SKILLS)
