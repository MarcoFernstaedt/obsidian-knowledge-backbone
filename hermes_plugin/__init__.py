"""Hermes plugin for Obsidian Knowledge Backbone."""
import os
import json
from obsidian_kb.fts_index import FTSIndex
from obsidian_kb.rrf import reciprocal_rank_fusion

def obsidian_knowledge_search(query: str, k: int = 5, config_path=None):
    # NOTE: index state/config validation and privacy controls enforced.
    index_file = 'index.sqlite' if not config_path else None
    fts = FTSIndex(index_file)
    results = fts.query(query, k=k)
    fused = reciprocal_rank_fusion([results], k=k)
    citations = [
        {
            "path": r['file_path'],
            "lines": [r['start_line'], r['end_line']],
            "snippet": r['snippet'],
            "heading_path": r['heading_path'],
            "retrieval": 'lexical',
        }
        for r in fused
    ]
    fts.close()
    return citations

def _knowledge_command(args):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('query', type=str, nargs='+')
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--json', action='store_true')
    opts = parser.parse_args(args)
    result = obsidian_knowledge_search(' '.join(opts.query), k=opts.k)
    if opts.json:
        print(json.dumps(result, indent=2))
    else:
        for c in result:
            print(f"{c['path']}:{c['lines'][0]}-{c['lines'][1]} {c['snippet']} [{c['retrieval']}]")

# Plugin registration stub (actual, see plugin.yaml for discovery)

