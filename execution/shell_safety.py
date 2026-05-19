"""Safety checks for commands that can break SSH/login shells.

These guards are intentionally conservative. BioMind-generated code should not
edit shell startup files, source user rc files, or put the terminal into raw /
no-echo mode during automated execution.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ShellSafetyViolation:
    code: str
    severity: str
    message: str
    evidence: str


SHELL_STARTUP_FILE_RE = re.compile(
    r"""(?ix)
    (?:
        (?:^|[\s'"`=:/])(?:~|\$HOME)?/
        (?:
            \.bashrc|\.bash_profile|\.bash_login|\.profile|\.zshrc|\.zprofile|
            \.zshenv|\.cshrc|\.tcshrc|\.config/fish/config\.fish
        )
        \b
        |
        /etc/(?:profile|bash\.bashrc|zsh/zprofile|zsh/zshrc|profile\.d/[^ \t\n;&|]+)
        |
        /etc/ssh/sshd_config\b
        |
        (?:^|[\s'"`=])(?:\.bashrc|\.bash_profile|\.bash_login|\.profile|\.zshrc|\.zprofile|\.zshenv)\b
    )
    """
)

SHELL_STARTUP_SOURCE_RE = re.compile(
    r"""(?ix)
    (?:^|[;&|]\s*|\n)\s*(?:source|\.)\s+
    (?:
        (?:~|\$HOME)?/
        (?:\.bashrc|\.bash_profile|\.bash_login|\.profile|\.zshrc|\.zprofile|\.zshenv)
        |
        /etc/(?:profile|bash\.bashrc|profile\.d/[^ \t\n;&|]+)
    )\b
    """
)

STARTUP_FILE_MUTATOR_RE = re.compile(
    r"""(?ix)
    (
        >>?|tee(?:\s+-a)?\b|sed\s+-i\b|perl\s+-pi\b|
        \b(?:rm|mv|cp|chmod|chown)\b|
        \bopen\s*\([^)]*['"][wa]\b|
        \bwrite_text\s*\(
    )
    """
)

UNSAFE_STTY_RE = re.compile(
    r"""(?ix)
    \bstty\s+
    (?:
        [^\n;&|]*\b(?:raw|cbreak)\b|
        [^\n;&|]*-(?:echo|icanon|isig)\b
    )
    """
)

INTERACTIVE_PYTHON_RE = re.compile(r"(?i)\b(?:input|getpass\.getpass)\s*\(")

INTERACTIVE_SHELL_RE = re.compile(
    r"""(?ix)
    (?:^|[;&|]\s*|\n)\s*
    (?:
        read\b|select\b|
        tmux\s+(?:attach|attach-session|new-session)\b|
        screen\s+-r\b
    )
    """
)


def _line_excerpt(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:300]


def analyze_shell_safety(code: str, language: str = "") -> List[ShellSafetyViolation]:
    text = str(code or "")
    violations: List[ShellSafetyViolation] = []

    source_match = SHELL_STARTUP_SOURCE_RE.search(text)
    if source_match:
        violations.append(
            ShellSafetyViolation(
                code="source_shell_startup",
                severity="critical",
                message="Do not source shell startup files from generated code; it can hang SSH login/session startup.",
                evidence=_line_excerpt(text, source_match),
            )
        )

    startup_match = SHELL_STARTUP_FILE_RE.search(text)
    if startup_match and STARTUP_FILE_MUTATOR_RE.search(text):
        violations.append(
            ShellSafetyViolation(
                code="mutates_shell_startup",
                severity="critical",
                message="Generated code must not mutate shell startup/login files such as .bashrc, .profile, or /etc/profile.",
                evidence=_line_excerpt(text, startup_match),
            )
        )

    stty_match = UNSAFE_STTY_RE.search(text)
    if stty_match:
        violations.append(
            ShellSafetyViolation(
                code="unsafe_terminal_mode",
                severity="critical",
                message="Unsafe stty mode can make terminal input invisible or unresponsive.",
                evidence=_line_excerpt(text, stty_match),
            )
        )

    if str(language).lower() in {"python", "py"} or INTERACTIVE_PYTHON_RE.search(text):
        input_match = INTERACTIVE_PYTHON_RE.search(text)
        if input_match:
            violations.append(
                ShellSafetyViolation(
                    code="interactive_python_input",
                    severity="high",
                    message="Generated Python code must not call input() or getpass(); it can block the agent.",
                    evidence=_line_excerpt(text, input_match),
                )
            )

    shell_match = INTERACTIVE_SHELL_RE.search(text)
    if shell_match:
        violations.append(
            ShellSafetyViolation(
                code="interactive_shell_command",
                severity="high",
                message="Generated shell code should not wait for foreground interactive input or attach to tmux/screen.",
                evidence=_line_excerpt(text, shell_match),
            )
        )

    return violations


def shell_safety_error(code: str, language: str = "") -> str:
    violations = analyze_shell_safety(code, language)
    if not violations:
        return ""
    if os.getenv("BIOMIND_ALLOW_SHELL_STARTUP_EDITS", "").lower() in {"1", "true", "yes", "on"}:
        return ""
    lines = [
        "BioMind shell-safety guard blocked this generated code.",
        "Reason: commands that mutate login shell startup files, source rc files, change terminal raw/no-echo mode, or wait for interactive input can make SSH sessions appear frozen.",
    ]
    for item in violations:
        lines.append(f"- {item.code}: {item.message} Evidence: {item.evidence}")
    lines.append("If you intentionally need this, edit the shell startup file manually outside the agent or set BIOMIND_ALLOW_SHELL_STARTUP_EDITS=1 for that one run.")
    return "\n".join(lines)
