"""MarketGPR Search — Hybrid semantic + keyword search over contract names.

Combines cosine similarity from the sqlite-vec ANN index with an additive
keyword-coverage boost from FTS5. Embedding similarity sets the floor for
every candidate (it's always directionally right, even with zero keyword
overlap); a keyword match can only push a result up, never drag one down —
so a paraphrase with no shared words still ranks on its merits.

Runs the embedding model on CPU by default — only one string (the query)
needs encoding per search, so no GPU is needed here.
"""

import argparse
import re
import sys

from marketgpr.db import accent, connect_readonly, dim, header, highlight, warn
from marketgpr.vecdb import (QUERY_INSTRUCTION, fts_candidates, get_meta, get_names,
                             get_vector, knn_search, open_search_db)

TOKEN_RE = re.compile(r"[a-z0-9]+")


def register_args(parser: argparse.ArgumentParser):
    parser.add_argument("query", nargs="+", help="Search text")
    parser.add_argument("--search-db", required=True, help="Path to search DB (from `marketgpr index`)")
    parser.add_argument("--contracts-db", required=True, help="Path to contracts DB (to resolve tickers)")
    parser.add_argument("-k", "--limit", type=int, default=15, help="Results to show (default: 15)")
    parser.add_argument("--candidates", type=int, default=200,
                        help="ANN candidate pool size before reranking (default: 200)")
    parser.add_argument("--boost-weight", type=float, default=0.15,
                        help="Max additive keyword-coverage boost, added to cosine similarity (default: 0.15)")
    parser.add_argument("--device", default="cpu", help="torch device for query embedding (default: cpu)")
    parser.add_argument("--examples", type=int, default=3, help="Example tickers to show per result (default: 3)")


def tokenize(text: str) -> list:
    return TOKEN_RE.findall(text.lower())


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

    query_text = " ".join(args.query)
    model = SentenceTransformer(model_name, device=args.device)
    query_vec = model.encode(
        QUERY_INSTRUCTION + query_text,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )

    import numpy as np

    knn_hits = knn_search(search_conn, query_vec.tolist(), args.candidates)
    similarities = {rowid: 1.0 - distance for rowid, distance in knn_hits}

    terms = tokenize(query_text)
    fts_rowids = fts_candidates(search_conn, terms)

    # FTS may surface names outside the ANN top-N candidate pool — score
    # those directly so a strong keyword match isn't dropped for lack of
    # cosine proximity alone.
    missing = fts_rowids - similarities.keys()
    for rowid in missing:
        blob = get_vector(search_conn, rowid)
        if blob is None:
            continue
        vec = np.frombuffer(blob, dtype="float32")
        similarities[rowid] = float(np.dot(query_vec, vec))

    names = get_names(search_conn, list(similarities.keys()))

    scored = []
    for rowid, similarity in similarities.items():
        name = names.get(rowid, "")
        name_lower = name.lower()
        if terms:
            matched = sum(1 for t in terms if t in name_lower)
            coverage = matched / len(terms)
        else:
            coverage = 0.0
        final_score = similarity + args.boost_weight * coverage
        scored.append((final_score, similarity, coverage, rowid, name))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: args.limit]

    search_conn.close()

    p("")
    p(header("=== MarketGPR  ·  Semantic Search ==="))
    p(accent(f"Query: {query_text!r}"))
    p(dim(f"  {len(similarities):,} candidates scored "
          f"({len(knn_hits)} ANN + {len(missing)} keyword-only)"))
    p("")

    if not top:
        p(warn("No results."))
        p("")
        return

    contracts_conn = connect_readonly(args.contracts_db)

    for rank, (final_score, similarity, coverage, rowid, name) in enumerate(top, start=1):
        tickers = contracts_conn.execute(
            "SELECT ticker FROM contracts WHERE name=? LIMIT ?",
            (name, args.examples),
        ).fetchall()
        total = contracts_conn.execute(
            "SELECT COUNT(*) FROM contracts WHERE name=?", (name,)
        ).fetchone()[0]

        p(highlight(f"{rank:>3}. [{final_score:.3f}]") + f"  {name}")
        p(dim(f"      cosine={similarity:.3f}  keyword_coverage={coverage:.2f}  "
              f"contracts={total:,}"))
        for (ticker,) in tickers:
            p(dim(f"      · {ticker}"))
        p("")

    contracts_conn.close()
