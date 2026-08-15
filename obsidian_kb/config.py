"""Strict local TOML configuration for the knowledge backbone."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tomllib


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    vault: Path
    state: Path
    excluded_folders: tuple[str, ...] = (".git", ".obsidian", ".stfolder", ".trash", "Templates", "node_modules", "__pycache__", "logs", "sessions")
    excluded_globs: tuple[str, ...] = ("**/*session-dump*", "**/*task-queue*", "**/*.excalidraw.md")
    exclude_hidden: bool = True
    frontmatter_false_keys: tuple[str, ...] = ("index", "knowledge_index")
    extra_secret_patterns: tuple[str, ...] = ()
    max_lines: int = 60
    max_chars: int = 1200
    overlap_lines: int = 2
    corpus_id: str = "curated-obsidian"
    schema_version: int = 3
    chunker_version: str = "heading-v4"
    maximum_note_bytes: int = 2_097_152
    fallback_max_files: int = 5_000
    freshness_max_files: int = 100_000
    include_globs: tuple[str, ...] = ("*.md", "**/*.md")

    def compatibility(self) -> dict[str, object]:
        policy = json.dumps({"include_globs": self.include_globs, "folders": self.excluded_folders,
                             "globs": self.excluded_globs, "hidden": self.exclude_hidden,
                             "frontmatter": self.frontmatter_false_keys, "secrets": self.extra_secret_patterns,
                             "maximum_note_bytes": self.maximum_note_bytes}, sort_keys=True, separators=(",", ":"))
        return {"schema_version": self.schema_version, "corpus_id": self.corpus_id,
                "chunker_version": self.chunker_version, "max_lines": self.max_lines,
                "max_chars": self.max_chars, "overlap_lines": self.overlap_lines,
                "policy_fingerprint": hashlib.sha256(policy.encode()).hexdigest()}

    def compatibility_signature(self) -> str:
        raw = json.dumps(self.compatibility(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def _strings(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{key} must be an array of non-empty strings")
    return tuple(value)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _known(table: dict, allowed: set[str], key: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"unknown {key} key(s): {', '.join(sorted(unknown))}")


def validate_relative_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path_prefix must be a non-empty relative vault path")
    if "\\" in value or value.startswith("/"):
        raise ValueError("path_prefix must be a relative POSIX vault path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path_prefix must not contain traversal")
    return path.as_posix().rstrip("/")


def _load_toml(path: Path, require_private: bool) -> dict:
    try:
        info = path.lstat()
        if require_private and (not stat.S_ISREG(info.st_mode) or path.is_symlink() or
                                info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ConfigError("config must be a current-user-owned regular non-symlink file with mode 0600")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (require_private and
                    (opened.st_uid != os.getuid() or opened.st_mode & 0o077 or
                     (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino))):
                raise ConfigError("config security invariant failed")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                loaded = tomllib.load(handle)
        finally:
            os.close(fd)
    except ConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load config: {type(exc).__name__}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("config root must be a table")
    return loaded


def load_settings(config_path: str | Path | None = None, *, vault: str | Path | None = None,
                  state: str | Path | None = None, require_private: bool = False) -> Settings:
    data: dict = {}
    base = Path.cwd()
    if config_path:
        path = Path(config_path)
        base = path.absolute().parent
        data = _load_toml(path, require_private)
    allowed = {"schema_version", "corpus_id", "vault", "state", "exclusions", "chunking"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    vault_cfg, state_cfg = _table(data, "vault"), _table(data, "state")
    exclusions, chunking = _table(data, "exclusions"), _table(data, "chunking")
    _known(vault_cfg, {"path"}, "vault")
    _known(state_cfg, {"sqlite_path"}, "state")
    _known(exclusions, {"include_globs", "folders", "globs", "hidden", "frontmatter_false_keys", "secret_patterns"}, "exclusions")
    _known(chunking, {"max_lines", "max_chars", "overlap_lines", "maximum_note_bytes", "fallback_max_files", "freshness_max_files"}, "chunking")
    vault_value, state_value = vault or vault_cfg.get("path"), state or state_cfg.get("sqlite_path")
    if not isinstance(vault_value, (str, Path)) or not str(vault_value):
        raise ConfigError("vault.path is required")
    if not isinstance(state_value, (str, Path)) or not str(state_value):
        raise ConfigError("state.sqlite_path is required")
    vault_path, state_path = Path(vault_value).expanduser(), Path(state_value).expanduser()
    if not vault_path.is_absolute(): vault_path = base / vault_path
    if not state_path.is_absolute(): state_path = base / state_path
    vault_path, state_path = vault_path.resolve(), state_path.resolve()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock").resolve()
    if state_path == vault_path or state_path.is_relative_to(vault_path) or lock_path == vault_path or lock_path.is_relative_to(vault_path):
        raise ConfigError("state database and lock must be outside the vault")
    folders = _strings(exclusions.get("folders"), "exclusions.folders") or Settings.excluded_folders
    globs = _strings(exclusions.get("globs"), "exclusions.globs") or Settings.excluded_globs
    includes = _strings(exclusions.get("include_globs"), "exclusions.include_globs") or Settings.include_globs
    for glob in globs + includes:
        if glob.startswith("/") or ".." in PurePosixPath(glob).parts or glob.count("[") != glob.count("]"):
            raise ConfigError(f"invalid glob: {glob!r}")
    keys = _strings(exclusions.get("frontmatter_false_keys"), "exclusions.frontmatter_false_keys") or Settings.frontmatter_false_keys
    patterns = _strings(exclusions.get("secret_patterns"), "exclusions.secret_patterns")
    hidden = exclusions.get("hidden", True)
    if not isinstance(hidden, bool): raise ConfigError("exclusions.hidden must be a boolean")
    values = {"max_lines": chunking.get("max_lines", 60), "max_chars": chunking.get("max_chars", 1200),
              "overlap_lines": chunking.get("overlap_lines", 2), "maximum_note_bytes": chunking.get("maximum_note_bytes", 2_097_152),
              "fallback_max_files": chunking.get("fallback_max_files", 5_000), "freshness_max_files": chunking.get("freshness_max_files", 100_000)}
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for key, value in values.items() if key != "overlap_lines"):
        raise ConfigError("chunking limits must be positive integers")
    if not isinstance(values["overlap_lines"], int) or isinstance(values["overlap_lines"], bool) or not 0 <= values["overlap_lines"] <= 2:
        raise ConfigError("chunking.overlap_lines must be between 0 and 2")
    if values["max_chars"] < 64: raise ConfigError("chunking.max_chars must be >= 64")
    if data.get("schema_version", 1) != 1: raise ConfigError("configuration schema_version must be 1")
    corpus = data.get("corpus_id", "curated-obsidian")
    if not isinstance(corpus, str) or not corpus.strip(): raise ConfigError("corpus_id must be a non-empty string")
    return Settings(vault_path, state_path, folders, globs, hidden, keys, patterns,
                    values["max_lines"], values["max_chars"], values["overlap_lines"], corpus,
                    3, "heading-v4", values["maximum_note_bytes"], values["fallback_max_files"],
                    values["freshness_max_files"], includes)
