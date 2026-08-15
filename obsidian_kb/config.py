"""Strict fixed TOML configuration for read-only live vault retrieval."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tomllib
import unicodedata

from .vault_io import bind_vault_root


class ConfigError(ValueError):
    """Raised when configuration is missing, unsafe, or malformed."""


def normalize_control_key(value: str) -> str:
    """Canonical projection shared by configuration and frontmatter parsing."""
    return unicodedata.normalize("NFKC", value).casefold()


def _control_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    forbidden = set(":#[]{}&,*!?|>'\"%@`")
    for value in values:
        key = normalize_control_key(value)
        if (not key or key == "imperator_retrieval" or key.startswith("-")
                or any(char.isspace() or unicodedata.category(char)[0] in {"C", "Z"}
                       or char in forbidden for char in key)):
            raise ConfigError("frontmatter control keys contain an unsafe or reserved name")
        if key in normalized:
            raise ConfigError("frontmatter control keys are ambiguous after Unicode normalization")
        normalized.append(key)
    return tuple(normalized)


@dataclass(frozen=True)
class Settings:
    vault: Path
    excluded_folders: tuple[str, ...] = (".git", ".obsidian", ".stfolder", ".trash", "Templates", "node_modules", "__pycache__", "logs", "sessions")
    excluded_globs: tuple[str, ...] = ("**/*session-dump*", "**/*task-queue*", "**/*.excalidraw.md")
    exclude_hidden: bool = True
    frontmatter_false_keys: tuple[str, ...] = ("index", "knowledge_index")
    extra_secret_patterns: tuple[str, ...] = ()
    max_lines: int = 60
    max_chars: int = 1200
    overlap_lines: int = 2
    corpus_id: str = "curated-obsidian"
    maximum_note_bytes: int = 2_097_152
    maximum_files: int = 5_000
    maximum_chunks: int = 50_000
    maximum_total_bytes: int = 268_435_456
    include_globs: tuple[str, ...] = ("*.md", "**/*.md")
    vault_device: int = field(init=False, repr=False)
    vault_inode: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        lexical = Path(self.vault).expanduser()
        if not lexical.is_absolute():
            lexical = Path(os.path.abspath(lexical))
        projected = _control_keys(tuple(self.frontmatter_false_keys))
        try:
            device, inode = bind_vault_root(lexical)
        except OSError as exc:
            raise ConfigError("vault root cannot be identity-bound safely") from exc
        object.__setattr__(self, "vault", lexical)
        object.__setattr__(self, "frontmatter_false_keys", projected)
        object.__setattr__(self, "vault_device", device)
        object.__setattr__(self, "vault_inode", inode)

    def compatibility_signature(self) -> str:
        policy = {
            "corpus": self.corpus_id,
            "include": self.include_globs,
            "folders": self.excluded_folders,
            "globs": self.excluded_globs,
            "hidden": self.exclude_hidden,
            "frontmatter": self.frontmatter_false_keys,
            "secrets": self.extra_secret_patterns,
            "max_lines": self.max_lines,
            "max_chars": self.max_chars,
            "overlap_lines": self.overlap_lines,
            "maximum_note_bytes": self.maximum_note_bytes,
        }
        raw = json.dumps(policy, sort_keys=True, separators=(",", ":"))
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
                  require_private: bool = False) -> Settings:
    data: dict = {}
    base = Path.cwd()
    if config_path:
        path = Path(config_path)
        base = path.absolute().parent
        data = _load_toml(path, require_private)
    allowed = {"schema_version", "corpus_id", "vault", "exclusions", "chunking", "resources"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    vault_cfg = _table(data, "vault")
    exclusions = _table(data, "exclusions")
    chunking = _table(data, "chunking")
    resources = _table(data, "resources")
    _known(vault_cfg, {"path"}, "vault")
    _known(exclusions, {"include_globs", "folders", "globs", "hidden", "frontmatter_false_keys", "secret_patterns"}, "exclusions")
    _known(chunking, {"max_lines", "max_chars", "overlap_lines", "maximum_note_bytes"}, "chunking")
    _known(resources, {"maximum_files", "maximum_chunks", "maximum_total_bytes"}, "resources")
    vault_value = vault or vault_cfg.get("path")
    if not isinstance(vault_value, (str, Path)) or not str(vault_value):
        raise ConfigError("vault.path is required")
    vault_path = Path(vault_value).expanduser()
    if not vault_path.is_absolute():
        vault_path = base / vault_path
    vault_path = Path(os.path.abspath(vault_path))
    folders = _strings(exclusions.get("folders"), "exclusions.folders") or Settings.excluded_folders
    globs = _strings(exclusions.get("globs"), "exclusions.globs") or Settings.excluded_globs
    includes = _strings(exclusions.get("include_globs"), "exclusions.include_globs") or Settings.include_globs
    for glob in globs + includes:
        if glob.startswith("/") or ".." in PurePosixPath(glob).parts or glob.count("[") != glob.count("]"):
            raise ConfigError(f"invalid glob: {glob!r}")
    keys = _strings(exclusions.get("frontmatter_false_keys"), "exclusions.frontmatter_false_keys") or Settings.frontmatter_false_keys
    patterns = _strings(exclusions.get("secret_patterns"), "exclusions.secret_patterns")
    hidden = exclusions.get("hidden", True)
    if not isinstance(hidden, bool):
        raise ConfigError("exclusions.hidden must be a boolean")
    values = {
        "max_lines": chunking.get("max_lines", 60),
        "max_chars": chunking.get("max_chars", 1200),
        "overlap_lines": chunking.get("overlap_lines", 2),
        "maximum_note_bytes": chunking.get("maximum_note_bytes", 2_097_152),
        "maximum_files": resources.get("maximum_files", 5_000),
        "maximum_chunks": resources.get("maximum_chunks", 50_000),
        "maximum_total_bytes": resources.get("maximum_total_bytes", 268_435_456),
    }
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1
           for key, value in values.items() if key != "overlap_lines"):
        raise ConfigError("chunking and resource limits must be positive integers")
    if not isinstance(values["overlap_lines"], int) or isinstance(values["overlap_lines"], bool) or not 0 <= values["overlap_lines"] <= 2:
        raise ConfigError("chunking.overlap_lines must be between 0 and 2")
    if values["max_chars"] < 64:
        raise ConfigError("chunking.max_chars must be >= 64")
    if data.get("schema_version", 1) != 1:
        raise ConfigError("configuration schema_version must be 1")
    corpus = data.get("corpus_id", "curated-obsidian")
    if not isinstance(corpus, str) or not corpus.strip():
        raise ConfigError("corpus_id must be a non-empty string")
    return Settings(vault_path, folders, globs, hidden, keys, patterns,
                    values["max_lines"], values["max_chars"], values["overlap_lines"], corpus,
                    values["maximum_note_bytes"], values["maximum_files"], values["maximum_chunks"],
                    values["maximum_total_bytes"], includes)
