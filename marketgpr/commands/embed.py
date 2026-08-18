"""MarketGPR Embed — Compute embeddings for pending names in a search DB.

The GPU-heavy step: loads a sentence-transformers model and encodes every
name_pool row with embedded=0, writing vectors into the sqlite-vec ANN table.
Commits after every batch so it's safe to Ctrl-C and resume — progress is
tracked entirely by the `embedded` flag, no separate checkpoint needed.

Run this on the rented GPU instance; `marketgpr index` (dedup/FTS sync) and
`marketgpr search` (querying) don't need one.
"""

import argparse
import sys
import time

from marketgpr.db import accent, dim, header, ok, warn
from marketgpr.vecdb import get_meta, insert_vectors, open_search_db, pending_names, count_pending


def register_args(parser: argparse.ArgumentParser):
    parser.add_argument("--search-db", required=True, help="Path to search DB (from `marketgpr index`)")
    parser.add_argument("--device", default=None,
                        help="torch device (default: auto — cuda if available, else cpu)")
    parser.add_argument("--batch-size", type=int, default=256,
                        help="Sentences per model.encode() call (default: 256)")
    parser.add_argument("--commit-every", type=int, default=5_000,
                        help="Names per DB commit — smaller = safer resume, more overhead (default: 5000)")


def resolve_device(requested: str) -> str:
    if requested:
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def run(args: argparse.Namespace):
    p = lambda s: print(s, flush=True)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        p(warn("Error: sentence-transformers not installed."))
        p(accent("  Install with: pip install -e '.[semantic]'"))
        sys.exit(1)

    search_conn = open_search_db(args.search_db, create=False)
    meta = get_meta(search_conn)
    model_name = meta["model_name"]

    device = resolve_device(args.device)

    p("")
    p(header("=== MarketGPR  ·  Embed ==="))
    p(accent(f"Search DB: {args.search_db}"))
    p(accent(f"Model:     {model_name}"))
    p(accent(f"Device:    {device}"))
    p("")

    total_pending = count_pending(search_conn)
    if total_pending == 0:
        p(ok("Nothing to embed — all names already have vectors."))
        p("")
        return

    p(accent(f"Pending: {total_pending:,} names"))
    p("")

    model = SentenceTransformer(model_name, device=device)

    done = 0
    start = time.monotonic()
    while True:
        batch = pending_names(search_conn, args.commit_every)
        if not batch:
            break

        rowids = [r[0] for r in batch]
        texts = [r[1] for r in batch]
        vectors = model.encode(
            texts,
            batch_size=args.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )

        insert_vectors(search_conn, zip(rowids, (v.tolist() for v in vectors)))

        done += len(batch)
        elapsed = time.monotonic() - start
        rate = done / elapsed if elapsed > 0 else 0
        remaining = total_pending - done
        eta = remaining / rate if rate > 0 else 0
        p(dim(f"  {done:,}/{total_pending:,}  "
              f"({rate:,.0f}/s, ETA {eta/60:.1f} min)"))

    search_conn.close()

    p("")
    p(ok(f"Done: {done:,} names embedded in {(time.monotonic() - start):.1f}s"))
    p("")
