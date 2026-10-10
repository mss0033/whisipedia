"""Guard that keeps databases and indexes on local disk (ADR-0002).

SMB/NFS mounts give unreliable byte-range locking and ``soft`` mounts can surface timeouts as
partial writes, so SQLite, Tantivy and other mutable stores refuse to open on them.
"""

from __future__ import annotations

from pathlib import Path

REMOTE_FS_TYPES = frozenset(
    {"cifs", "smb3", "smbfs", "nfs", "nfs4", "afs", "ceph", "glusterfs", "fuse.sshfs", "9p", "drvfs"}
)

_MOUNTS = Path("/proc/mounts")


class RemoteFilesystemError(RuntimeError):
    """Raised when a mutable store would be placed on a network filesystem."""


def _unescape(field: str) -> str:
    # /proc/mounts escapes space, tab, newline and backslash as octal.
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def filesystem_type(path: Path, mounts_file: Path = _MOUNTS) -> str | None:
    """Filesystem type of the mount containing ``path``; ``None`` if unknown (e.g. not Linux)."""
    try:
        lines = mounts_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    target = path.resolve()
    best: tuple[int, str] | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = Path(_unescape(parts[1]))
        if target == mount_point or mount_point in target.parents:
            depth = len(mount_point.parts)
            if best is None or depth >= best[0]:
                best = (depth, parts[2])
    return None if best is None else best[1]


def ensure_local(path: Path, *, mounts_file: Path = _MOUNTS) -> Path:
    """Return ``path`` (created if missing) or raise if it lives on a network filesystem."""
    path.mkdir(parents=True, exist_ok=True)
    fstype = filesystem_type(path, mounts_file)
    if fstype is not None and fstype.lower() in REMOTE_FS_TYPES:
        raise RemoteFilesystemError(
            f"{path} is on a {fstype} mount; indexes and databases must live on local disk (ADR-0002)"
        )
    return path
