"""Descriptor-bound, no-follow reads beneath one identity-bound vault root."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import stat


_REQUIRED = ("O_DIRECTORY", "O_NOFOLLOW")
_METADATA_FIELDS = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


class VaultPolicyError(OSError):
    """A deterministic path/type policy exclusion, not a transient read failure."""


class VaultOversizeError(OSError):
    """A regular source exceeded the configured byte bound."""


class VaultInventoryOverflow(OSError):
    """Descriptor-bound enumeration exceeded its configured entry bound."""


@dataclass(frozen=True, order=True)
class InventoryEntry:
    path: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, path: str, info: os.stat_result) -> "InventoryEntry":
        mode = info.st_mode
        if stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISREG(mode):
            kind = "regular"
        elif stat.S_ISLNK(mode):
            kind = "symlink"
        else:
            kind = "nonregular"
        return cls(path, kind, info.st_dev, info.st_ino, mode, info.st_size,
                   info.st_mtime_ns, info.st_ctime_ns)

    def same_open_file(self, info: os.stat_result) -> bool:
        return all(getattr(info, field) == value for field, value in zip(
            _METADATA_FIELDS,
            (self.device, self.inode, self.mode, self.size, self.mtime_ns, self.ctime_ns),
        ))


def _supported() -> bool:
    return (all(hasattr(os, name) for name in _REQUIRED)
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd)


def _parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path or relative_path.startswith("/"):
        raise OSError("unsafe vault-relative path")
    parts = PurePosixPath(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("unsafe vault-relative path")
    return parts


def _open_absolute_directory(root: str | Path) -> int:
    """Traverse an absolute root from `/`, rejecting every unsafe ancestor."""
    if not _supported():
        raise OSError("descriptor-based no-follow traversal is unsupported")
    path = Path(root)
    if not path.is_absolute():
        raise OSError("vault root must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise OSError("vault root contains an unsafe component")
            try:
                child = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise OSError("vault root has a symlink or non-directory ancestor") from exc
                raise
            os.close(current)
            current = child
        info = os.fstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("vault root is not a trusted directory")
        return current
    except Exception:
        os.close(current)
        raise


def bind_vault_root(root: str | Path) -> tuple[int, int]:
    """Return the current descriptor-bound root identity, failing closed if unsupported."""
    fd = _open_absolute_directory(root)
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


class TrustedVault:
    """Hold an approved root descriptor and traverse only no-follow descendants."""

    def __init__(self, root: str | Path, identity: tuple[int, int] | None = None):
        self.root = Path(root)
        self.identity = identity
        self.fd: int | None = None

    def __enter__(self) -> "TrustedVault":
        self.fd = _open_absolute_directory(self.root)
        info = os.fstat(self.fd)
        if self.identity is not None and (info.st_dev, info.st_ino) != self.identity:
            self.close()
            raise OSError("configured vault root identity changed")
        return self

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *_args) -> None:
        self.close()

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        if self.fd is None:
            raise OSError("vault is not open")
        current = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=current)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise VaultPolicyError("vault path contains a symlink or non-directory") from exc
                    raise
                os.close(current)
                current = child
            return current
        except Exception:
            os.close(current)
            raise

    def read(self, relative_path: str, maximum_bytes: int) -> tuple[bytes, os.stat_result]:
        parts = _parts(relative_path)
        parent = self._open_parent(parts)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise VaultPolicyError("vault entry is a symlink or unsafe path") from exc
                raise
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise VaultPolicyError("vault entry is not a regular file")
            if info.st_size > maximum_bytes:
                raise VaultOversizeError("vault entry exceeds size limit")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                block = os.read(fd, min(131072, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
            if len(raw) > maximum_bytes:
                raise VaultOversizeError("vault entry exceeds size limit")
            after = os.fstat(fd)
            if InventoryEntry.from_stat(relative_path, info) != InventoryEntry.from_stat(relative_path, after):
                raise OSError("vault entry changed during read")
            return raw, after
        finally:
            os.close(fd)

    def inventory(self, maximum_entries: int) -> tuple[InventoryEntry, ...]:
        """Lazily enumerate within a global bound, then sort only bounded entries."""
        if self.fd is None:
            raise OSError("vault is not open")
        if maximum_entries < 1:
            raise VaultInventoryOverflow("vault inventory limit exceeded")
        output = [InventoryEntry.from_stat("", os.fstat(self.fd))]
        enumerated = 0

        def append(entry: InventoryEntry) -> None:
            output.append(entry)
            if len(output) - 1 > maximum_entries:
                raise VaultInventoryOverflow("vault inventory limit exceeded")

        def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
            nonlocal enumerated
            bounded: list[tuple[str, os.stat_result]] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    enumerated += 1
                    if enumerated > maximum_entries:
                        raise VaultInventoryOverflow("vault inventory limit exceeded")
                    bounded.append((entry.name, entry.stat(follow_symlinks=False)))
            for name, info in sorted(bounded, key=lambda item: item[0]):
                rel_parts = prefix + (name,)
                relative = PurePosixPath(*rel_parts).as_posix()
                item = InventoryEntry.from_stat(relative, info)
                append(item)
                if item.kind != "directory":
                    continue
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise OSError("vault directory changed during inventory") from exc
                    raise
                try:
                    if not item.same_open_file(os.fstat(child)):
                        raise OSError("vault directory changed during inventory")
                    walk(child, rel_parts)
                    if not item.same_open_file(os.fstat(child)):
                        raise OSError("vault directory changed during inventory")
                finally:
                    os.close(child)

        walk(self.fd, ())
        root_after = InventoryEntry.from_stat("", os.fstat(self.fd))
        if output[0] != root_after:
            raise OSError("vault root changed during inventory")
        return tuple(sorted(output))

    def markdown_paths(self, maximum_files: int | None = None) -> list[str]:
        """Compatibility helper backed by the descriptor-bound full inventory."""
        limit = maximum_files if maximum_files is not None else 1_000_000
        inventory = self.inventory(limit)
        paths = [item.path for item in inventory
                 if item.path.endswith(".md") and item.kind == "regular"]
        return paths[:maximum_files] if maximum_files is not None else paths
