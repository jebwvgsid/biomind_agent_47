"""Credential-free selection shared by the product entry and offline acceptance.

Never imports legacy settings or clients. Product launches may project three
nonsecret fields from the original two .env locations; offline isolation disables
that projection before any file operation or optional dotenv import.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping
from urllib.parse import urlsplit

FIELDS = {'schema', 'provider', 'model', 'api', 'thinking', 'endpoint_hostname', 'origin', 'confirmed_active'}
LAUNCH_FIELDS = ('LLM_DEFAULT_MODEL', 'LLM_BASE_URL', 'BIO_SOFTWARE_ROOT')


def project_launch_environment(environment: Mapping[str, str], *, repo, project):
    """Match legacy repo-then-project precedence, parsing only allowlisted rows.

    Unselected assignments never enter dotenv_values, interpolation or returned
    metadata. Multiline values and references outside this allowlist fail closed.
    This never loads .env.api_keys and never mutates the caller's environment.
    """
    selected = {key: environment.get(key, '') for key in LAUNCH_FIELDS}
    sources = []
    if environment.get('BIOMIND_DISABLE_DOTENV', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        return selected, {'dotenv_projection': 'DISABLED', 'dotenv_sources': sources}
    for directory in dict.fromkeys((Path(repo).absolute(), Path(project).absolute())):
        path = directory/'.env'
        if path.is_symlink():
            raise ValueError('PRODUCT_DOTENV_SYMLINK_FORBIDDEN')
        if not path.exists():
            continue
        if path.resolve(strict=True) != path:
            raise ValueError('PRODUCT_DOTENV_SYMLINK_FORBIDDEN')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError('BOUNDED_REGULAR_PRODUCT_DOTENV_REQUIRED')
            with os.fdopen(descriptor, 'r', encoding='utf-8', closefd=False) as stream:
                rows = [line for line in stream if re.match(r'^\s*(?:export\s+)?(?:LLM_DEFAULT_MODEL|LLM_BASE_URL|BIO_SOFTWARE_ROOT)\s*=', line)]
        finally:
            os.close(descriptor)
        # Import is delayed until an authorized original .env actually exists.
        from dotenv import dotenv_values
        values = {}
        for line in rows:
            parsed = dotenv_values(stream=io.StringIO(line), interpolate=False)
            if not parsed:
                raise ValueError('MULTILINE_OR_INVALID_NONSECRET_DOTENV_VALUE')
            for key, value in parsed.items():
                if key not in LAUNCH_FIELDS or value is None:
                    raise ValueError('INVALID_NONSECRET_DOTENV_ASSIGNMENT')
                def expand(match):
                    name = match.group(1)
                    if name not in LAUNCH_FIELDS:
                        raise ValueError('NONSECRET_DOTENV_INTERPOLATION_OUTSIDE_ALLOWLIST')
                    return values.get(name, selected.get(name, ''))
                value = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', expand, value)
                if '${' in value:
                    raise ValueError('UNSUPPORTED_NONSECRET_DOTENV_INTERPOLATION')
                values[key] = value
                if value != '' or not selected.get(key):
                    selected[key] = value
        sources.append({'path': str(path), 'fields': sorted(values),
                        'projection_sha256': hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()})
    root = selected.get('BIO_SOFTWARE_ROOT', '')
    if root and (not Path(root).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in root)):
        raise ValueError('ABSOLUTE_NONSECRET_SOFTWARE_ROOT_REQUIRED')
    return selected, {'dotenv_projection': 'READ_ALLOWLIST_ONLY', 'dotenv_sources': sources}


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('EXACT_NONSECRET_CONFIGURATION_FIELDS_REQUIRED')
    v2 = value.get('schema') == 'biomind-active-model-config-v2'
    if set(value) != FIELDS | ({'endpoint_base_url'} if v2 else set()):
        raise ValueError('EXACT_NONSECRET_CONFIGURATION_FIELDS_REQUIRED')
    if value['schema'] not in {'biomind-active-model-config-v1', 'biomind-active-model-config-v2'} or value['confirmed_active'] is not True:
        raise ValueError('ACTIVE_PRODUCT_CONFIGURATION_NOT_CONFIRMED')
    if value['origin'] not in {'user-confirmed-active-product-config', 'active-launch-environment'}:
        raise ValueError('CONFIGURATION_ORIGIN_NOT_ACCEPTED')
    for key in ('provider', 'model'):
        identifier = value[key]
        if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}', identifier)
                or re.search(r'(?i)(?:^|[/+])(?:sk[-_]|key[-_]|token[-_]|bearer[-_]|api[-_]?key)', identifier)
                or re.search(r'[A-Za-z0-9]{40,}', identifier)):
            raise ValueError('INVALID_NONSECRET_IDENTIFIER')
    if value['api'] != 'openai-completions':
        raise ValueError('UNIMPLEMENTED_CURRENT_API_FAMILY_NO_SUBSTITUTION')
    if value['thinking'] not in {'off', 'minimal', 'low', 'medium', 'high'}:
        raise ValueError('EXPLICIT_CURRENT_THINKING_REQUIRED')
    host = value['endpoint_hostname']
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host) or '..' in host:
        raise ValueError('HOSTNAME_ONLY_NO_URL_OR_CREDENTIALS')
    if v2:
        try:
            base = value['endpoint_base_url']
            parsed = (urlsplit(base) if isinstance(base, str) and len(base) <= 2048
                      and re.fullmatch(r'https://[A-Za-z0-9.-]+(?::443)?(?:/[A-Za-z0-9._~/-]*)?', base) else None)
            valid = (parsed and parsed.scheme == 'https' and parsed.hostname == host
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.port in (None, 443) and re.fullmatch(r'/[A-Za-z0-9._~/-]*', parsed.path or '/'))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('EXACT_NONSECRET_HTTPS_ENDPOINT_REQUIRED')
    return dict(value)


def read_config_record(path):
    """Read a bounded regular noncredential record without following symlinks."""
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    for candidate in (path, resolved):
        if (candidate.name.lower().startswith('.env')
                or re.fullmatch(r'(?:auth|credentials?|api[-_]?keys?)\.json', candidate.name, re.I)
                or candidate.name.lower() in {'.netrc', '.npmrc'}):
            raise ValueError('CREDENTIAL_FILES_FORBIDDEN')
    if resolved != path or path.is_symlink():
        raise ValueError('CONFIGURATION_RECORD_SYMLINK_FORBIDDEN')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError('BOUNDED_REGULAR_CONFIGURATION_RECORD_REQUIRED')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('BOUNDED_REGULAR_CONFIGURATION_RECORD_REQUIRED')
        return validate_config(json.loads(raw)), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def launch_environment_config(environment: Mapping[str, str]):
    """Read the actual process's existing BioMind launch selection, never files.

    The original shared DeepSeek client disables thinking; scoped legacy node
    overrides must not become the new conversation's global thinking policy.
    No provider is inferred from a model name or an unset legacy default.
    """
    model = environment.get('LLM_DEFAULT_MODEL', '').strip()
    endpoint = environment.get('LLM_BASE_URL', '').strip()
    if not model and not endpoint:
        return None
    if not model or not endpoint:
        raise ValueError('INCOMPLETE_ACTIVE_LAUNCH_CONFIGURATION_MODEL_AND_ENDPOINT_REQUIRED')
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == 'https' and url.hostname == 'api.deepseek.com'
                 and not url.username and not url.password and not url.query and not url.fragment
                 and url.port in (None, 443) and url.path.rstrip('/') in ('', '/v1'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('ACTIVE_ENDPOINT_REQUIRES_EXPLICIT_SUPPORTED_MODEL_RECORD_NO_SUBSTITUTION')
    return validate_config({'schema': 'biomind-active-model-config-v2', 'provider': 'deepseek',
        'model': model, 'api': 'openai-completions', 'thinking': 'off',
        'endpoint_hostname': 'api.deepseek.com', 'origin': 'active-launch-environment',
        'endpoint_base_url': endpoint,
        'confirmed_active': True})


def resolve_model_config(*, record_path=None, environment: Mapping[str, str] | None = None, repo=None, project=None):
    environment = os.environ if environment is None else environment
    effective, projection = (project_launch_environment(environment, repo=repo, project=project or Path.cwd())
                             if repo is not None else (environment, {}))
    shared = {**projection, **({'software_root': effective['BIO_SOFTWARE_ROOT']} if effective.get('BIO_SOFTWARE_ROOT') else {})}
    selected_path = record_path or environment.get('BIOMIND_PI_MODEL_RECORD')
    if selected_path:
        record, digest = read_config_record(selected_path)
        return record, {'kind': 'explicit-model-record', 'sha256': digest, **shared}
    record = launch_environment_config(effective)
    if record is None:
        return None, {'kind': 'not-present', 'missing_fields': ['LLM_DEFAULT_MODEL', 'LLM_BASE_URL'], **shared}
    encoded = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
    return record, {'kind': 'active-launch-environment', 'sha256': hashlib.sha256(encoded).hexdigest(),
                    'read_environment_fields': ['LLM_DEFAULT_MODEL', 'LLM_BASE_URL'],
                    'api_and_thinking_source': 'legacy-shared-openai-client-explicit-thinking-disabled', **shared}
