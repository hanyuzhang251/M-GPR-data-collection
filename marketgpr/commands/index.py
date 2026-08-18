"""MarketGPR Index — Build/refresh the semantic-search name pool.

Reads DISTINCT contract names from a contracts database and syncs them into
a companion search database (dedup name pool + FTS5 keyword index + an
empty/partial sqlite-vec ANN table). Cheap, pure-SQL, no GPU or model needed —
safe to re-run any time the contracts DB grows (e.g. after `collect`/`enrich`).

New names land with embedded=0; run `marketgpr embed` afterward to fill in
their vectors.
"""

import argparse
import os
import sys

from marketgpr.db import accent, bold, connect_readonly, header, ok, warn
from marketgpr.vecdb import DEFAULT_DIM, DEFAULT_MODEL, get_meta, open_search_db, sync_names


def resolve_search_db_path(contracts_db: str, search_db_arg: str) -> str:
    if search_db_arg:
        return search_db_arg
    db_dir = os.path.dirname(os.path.abspath(contracts_db))
    base = os.path.splitext(os.path.basename(contracts_db))[0]
    return os.path.join(db_dir, f"{base}_search.db")


def register_args(parser: argparse.ArgumentParser):
    parser.add_argument("--db", required=True, help="Path to contracts SQLite database")
    parser.add_argument("--search-db", help="Path to search DB (default: <db>_search.db)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Embedding model this index is for (default: {DEFAULT_MODEL})")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM,
                        help=f"Embedding dimension for the model (default: {DEFAULT_DIM})")


def run(args: argparse.Namespace):
    p = lambda s: print(s, flush=True)

    if not os.path.isfile(args.db):
        p(warn(f"Error: database not found: {args.db}"))
        sys.exit(1)

    search_db_path = resolve_search_db_path(args.db, args.search_db)

    p("")
    p(header("=== MarketGPR  ·  Semantic Index Sync ==="))
    p(accent(f"Contracts DB: {args.db}"))
    p(accent(f"Search DB:    {search_db_path}"))
    p("")

    contracts_conn = connect_readonly(args.db)
    search_conn = open_search_db(search_db_path, dim=args.dim, model_name=args.model)

    meta = get_meta(search_conn)
    if meta["model_name"] != args.model or int(meta["dim"]) != args.dim:
        p(warn(f"Note: {search_db_path} already exists with model={meta['model_name']!r} "
               f"dim={meta['dim']} — using that, ignoring --model/--dim."))
        p("")

    added, total = sync_names(contracts_conn, search_conn)

    contracts_conn.close()

    pending = search_conn.execute(
        "SELECT COUNT(*) FROM name_pool WHERE embedded=0"
    ).fetchone()[0]
    search_conn.close()

    p(bold("Result"))
    p(ok(f"  New unique names added:  {added:,}"))
    p(accent(f"  Total unique names:      {total:,}"))
    if pending:
        p(warn(f"  Pending embedding:       {pending:,}"))
        p(accent(f"  Run: marketgpr embed --search-db {search_db_path}"))
    else:
        p(ok(f"  All names embedded."))
    p("")
