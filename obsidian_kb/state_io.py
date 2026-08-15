"""Descriptor-bound access to private SQLite state outside the vault."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat


_REQUIRED = ("O_DIRECTORY", "O_NOFOLLOW")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _absolute_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        raise OSError("state path must be absolute")
    return tuple(part for part in path.parts if part != path.anchor)


def _open_directory(path: Path, *, create: bool) -> int:
    """Traverse an absolute directory from / without following any symlink."""
    current = os.open("/", _DIR_FLAGS)
    try:
        for part in _absolute_parts(path):
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=current)
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise OSError("state path contains a symlink or non-directory ancestor") from exc
                raise
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise OSError("state path contains a non-directory ancestor")
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _is_at_or_beneath(directory_fd: int, ancestor: tuple[int, int]) -> bool:
    current = os.dup(directory_fd)
    try:
        while True:
            here = _identity(os.fstat(current))
            if here == ancestor:
                return True
            parent = os.open("..", _DIR_FLAGS, dir_fd=current)
            parent_identity = _identity(os.fstat(parent))
            os.close(current)
            current = parent
            if parent_identity == here:
                return False
    finally:
        os.close(current)


class TrustedStateDirectory:
    """Hold a stable state-directory fd and bind every operation to it."""

    def __init__(self, state_path: str | Path, vault_path: str | Path, *, create: bool):
        if not all(hasattr(os, name) for name in _REQUIRED) or os.open not in os.supports_dir_fd:
            raise OSError("descriptor-based state traversal is unsupported")
        if os.name != "posix" or not Path("/proc/self/fd").is_dir():
            raise OSError("stable descriptor SQLite paths are unavailable")
        self.path = Path(state_path)
        self.parent_path = self.path.parent
        self.name = self.path.name
        self.vault_path = Path(vault_path)
        if not self.name or self.name in {".", ".."}:
            raise OSError("invalid SQLite state filename")
        self.fd = _open_directory(self.parent_path, create=create)
        self.identity = _identity(os.fstat(self.fd))
        try:
            self.assert_boundary()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "TrustedStateDirectory":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def assert_boundary(self) -> None:
        if self.fd is None:
            raise OSError("state directory is closed")
        info = os.fstat(self.fd)
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != self.identity:
            raise OSError("trusted state directory identity changed")
        current = _open_directory(self.parent_path, create=False)
        try:
            if _identity(os.fstat(current)) != self.identity:
                raise OSError("configured state directory was replaced")
        finally:
            os.close(current)
        vault = _open_directory(self.vault_path, create=False)
        try:
            if _is_at_or_beneath(self.fd, _identity(os.fstat(vault))):
                raise OSError("state database and lock must remain outside the vault")
        finally:
            os.close(vault)
        proc_dir = Path(f"/proc/self/fd/{self.fd}")
        if _identity(proc_dir.stat()) != self.identity:
            raise OSError("stable descriptor path identity mismatch")

    def proc_path(self, suffix: str = "") -> str:
        self.assert_boundary()
        return f"/proc/self/fd/{self.fd}/{self.name}{suffix}"

    def open_regular(self, suffix: str = "", *, read_only: bool = False, append: bool = False) -> int:
        self.assert_boundary()
        flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
        if append:
            flags |= os.O_APPEND
        flags |= os.O_NOFOLLOW
        fd = os.open(self.name + suffix, flags, 0o600, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("state entry is not a regular file")
            if not read_only:
                os.fchmod(fd, 0o600)
            return fd
        except Exception:
            os.close(fd)
            raise

    def same_entry(self, opened_fd: int, suffix: str = "") -> bool:
        if self.fd is None:
            return False
        try:
            current = os.stat(self.name + suffix, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(current.st_mode) and _identity(current) == _identity(os.fstat(opened_fd))
