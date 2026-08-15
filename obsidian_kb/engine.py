import os
import hashlib
from typing import List, Dict, Optional
from .chunker import chunk_markdown
from .fts_index import FTSIndex
from .secret_filter import contains_secret

class IndexEngine:
    def __init__(self, vault_root, index_path, exclusion_dirs, exclusion_globs, secret_patterns):
        self.vault_root = vault_root
        self.index_path = index_path
        self.exclusion_dirs = set(exclusion_dirs)
        self.exclusion_globs = exclusion_globs
        self.secret_patterns = secret_patterns
        self.fts = FTSIndex(index_path)
    def should_exclude_file(self, rel_path: str, abspath: str, lines: List[str]) -> Optional[str]:
        parts = rel_path.strip(os.sep).split(os.sep)
        if any(p in self.exclusion_dirs for p in parts):
            return 'dir-excluded'
        for g in self.exclusion_globs:
            if g.strip('*').lower() in rel_path.lower():
                return 'glob-excluded'
        raw = '\n'.join(lines)
        if contains_secret(raw, self.secret_patterns):
            return 'secret-pattern'
        return None
    def index_vault(self):
        for dirpath, dirs, files in os.walk(self.vault_root):
            dirs[:] = [d for d in dirs if d not in self.exclusion_dirs]
            for fname in files:
                if not fname.endswith('.md'): continue
                abspath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abspath, self.vault_root)
                with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                exclude_reason = self.should_exclude_file(rel_path, abspath, lines)
                if exclude_reason:
                    # log only rel_path and reason
                    continue
                text = '\n'.join(lines)
                sha256 = hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()
                for chunk in chunk_markdown(text, sha256, rel_path):
                    self.fts.upsert_chunks([chunk])
    def close(self):
        self.fts.close()
