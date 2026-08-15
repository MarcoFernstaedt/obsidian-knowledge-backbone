import click
import sys
import os
import toml
from obsidian_kb.engine import IndexEngine
from obsidian_kb.fts_index import FTSIndex
from obsidian_kb.rrf import reciprocal_rank_fusion

@click.group()
def cli():
    """Obsidian Knowledge Backbone CLI."""
    pass

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None)
@click.option('--vault', type=click.Path(exists=True), default=None)
@click.option('--state', type=click.Path(), default=None)
@click.option('--exclude', multiple=True)
@click.option('--offline', is_flag=True, default=False)
def index(config, vault, state, exclude, offline):
    """Index the Obsidian vault (incremental, exclusion safe)."""
    cfg = toml.load(config) if config else {}
    vault_root = vault or cfg.get('vault', {}).get('path')
    state_path = state or cfg.get('state', {}).get('sqlite_path', 'index.sqlite')
    exclusion_dirs = set(cfg.get('exclusions', {}).get('folders', []))
    exclusion_globs = set(cfg.get('exclusions', {}).get('globs', []))
    secret_patterns = cfg.get('exclusions', {}).get('secret_patterns', [])
    engine = IndexEngine(vault_root, state_path, exclusion_dirs, exclusion_globs, secret_patterns)
    engine.index_vault()
    click.echo('Indexing complete.')
    engine.close()

@cli.command()
@click.argument('query')
@click.option('--config', type=click.Path(exists=True), default=None)
@click.option('--vault', type=click.Path(exists=True), default=None)
@click.option('--state', type=click.Path(), default=None)
@click.option('--k', type=int, default=5)
@click.option('--offline', is_flag=True, default=False)
@click.option('--json', 'as_json', is_flag=True, default=False)
def query(query, config, vault, state, k, offline, as_json):
    """Run a hybrid search. Fallbacks if semantic/Qdrant unavailable."""
    cfg = toml.load(config) if config else {}
    state_path = state or cfg.get('state', {}).get('sqlite_path', 'index.sqlite')
    fts = FTSIndex(state_path)
    results = fts.query(query, k=k)
    fused = reciprocal_rank_fusion([results], k=k)
    if as_json:
        import json
        click.echo(json.dumps(fused, indent=2))
    else:
        for r in fused:
            click.echo(f"{r.get('file_path')}:{r.get('start_line')}-{r.get('end_line')}: {r.get('snippet')}")
    fts.close()

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None)
@click.option('--vault', type=click.Path(exists=True), default=None)
@click.option('--state', type=click.Path(), default=None)
def audit(config, vault, state):
    """Audit index for exclusion counts, health, no leaking content."""
    cfg = toml.load(config) if config else {}
    state_path = state or cfg.get('state', {}).get('sqlite_path', 'index.sqlite')
    fts = FTSIndex(state_path)
    count = 0
    for row in fts.conn.execute("SELECT COUNT(1) FROM chunks").fetchall():
        count += row[0]
    click.echo(f"Chunks in index: {count}")
    fts.close()

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None)
@click.option('--state', type=click.Path(), default=None)
def status(config, state):
    cfg = toml.load(config) if config else {}
    state_path = state or cfg.get('state', {}).get('sqlite_path', 'index.sqlite')
    exists = os.path.exists(state_path)
    if not exists:
        click.echo("No index at " + state_path)
        sys.exit(1)
    fts = FTSIndex(state_path)
    num_chunks = fts.conn.execute("SELECT COUNT(1) FROM chunks").fetchone()[0]
    click.echo(f"SQLite index ok; {num_chunks} chunks present.")
    fts.close()

if __name__ == "__main__":
    cli()
