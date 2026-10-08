"""Lightweight project boundary shared by the launcher and scientific backend."""
from __future__ import annotations

from pathlib import Path


class ProjectDirectoryError(ValueError):
    code = "PROJECT_DIRECTORY_INVALID"


def validate_project_directory(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ProjectDirectoryError(f"Not a directory: {path}")
    except OSError as error:
        raise ProjectDirectoryError(f"Project directory is unavailable: {value} ({error.strerror})") from error
    if path in {Path('/'), Path('/home'), Path('/public'), Path('/public/home'),
                Path('/tmp'), Path('/etc'), Path('/proc'), Path('/root'), Path.home().resolve()}:
        raise ProjectDirectoryError(f"Choose a project directory, not a shared/home root: {path}")
    return path
