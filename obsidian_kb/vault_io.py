"""Descriptor-based, no-follow reads beneath one trusted vault root."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat


_REQUIRED = ("O_DIRECTORY", "O_NOFOLLOW")


def _supported() -> bool:
    return all(hasattr(os, name) for name in _REQUIRED) and os.open in os.supports_dir_fd


def _parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path or relative_path.startswith("/"):
        raise OSError("unsafe vault-relative path")
    parts = PurePosixPath(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("unsafe vault-relative path")
    return parts


class TrustedVault:
    """Open a vault root once and traverse only via no-follow directory descriptors."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.fd: int | None = None

    def __enter__(self) -> "TrustedVault":
        if not _supported():
            raise OSError("descriptor-based no-follow traversal is unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.fd = os.open(self.root, flags)
        info = os.fstat(self.fd)
        if not stat.S_ISDIR(info.st_mode):
            self.close(); raise OSError("vault root is not a trusted directory")
        return self

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd); self.fd = None

    def __exit__(self, *_args) -> None:
        self.close()

    def _open_parent(self, parts: tuple[str, ...]) -> tuple[int, bool]:
        if self.fd is None: raise OSError("vault is not open")
        current = os.dup(self.fd); owned = True
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current); current = child
            return current, owned
        except Exception:
            os.close(current); raise

    def read(self, relative_path: str, maximum_bytes: int) -> tuple[bytes, os.stat_result]:
        parts = _parts(relative_path); parent, _ = self._open_parent(parts)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode): raise OSError("vault entry is not a regular file")
            if info.st_size > maximum_bytes: raise OSError("vault entry exceeds size limit")
            chunks: list[bytes] = []; remaining = maximum_bytes + 1
            while remaining:
                block = os.read(fd, min(131072, remaining))
                if not block: break
                chunks.append(block); remaining -= len(block)
            raw = b"".join(chunks)
            if len(raw) > maximum_bytes: raise OSError("vault entry exceeds size limit")
            after = os.fstat(fd)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise OSError("vault entry changed during read")
            return raw, after
        finally:
            os.close(fd)

    def markdown_paths(self, maximum_files: int | None = None) -> list[str]:
        if self.fd is None: raise OSError("vault is not open")
        output: list[str] = []

        def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
            try: entries = sorted(os.scandir(directory_fd), key=lambda item: item.name)
            except OSError: raise
            for entry in entries:
                if maximum_files is not None and len(output) >= maximum_files: return
                rel = prefix + (entry.name,)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                        try: walk(child, rel)
                        finally: os.close(child)
                    elif entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                        output.append(PurePosixPath(*rel).as_posix())
                except OSError:
                    continue
        walk(self.fd, ())
        return output
