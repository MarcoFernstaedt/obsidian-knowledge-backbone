"""Strict TOML configuration for the knowledge backbone."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    vault: Path
    state: Path
    excluded_folders: tuple[str, ...] = (".git", ".obsidian", ".trash", "Templates")
    excluded_globs: tuple[str, ...] = ()
    exclude_hidden: bool = True
    frontmatter_false_keys: tuple[str, ...] = ("index", "semantic_index", "knowledge_index")
    extra_secret_patterns: tuple[str, ...] = ()
    max_lines: int = 60
    max_chars: int = 6000
    ollama_url: str | None = None
    ollama_model: str = "nomic-embed-text"
    qdrant_url: str | None = None
    qdrant_collection: str = "obsidian_knowledge"
    vector_size: int = 768
    timeout: float = 10.0


def _strings(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ConfigError(f"{key} must be an array of non-empty strings")
    return tuple(value)


def load_settings(config_path: str | Path | None = None, *, vault: str | Path | None = None,
                  state: str | Path | None = None) -> Settings:
    data: dict = {}
    base = Path.cwd()
    if config_path:
        path = Path(config_path)
        base = path.resolve().parent
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot load config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be a table")
        data = loaded
    allowed = {"vault", "state", "exclusions", "chunking", "semantic"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    vault_cfg = data.get("vault", {})
    state_cfg = data.get("state", {})
    exclusions = data.get("exclusions", {})
    chunking = data.get("chunking", {})
    semantic = data.get("semantic", {})
    for name, value in (("vault", vault_cfg), ("state", state_cfg), ("exclusions", exclusions),
                        ("chunking", chunking), ("semantic", semantic)):
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a table")
    vault_value = vault or vault_cfg.get("path")
    state_value = state or state_cfg.get("sqlite_path")
    if not isinstance(vault_value, (str, Path)) or not str(vault_value):
        raise ConfigError("vault.path or --vault is required")
    if not isinstance(state_value, (str, Path)) or not str(state_value):
        raise ConfigError("state.sqlite_path or --state is required")
    vault_path = Path(vault_value).expanduser()
    state_path = Path(state_value).expanduser()
    if not vault_path.is_absolute(): vault_path = base / vault_path
    if not state_path.is_absolute(): state_path = base / state_path
    folders = _strings(exclusions.get("folders"), "exclusions.folders") or Settings.excluded_folders
    globs = _strings(exclusions.get("globs"), "exclusions.globs")
    keys = _strings(exclusions.get("frontmatter_false_keys"), "exclusions.frontmatter_false_keys") or Settings.frontmatter_false_keys
    patterns = _strings(exclusions.get("secret_patterns"), "exclusions.secret_patterns")
    exclude_hidden = exclusions.get("hidden", True)
    if not isinstance(exclude_hidden, bool):
        raise ConfigError("exclusions.hidden must be a boolean")
    max_lines = chunking.get("max_lines", 60)
    max_chars = chunking.get("max_chars", 6000)
    vector_size = semantic.get("vector_size", 768)
    timeout = semantic.get("timeout", 10.0)
    if not isinstance(max_lines, int) or max_lines < 1: raise ConfigError("chunking.max_lines must be a positive integer")
    if not isinstance(max_chars, int) or max_chars < 64: raise ConfigError("chunking.max_chars must be an integer >= 64")
    if not isinstance(vector_size, int) or vector_size < 1: raise ConfigError("semantic.vector_size must be a positive integer")
    if not isinstance(timeout, (int, float)) or timeout <= 0: raise ConfigError("semantic.timeout must be positive")
    for key in ("ollama_url", "ollama_model", "qdrant_url", "collection"):
        if key in semantic and semantic[key] is not None and not isinstance(semantic[key], str):
            raise ConfigError(f"semantic.{key} must be a string")
    return Settings(vault_path.resolve(), state_path.resolve(), folders, globs, exclude_hidden, keys, patterns,
                    max_lines, max_chars, semantic.get("ollama_url"), semantic.get("ollama_model", "nomic-embed-text"),
                    semantic.get("qdrant_url"), semantic.get("collection", "obsidian_knowledge"),
                    vector_size, float(timeout))
