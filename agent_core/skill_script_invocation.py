"""Opt-in invocation contracts for registered, fixed Skill utilities.

This is a bounded simple-command checker, not a general shell interpreter.
It never executes a candidate, repairs its source, or selects a scientific tool.
"""
from copy import deepcopy
import os
from pathlib import Path
import re
import shlex
import sys

import yaml

from agent_core.skill_access import SkillAccessError


def _fail(code, detail, **diagnostic):
    error = SkillAccessError(code, detail, classification=(
        "INTERNAL_PROTOCOL" if code in {"SKILL_INVOCATION_CONTRACT_INVALID", "SKILL_INVOCATION_RUNTIME_UNAVAILABLE"}
        else "ACTION_FORMAT_REPAIR"))
    error.diagnostic = {
        "validation_source": "agent_core.skill_script_invocation.validate_skill_script_invocations",
        "repair_stage": "skill_script_invocation", "detail": detail,
        "owner": "GENERATED_ACTION", "check_status": "UNVERIFIABLE" if code == "SKILL_INVOCATION_UNVERIFIABLE" else "FAIL",
        **diagnostic,
    }
    raise error


def resolve_script_invocation(skill_text, relative_path, script_path):
    try:
        return _resolve_script_invocation(skill_text, relative_path, script_path)
    except SkillAccessError as exc:
        exc.diagnostic = {**getattr(exc, "diagnostic", {}),
            "validation_source": "agent_core.skill_script_invocation.resolve_script_invocation",
            "repair_stage": "skill_script_invocation", "owner": "SKILL_CONTRACT",
            "field_path": "generate.script_invocations." + str(relative_path),
            "helper_relative_path": relative_path, "detail": exc.detail}
        raise


