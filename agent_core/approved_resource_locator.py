"""Thin, fail-closed transport for already approved managed resource rows.

Identity and visibility remain owned by managed_artifacts. No model arguments
or process CWD supply resource authority. Reconstructed from durable TaskSpec.
"""
from pathlib import Path
from agent_core.managed_artifacts import _canonical_path


class ResourceLocatorError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def approved_resource_locator(row, *, trusted_root=""):
    fields = {key: str(row[key]).strip() for key in
              ("canonical_path", "resolved_path", "path") if row.get(key)}
    if not fields:
        raise ResourceLocatorError("ARTIFACT_LOCATOR_MISSING")
    paths = []
    for raw in fields.values():
        path = Path(raw)
        if ".." in path.parts or str(path).startswith("~"):
            raise ResourceLocatorError("ARTIFACT_PATH_DENIED")
        if not path.is_absolute():
            root = Path(trusted_root)
            if not trusted_root or not root.is_absolute():
                raise ResourceLocatorError("ARTIFACT_TRUSTED_ROOT_MISSING")
            path = root / path
        # Do not follow links, including links swapped after publication.
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ResourceLocatorError("ARTIFACT_SYMLINK_DENIED")
        paths.append(_canonical_path({"canonical_path": str(path)}))
    if len(set(paths)) != 1:
        raise ResourceLocatorError("ARTIFACT_PATH_CONFLICT")
    return {"path": paths[0], "source_fields": sorted(fields),
            "canonical_resource_id": str(row.get("canonical_resource_id") or row.get("artifact_id") or ""),
            "revision": row.get("revision", row.get("resource_revision")),
            "declared_sha256": str(row.get("sha256") or "")}


from contextlib import contextmanager
import os

@contextmanager
def open_approved_resource(path):
    """Open relative to no-follow directory descriptors to close symlink races."""
    checked = approved_resource_locator({"canonical_path": str(path)})
    parts = Path(checked["path"]).parts
    directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        for name in parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        import stat
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ResourceLocatorError("ARTIFACT_FORMAT_UNSUPPORTED")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            yield handle
    finally:
        if descriptor is not None: os.close(descriptor)
        os.close(directory)
