"""Passive software registration and existing Conda inventory path metadata.

No configuration imports, shell startup, Conda invocation, scanning, healthcheck
or registration writes. Inventory only locates an already successful registration;
it cannot create one or resolve duplicate environment names by guesswork.
"""
import hashlib
import json
import os
from pathlib import Path


def registry_path(install_root, explicit=None, *, environment=None):
    environment = os.environ if environment is None else environment
    if explicit or environment.get('BIOMIND_PI_REGISTRY_PATH'):
        return Path(explicit or environment['BIOMIND_PI_REGISTRY_PATH']).expanduser()
    root = environment.get('BIO_SOFTWARE_ROOT') or install_root
    return Path(root).expanduser() / 'software_registry.json'


class RegistryView:
    def __init__(self, path, environment_inventory=None):
        self.path = Path(path) if path else None
        self.environment_inventory = (Path(environment_inventory).expanduser()
                                      if environment_inventory else Path.home()/'.conda/environments.txt')
        self.reload()

    def reload(self):
        self.registry = (json.loads(self.path.read_text()) if self.path and self.path.is_file()
                         else {'environments': {}, 'tools': {}})
        if not isinstance(self.registry, dict):
            raise ValueError('REGISTRY_NOT_OBJECT')

    def _known_environment_prefixes(self):
        found = {}
        try:
            lines = self.environment_inventory.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            lines = []
        for raw in lines:
            path = Path(raw.strip())
            if not path.is_absolute() or not path.is_dir():
                continue
            value = str(path.resolve())
            if value not in found.setdefault(path.name, []):
                found[path.name].append(value)
        return found

    def _registered_prefixes(self, name, row, known=None):
        explicit = (row.get('prefix') or row.get('path')
                    or (row.get('resolved_runtime_paths') or {}).get('RESOLVED_ENV_PREFIX'))
        if explicit:
            path = Path(str(explicit))
            return [str(path.resolve())] if path.is_absolute() else []
        return (known if known is not None else self._known_environment_prefixes()).get(name, [])


def registered_scientific_tool(path, environment_inventory=None):
    """Use the product selection predicate; retain only nonsecret plan metadata."""
    from agent_core.registered_runtime import registered_runtime_reuse
    view = RegistryView(path, environment_inventory)
    for name in ('bcftools', 'samtools'):
        for env_name in view.registry.get('environments') or {}:
            result = registered_runtime_reuse(view, {'tools': [name]}, skill_record={}, target_env=env_name)
            if result.get('availability') != 'AVAILABLE':
                continue
            candidate = result['selected_candidate']
            binary = Path(candidate['prefix']) / 'bin' / name
            if not binary.is_file() or not os.access(binary, os.X_OK):
                continue
            record = view.registry['environments'][env_name]
            return {'name': name, 'env_name': env_name, 'prefix': candidate['prefix'],
                    'executable': str(binary.resolve()),
                    'registry_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'registration_status': record.get('status'),
                    'last_verified_at': record.get('last_verified_at', ''),
                    'healthcheck_status': candidate['audit']['healthcheck_status'],
                    'source_ref': candidate['source_ref'],
                    'prefix_source': ('registry' if any(record.get(k) for k in
                        ('prefix', 'path', 'resolved_runtime_paths')) else 'existing_environment_inventory')}
    return None
