"""Strict TOML configuration for the knowledge backbone."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import tomllib
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    vault: Path
    state: Path
    excluded_folders: tuple[str, ...] = (".git", ".obsidian", ".stfolder", ".trash", "Templates", "node_modules", "__pycache__", "logs", "sessions")
    excluded_globs: tuple[str, ...] = ("**/*session-dump*", "**/*task-queue*", "**/*.excalidraw.md")
    exclude_hidden: bool = True
    frontmatter_false_keys: tuple[str, ...] = ("index", "semantic_index", "knowledge_index")
    extra_secret_patterns: tuple[str, ...] = ()
    max_lines: int = 60
    max_chars: int = 1200
    ollama_url: str | None = None
    ollama_model: str = "nomic-embed-text"
    qdrant_url: str | None = None
    qdrant_collection: str = "imperator_obsidian_chunks_v2"
    vector_size: int = 768
    timeout: float = 10.0
    corpus_id: str = "curated-obsidian"
    schema_version: int = 2
    chunker_version: str = "heading-v2"
    model_digest: str = "unknown"
    maximum_note_bytes: int = 2_097_152
    fallback_max_files: int = 5_000
    include_globs: tuple[str, ...] = ("*.md", "**/*.md")

    def compatibility(self) -> dict[str, object]:
        policy = json.dumps({"include_globs": self.include_globs, "folders": self.excluded_folders, "globs": self.excluded_globs,
                             "hidden": self.exclude_hidden, "frontmatter": self.frontmatter_false_keys,
                             "secrets": self.extra_secret_patterns}, sort_keys=True, separators=(",", ":"))
        return {"schema_version": self.schema_version, "corpus_id": self.corpus_id,
                "collection": self.qdrant_collection, "embedding_model": self.ollama_model,
                "model_digest": self.model_digest, "vector_size": self.vector_size,
                "chunker_version": self.chunker_version,
                "policy_fingerprint": hashlib.sha256(policy.encode()).hexdigest()}

    def compatibility_signature(self) -> str:
        raw = json.dumps(self.compatibility(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def _strings(value: object, key: str) -> tuple[str, ...]:
    if value is None: return ()
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ConfigError(f"{key} must be an array of non-empty strings")
    return tuple(value)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict): raise ConfigError(f"{key} must be a table")
    return value


def _known(table: dict, allowed: set[str], key: str) -> None:
    unknown = set(table) - allowed
    if unknown: raise ConfigError(f"unknown {key} key(s): {', '.join(sorted(unknown))}")


def _url(value: object, key: str) -> str | None:
    if value is None: return None
    if not isinstance(value, str): raise ConfigError(f"{key} must be a string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError(f"{key} must be an http(s) URL without credentials")
    return value.rstrip("/")


def validate_relative_prefix(value: str | None) -> str | None:
    if value is None: return None
    if not isinstance(value, str) or not value.strip(): raise ValueError("path_prefix must be a non-empty relative vault path")
    if "\\" in value or value.startswith("/"):
        raise ValueError("path_prefix must be a relative POSIX vault path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path_prefix must not contain traversal")
    return path.as_posix().rstrip("/")


def load_settings(config_path: str | Path | None = None, *, vault: str | Path | None = None,
                  state: str | Path | None = None) -> Settings:
    data: dict = {}; base = Path.cwd()
    if config_path:
        path = Path(config_path); base = path.resolve().parent
        try:
            with path.open("rb") as handle: loaded = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc: raise ConfigError(f"cannot load config: {exc}") from exc
        if not isinstance(loaded, dict): raise ConfigError("config root must be a table")
        data = loaded
    allowed = {"schema_version", "corpus_id", "vault", "state", "exclusions", "chunking", "semantic"}
    unknown = set(data) - allowed
    if unknown: raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    vault_cfg, state_cfg = _table(data,"vault"), _table(data,"state")
    exclusions, chunking, semantic = _table(data,"exclusions"), _table(data,"chunking"), _table(data,"semantic")
    _known(vault_cfg,{"path"},"vault"); _known(state_cfg,{"sqlite_path"},"state")
    _known(exclusions,{"include_globs","folders","globs","hidden","frontmatter_false_keys","secret_patterns"},"exclusions")
    _known(chunking,{"max_lines","max_chars","maximum_note_bytes","fallback_max_files"},"chunking")
    _known(semantic,{"ollama_url","ollama_model","qdrant_url","collection","vector_size","timeout","model_digest"},"semantic")
    vault_value, state_value = vault or vault_cfg.get("path"), state or state_cfg.get("sqlite_path")
    if not isinstance(vault_value,(str,Path)) or not str(vault_value): raise ConfigError("vault.path or --vault is required")
    if not isinstance(state_value,(str,Path)) or not str(state_value): raise ConfigError("state.sqlite_path or --state is required")
    vault_path, state_path = Path(vault_value).expanduser(), Path(state_value).expanduser()
    if not vault_path.is_absolute(): vault_path = base / vault_path
    if not state_path.is_absolute(): state_path = base / state_path
    folders = _strings(exclusions.get("folders"),"exclusions.folders") or Settings.excluded_folders
    globs = _strings(exclusions.get("globs"),"exclusions.globs") or Settings.excluded_globs
    includes = _strings(exclusions.get("include_globs"),"exclusions.include_globs") or Settings.include_globs
    for glob in globs + includes:
        if glob.startswith("/") or ".." in PurePosixPath(glob).parts or glob.count("[") != glob.count("]"):
            raise ConfigError(f"invalid exclusion glob: {glob!r}")
    keys = _strings(exclusions.get("frontmatter_false_keys"),"exclusions.frontmatter_false_keys") or Settings.frontmatter_false_keys
    patterns = _strings(exclusions.get("secret_patterns"),"exclusions.secret_patterns")
    hidden = exclusions.get("hidden",True)
    if not isinstance(hidden,bool): raise ConfigError("exclusions.hidden must be a boolean")
    ints = {"max_lines": chunking.get("max_lines",60), "max_chars": chunking.get("max_chars",1200),
            "maximum_note_bytes": chunking.get("maximum_note_bytes",2_097_152),
            "fallback_max_files": chunking.get("fallback_max_files",5_000),
            "vector_size": semantic.get("vector_size",768)}
    if any(not isinstance(v,int) or isinstance(v,bool) or v < 1 for v in ints.values()): raise ConfigError("chunking and vector sizes must be positive integers")
    if ints["max_chars"] < 64: raise ConfigError("chunking.max_chars must be >= 64")
    timeout = semantic.get("timeout",10.0)
    if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or timeout <= 0 or timeout > 120: raise ConfigError("semantic.timeout must be in (0, 120]")
    schema_version=data.get("schema_version",2); corpus=data.get("corpus_id","curated-obsidian")
    if schema_version != 2: raise ConfigError("schema_version must be 2")
    if not isinstance(corpus,str) or not corpus.strip(): raise ConfigError("corpus_id must be a non-empty string")
    model=semantic.get("ollama_model","nomic-embed-text"); collection=semantic.get("collection","imperator_obsidian_chunks_v2")
    digest=semantic.get("model_digest","unknown")
    if any(not isinstance(x,str) or not x for x in (model,collection,digest)): raise ConfigError("semantic model, collection, and digest must be non-empty strings")
    return Settings(vault_path.resolve(),state_path.resolve(),folders,globs,hidden,keys,patterns,
                    ints["max_lines"],ints["max_chars"],_url(semantic.get("ollama_url"),"semantic.ollama_url"),model,
                    _url(semantic.get("qdrant_url"),"semantic.qdrant_url"),collection,ints["vector_size"],float(timeout),
                    corpus,schema_version,"heading-v2",digest,ints["maximum_note_bytes"],ints["fallback_max_files"],includes)