def _resolve_script_invocation(skill_text, relative_path, script_path):
    """Resolve only an explicit declaration from the pinned Skill document."""
    frontmatter = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", skill_text, re.DOTALL)
    if frontmatter is None:
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", "Pinned Skill has no valid frontmatter")
    try:
        meta = yaml.safe_load(frontmatter[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillAccessError("SKILL_INVOCATION_CONTRACT_INVALID", "Invalid Skill frontmatter") from exc
    if not isinstance(meta, dict):
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", "Skill frontmatter must be a mapping")
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    generate = meta.get("generate", metadata.get("generate")) or {}
    if not isinstance(generate, dict):
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", "Skill generate must be a mapping")
    declarations = generate.get("script_invocations", {})
    if not isinstance(declarations, dict):
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", "generate.script_invocations must be a mapping")
    if relative_path not in declarations:
        return None
    declaration = declarations[relative_path]
    if (not isinstance(declaration, dict)
            or set(declaration) - {"schema_version", "runtime", "arguments", "delegates"}
            or type(declaration.get("schema_version")) is not int
            or declaration["schema_version"] != 1
            or declaration.get("runtime") != "controller_python"):
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", f"{relative_path}: expected v1 controller_python utility contract")
    arguments = declaration.get("arguments")
    if not isinstance(arguments, dict) or not arguments:
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", f"{relative_path}: explicit argument declarations required")
    for name, spec in arguments.items():
        if (not isinstance(name, str) or not re.fullmatch(r"--[a-z][a-z0-9-]*", name)
                or not isinstance(spec, dict)
                or set(spec) - {"nargs", "required", "repeatable", "runtime_environment_variable"}
                or type(spec.get("nargs")) is not int or not 1 <= spec["nargs"] <= 8
                or any(key in spec and type(spec[key]) is not bool for key in ("required", "repeatable"))):
            _fail("SKILL_INVOCATION_CONTRACT_INVALID", f"{relative_path}: invalid argument declaration {name}")
        runtime_variable = spec.get("runtime_environment_variable")
        if runtime_variable is not None and (spec["nargs"] != 1 or not isinstance(runtime_variable, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", runtime_variable)):
            _fail("SKILL_INVOCATION_CONTRACT_INVALID", f"{relative_path}: invalid runtime argument {name}")
    delegates = declaration.get("delegates", [])
    if (not isinstance(delegates, list) or any(not isinstance(p, str) or not p.startswith("scripts/")
            or ".." in Path(p).parts or Path(p).is_absolute() for p in delegates)
            or len(set(delegates)) != len(delegates) or relative_path in delegates):
        _fail("SKILL_INVOCATION_CONTRACT_INVALID", f"{relative_path}: invalid delegated resources")
    # Preserve the venv launch path: resolving a Python symlink can select the
    # base interpreter instead of the Controller's installed environment.
    interpreter = os.path.abspath(sys.executable)
    if not Path(interpreter).is_file() or not os.access(interpreter, os.X_OK):
        _fail("SKILL_INVOCATION_RUNTIME_UNAVAILABLE", "Controller Python is not executable")
    info = os.stat(interpreter)
    prefix = [interpreter, str(script_path)]
    return {"schema_version": 1, "runtime": "controller_python", "interpreter": interpreter,
            "interpreter_identity": {"real_path": os.path.realpath(interpreter), "device": info.st_dev,
                                     "inode": info.st_ino, "size": info.st_size,
                                     "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns},
            "argv_prefix": prefix, "shell_prefix": shlex.join(prefix),
            "arguments": deepcopy(arguments), "delegates": list(delegates),
            "carrier": ("simple_shell" if any(spec.get("runtime_environment_variable") for spec in arguments.values())
                        else "simple_shell_or_direct_argv"),
            "scope": "registered utility; does not activate the Skill scientific environment"}


def _validate_arguments(invocation, arguments):
    specs = invocation["arguments"]
    seen, issues = {}, []
    position = 0
    def issue(detail, **fields):
        if len(issues) < 32:
            issues.append({"detail": detail, **fields})
    while position < len(arguments):
        option = arguments[position]
        spec = specs.get(option)
        if spec is None:
            issue(f"Unknown or positional argument {option!r}; use declared separate option/value tokens",
                  argument_index=position, actual=option, expected=list(specs))
            position += 1
            continue
        seen[option] = seen.get(option, 0) + 1
        if seen[option] > 1 and not spec.get("repeatable", False):
            issue(f"{option} is not repeatable", argument_index=position,
                  actual=seen[option], expected=1, argument_name=option)
        values = arguments[position + 1:position + 1 + spec["nargs"]]
        if len(values) != spec["nargs"] or any(not value or value.startswith("--") for value in values):
            issue(f"{option} needs {spec['nargs']} separate nonempty values", argument_index=position,
                  actual=values, expected={"nargs": spec["nargs"], "nonempty_separate_values": True}, argument_name=option)
            position += 1
        else:
            position += 1 + spec["nargs"]
    missing = [name for name, spec in specs.items() if spec.get("required", False) and name not in seen]
    if missing:
        issue("Missing required arguments: " + ", ".join(missing), actual=list(seen), expected=missing)
    if issues:
        _fail("SKILL_INVOCATION_ARGUMENT_INVALID", "; ".join(row["detail"] for row in issues),
              **{key: value for key, value in issues[0].items() if key != "detail"}, argument_issues=issues)


def build_skill_script_argv(resource, arguments):
    """Construct reviewable argv from a bound utility and explicit arguments."""
    invocation = resource.get("invocation")
    if not invocation:
        _fail("SKILL_INVOCATION_CONTRACT_REQUIRED", str(resource.get("handle", "")))
    if (not isinstance(arguments, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in arguments)):
        _fail("SKILL_INVOCATION_ARGUMENT_INVALID", "arguments must be a list of NUL-free strings")
    _validate_arguments(invocation, arguments)
    if any(spec.get("runtime_environment_variable") for spec in invocation["arguments"].values()):
        _fail("SKILL_INVOCATION_RUNTIME_ARGUMENT_DEFERRED",
              "This utility needs a runtime argument after approval; use its shell_prefix and quoted runtime variable")
    return [*invocation["argv_prefix"], *arguments]


def build_skill_script_shell(resource, arguments):
    """Serialize explicit arguments; only declared late bindings expand in Bash."""
    invocation = resource.get("invocation") or {}
    specs = invocation.get("arguments") or {}
    if not isinstance(arguments, list):
        _fail("SKILL_INVOCATION_ARGUMENT_INVALID", "arguments must be an array")
    flattened, deferred = [], {}
    for index, value in enumerate(arguments):
        if isinstance(value, str) and "\x00" not in value:
            flattened.append(value)
        elif (isinstance(value, dict) and set(value) == {"runtime_binding"}
              and isinstance(value["runtime_binding"], str)):
            deferred[index] = value["runtime_binding"]
            flattened.append("$" + value["runtime_binding"])
        else:
            _fail("SKILL_INVOCATION_ARGUMENT_INVALID", f"Invalid arguments[{index}]", field_path=f"arguments[{index}]")
    if not invocation:
        _fail("SKILL_INVOCATION_CONTRACT_REQUIRED", str(resource.get("handle", "")))
    _validate_arguments(invocation, flattened)
    position = 0
    expected_deferred = {}
    while position < len(flattened):
        spec = specs[flattened[position]]
        binding = spec.get("runtime_environment_variable")
        if binding:
            for index in range(position + 1, position + 1 + spec["nargs"]):
                expected_deferred[index] = binding
        position += 1 + spec["nargs"]
    if deferred != expected_deferred:
        _fail("SKILL_INVOCATION_ARGUMENT_INVALID", "Use the exact declared runtime_binding object for late-bound arguments only",
              actual=deferred, expected=expected_deferred, field_path="arguments")
    if not expected_deferred:
        return shlex.join(build_skill_script_argv(resource, flattened))
    tokens = [shlex.quote(value) for value in invocation["argv_prefix"]]
    tokens.extend(('"$' + deferred[index] + '"') if index in deferred else shlex.quote(value)
                  for index, value in enumerate(flattened))
    return " ".join(tokens)


def _simple_commands(source, environment, deferred):
    """Read literal words and quoted bound variables, retaining shell quoting.

    Only newline-separated simple commands are admitted. Unsupported syntax
    returns an explicit incomplete check rather than guessed shell semantics.
    Each word carries unresolved runtime variables so literals cannot impersonate
    a variable that the execution controller will materialize after approval.
    """
    if len(source) > 65536 or "\x00" in source or "\r" in source:
        _fail("SKILL_INVOCATION_UNVERIFIABLE", "Utility carrier exceeds the simple-shell limits")
    commands, command, value, variables = [], [], [], set()
    quote = ""
    started = False
    index = 0

    def word():
        nonlocal started, value, variables
        if started:
            command.append(("".join(value), frozenset(variables)))
        started, value, variables = False, [], set()

    def end_command():
        word()
        if command:
            commands.append(list(command))
            command.clear()

    while index < len(source):
        char = source[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                value.append(char)
            index += 1
            continue
        if char == "\\":
            if index + 1 == len(source):
                _fail("SKILL_INVOCATION_UNVERIFIABLE", "Trailing shell escape")
            following = source[index + 1]
            if following == "\n":
                index += 2
                continue
            started = True
            if quote == '"' and following not in '$`"\\':
                value.append("\\")
            value.append(following)
            index += 2
            continue
        if char in "\"'" and (not quote or quote == char):
            started = True
            quote = "" if quote else char
            index += 1
            continue
        if char == "$":
            if quote != '"':
                _fail("SKILL_INVOCATION_UNVERIFIABLE", "Use double quotes around bound environment variables")
            match = re.match(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", source[index:])
            if not match:
                _fail("SKILL_INVOCATION_UNVERIFIABLE", "Dynamic shell expansion is not a utility invocation")
            name = match[1] or match[2]
            if name in deferred:
                value.append("${" + name + "}")
                variables.add(name)
            elif name in environment:
                value.append(str(environment[name]))
            else:
                _fail("SKILL_INVOCATION_UNVERIFIABLE", f"Environment variable {name} has no approved binding")
            started = True
            index += len(match[0])
            continue
        if char == "`" or (not quote and char in ";&|()<>{}*?[]~"):
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "Use a direct utility argv or simple command; compound shell is not verified")
        if not quote and char == "#" and not started:
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline
            continue
        if not quote and char in " \t\n":
            if char == "\n":
                end_command()
            else:
                word()
        else:
            started = True
            value.append(char)
        index += 1
    if quote:
        _fail("SKILL_INVOCATION_UNVERIFIABLE", "Unterminated shell quote")
    end_command()
    return commands


def validate_skill_script_invocations(action, resources, *, enforce=True):
    """Optionally enforce the utility's static invocation contract.

    The caller owns the candidate-validation policy. Disabling this check leaves
    the candidate untouched; selected-resource binding, snapshot identity and
    file availability remain the responsibility of the resource-binding layer.
    Direct callers retain strict validation unless they explicitly opt out.
    """
    if not enforce:
        return
    context = {}
    try:
        return _validate_skill_script_invocations(action, resources, context)
    except SkillAccessError as exc:
        diagnostic = dict(getattr(exc, "diagnostic", {}))
        exc.diagnostic = {**context, **diagnostic}
        index = diagnostic.get("argument_index")
        if index is not None:
            source = context.get("field_path", "argv")
            exc.diagnostic["field_path"] = (f"argv[{index + 2}]" if source == "argv" else source)
        if exc.code == "SKILL_INVOCATION_UNVERIFIABLE":
            exc.diagnostic["supported_carriers"] = ["direct utility argv when no deferred argument", "newline-separated direct simple-shell calls with quoted bound variables"]
        raise


def _validate_skill_script_invocations(action, resources, context):
    """Validate opt-in utilities before approval, and again without rewriting."""
    contracted = [row for row in resources if row.get("invocation")]
    if not contracted or action.get("type") != "execute":
        return
    from agent_core.step_effects import executable_carrier
    selected = executable_carrier(action)
    source = selected["source"] if selected["script_available"] else "argv"
    context.update(carrier={"source": selected["source"], "language": selected["language"],
                            "script_available": selected["script_available"]},
                   field_path=source, helper_handles=[row["handle"] for row in contracted],
                   actual=selected["body"] if selected["script_available"] else action.get("argv"),
                   expected="declared interpreter + helper + separate option/value tokens",
                   evidence=[{"handle": row["handle"], "snapshot_id": row.get("snapshot_id"),
                              "resource_snapshot_id": row.get("resource_snapshot_id")} for row in contracted])
    deferred = {spec["runtime_environment_variable"] for row in contracted
                for spec in row["invocation"]["arguments"].values() if spec.get("runtime_environment_variable")}
    if selected["script_available"]:
        if selected["language"] not in {"bash", "sh", "shell"}:
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "This utility declares simple shell or direct argv; use its published argv_prefix")
        commands = _simple_commands(selected["body"], action.get("env", {}), deferred)
    else:
        argv = action.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "A direct utility argv or simple shell command is required")
        commands = [[(arg, frozenset()) for arg in argv]]
    invoked = 0
    handles = {row["handle"] for row in resources}
    for command in commands:
        words = [item[0] for item in command]
        if words in (["set", "-e"], ["set", "-eu"], ["set", "-euo", "pipefail"], ["set", "-o", "pipefail"]):
            continue
        if words and words[0] == "exec":
            words, command = words[1:], command[1:]
        if not words:
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "Empty invocation")
        direct_source = next((row for row in contracted if words[0] == row["canonical_path"]), None)
        if direct_source:
            _fail("SKILL_INVOCATION_INTERPRETER_REQUIRED", "Skill source files are not executables; use the published interpreter + script argv_prefix",
                  helper_handle=direct_source["handle"], actual=words[0], expected=direct_source["invocation"]["argv_prefix"],
                  field_path="argv[0]" if source == "argv" else source)
        row = next((row for row in contracted if len(words) >= 2 and words[1] == row["canonical_path"]), None)
        if row is None:
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "Only declared direct utility calls and optional set/exec are verified; use the published shell_prefix")
        invocation = row["invocation"]
        context.update(helper_handle=row["handle"], actual=words, expected=invocation["argv_prefix"])
        if words[0] != invocation["interpreter"]:
            _fail("SKILL_INVOCATION_INTERPRETER_REQUIRED", f"Use the bound Controller interpreter: {invocation['interpreter']}",
                  actual=words[0], expected=invocation["interpreter"], field_path="argv[0]" if source == "argv" else source)
        if any(item[1] for item in command[:2]):
            _fail("SKILL_INVOCATION_UNVERIFIABLE", "The interpreter and script must be bound before approval")
        if not selected["script_available"] and not any(
                spec.get("runtime_environment_variable") for spec in invocation["arguments"].values()):
            # The existing public action already supplies argv. Use the builder
            # to verify that exact argv; never rewrite an approved carrier.
            expected_argv = build_skill_script_argv(row, words[2:])
            if words != expected_argv:
                _fail("SKILL_INVOCATION_ARGUMENT_INVALID", "Direct argv differs from the bound invocation",
                      actual=words, expected=expected_argv)
        else:
            _validate_arguments(invocation, words[2:])
        offset = 2
        while offset < len(words):
            spec = invocation["arguments"][words[offset]]
            expected = spec.get("runtime_environment_variable")
            values = command[offset + 1:offset + 1 + spec["nargs"]]
            if expected:
                if values != [("${" + expected + "}", frozenset({expected}))]:
                    _fail("SKILL_INVOCATION_ARGUMENT_INVALID", f"{words[offset]} requires the quoted runtime binding ${expected}",
                          actual=[item[0] for item in values], expected={"quoted_runtime_variable": expected}, argument_name=words[offset])
            elif any(item[1] for item in values):
                _fail("SKILL_INVOCATION_ARGUMENT_INVALID", f"{words[offset]} cannot consume an unresolved runtime binding",
                      actual=[item[0] for item in values], expected="fully bound literal values", argument_name=words[offset])
            offset += 1 + spec["nargs"]
        required = {row["skill_id"] + "#" + path for path in invocation["delegates"]}
        if not required <= handles:
            _fail("SKILL_INVOCATION_DEPENDENCY_UNBOUND", "Declare delegated resources: " + ", ".join(sorted(required - handles)))
        invoked += 1
    if not invoked:
        _fail("SKILL_INVOCATION_MISSING", "No declared utility invocation was found")
