"""Semantic search backing store — dedup name pool, FTS5 keyword index, and
a sqlite-vec ANN vector index, all kept in a companion SQLite file alongside
a contracts database.

Never touches the source contracts database. The search DB holds one row per
*distinct* contract name (not per contract) since Kalshi names are heavily
templated and the unique-name space is far smaller than the contract count.
"""

import hashlib
import os
import sqlite3
from typing import Iterable, Optional

import sqlite_vec

DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_DIM = 768

# bge-style models are asymmetric: only the query gets this instruction
# prefix, not the indexed passages/names.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

CREATE_NAME_POOL = """CREATE TABLE IF NOT EXISTS name_pool (
    name_hash  TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    embedded   INTEGER NOT NULL DEFAULT 0
)"""

CREATE_NAME_FTS = """CREATE VIRTUAL TABLE IF NOT EXISTS name_fts USING fts5(
    name, content='name_pool', content_rowid='rowid'
)"""

CREATE_META = """CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
)"""


def name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _require_extension_loading(conn: sqlite3.Connection) -> None:
    if not hasattr(conn, "enable_load_extension"):
        raise RuntimeError(
            "This Python's sqlite3 module was built without loadable-extension "
            "support, so sqlite-vec cannot be loaded. Use a Python build that "
            "supports it (Homebrew Python on macOS, or the system/apt Python on "
            "most Linux distros — the default on most GPU rental images)."
        )


def _create_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS name_vec USING vec0("
        f"embedding float[{dim}] distance_metric=cosine)"
    )


def open_search_db(
    path: str,
    dim: int = DEFAULT_DIM,
    model_name: str = DEFAULT_MODEL,
    create: bool = True,
) -> sqlite3.Connection:
    """Open (or create) the companion search DB with the name pool, FTS5
    keyword index, and sqlite-vec ANN index wired up.

    If the DB already exists, `dim`/`model_name` are ignored in favor of
    whatever it was actually built with — callers that just want to use an
    existing index (embed, search) should not need to know or repeat its
    model. `create=True` with a brand-new path is the only case where the
    given `dim`/`model_name` take effect, recorded permanently in `meta`.
    """
    exists = os.path.isfile(path)
    if not create and not exists:
        raise FileNotFoundError(f"Search DB not found: {path}")

    conn = sqlite3.connect(path)
    _require_extension_loading(conn)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute(CREATE_META)
    conn.execute(CREATE_NAME_POOL)
    conn.execute(CREATE_NAME_FTS)

    row = conn.execute("SELECT value FROM meta WHERE key='model_name'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('model_name', ?)", (model_name,))
        conn.execute("INSERT INTO meta(key, value) VALUES ('dim', ?)", (str(dim),))
        conn.commit()
    else:
        model_name = row[0]
        dim = int(conn.execute("SELECT value FROM meta WHERE key='dim'").fetchone()[0])

    _create_vec_table(conn, dim)
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return dict(rows)


def sync_names(contracts_conn: sqlite3.Connection, search_conn: sqlite3.Connection,
                batch_size: int = 20_000) -> tuple:
    """Pull DISTINCT names from the contracts DB into the name pool.

    Idempotent — only newly-seen names are inserted (existing ones are left
    with whatever `embedded` state they already have). Returns
    (names_added, total_names).
    """
    cursor = contracts_conn.execute("SELECT DISTINCT name FROM contracts")
    added = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for (raw_name,) in rows:
            name = raw_name.strip()
            if not name:
                continue
            h = name_hash(name)
            cur = search_conn.execute(
                "INSERT INTO name_pool(name_hash, name) VALUES (?, ?) "
                "ON CONFLICT(name_hash) DO NOTHING RETURNING rowid",
                (h, name),
            )
            result = cur.fetchone()
            if result is not None:
                search_conn.execute(
                    "INSERT INTO name_fts(rowid, name) VALUES (?, ?)",
                    (result[0], name),
                )
                added += 1
        search_conn.commit()

    total = search_conn.execute("SELECT COUNT(*) FROM name_pool").fetchone()[0]
    return added, total


def pending_names(search_conn: sqlite3.Connection, limit: int) -> list:
    """Names not yet embedded, as (rowid, name) pairs."""
    return search_conn.execute(
        "SELECT rowid, name FROM name_pool WHERE embedded=0 LIMIT ?", (limit,)
    ).fetchall()


def count_pending(search_conn: sqlite3.Connection) -> int:
    return search_conn.execute("SELECT COUNT(*) FROM name_pool WHERE embedded=0").fetchone()[0]


def insert_vectors(search_conn: sqlite3.Connection, rowid_vectors: Iterable[tuple]) -> None:
    """rowid_vectors: iterable of (rowid, list[float]). Also flips `embedded`."""
    rowid_vectors = list(rowid_vectors)
    search_conn.executemany(
        "INSERT INTO name_vec(rowid, embedding) VALUES (?, ?)",
        [(rowid, sqlite_vec.serialize_float32(vec)) for rowid, vec in rowid_vectors],
    )
    search_conn.executemany(
        "UPDATE name_pool SET embedded=1 WHERE rowid=?",
        [(rowid,) for rowid, _ in rowid_vectors],
    )
    search_conn.commit()


def knn_search(search_conn: sqlite3.Connection, query_vector: list, k: int) -> list:
    """Top-k nearest names by cosine distance. Returns [(rowid, distance), ...]."""
    return search_conn.execute(
        "SELECT rowid, distance FROM name_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(query_vector), k),
    ).fetchall()


def get_vector(search_conn: sqlite3.Connection, rowid: int) -> Optional[bytes]:
    row = search_conn.execute("SELECT embedding FROM name_vec WHERE rowid=?", (rowid,)).fetchone()
    return row[0] if row else None


def fts_candidates(search_conn: sqlite3.Connection, terms: list, limit: int = 500) -> set:
    """Rowids of names matching ANY query term (FTS5 OR query)."""
    if not terms:
        return set()
    match_query = " OR ".join(f'"{t}"' for t in terms)
    rows = search_conn.execute(
        "SELECT rowid FROM name_fts WHERE name_fts MATCH ? LIMIT ?",
        (match_query, limit),
    ).fetchall()
    return {r[0] for r in rows}


def get_names(search_conn: sqlite3.Connection, rowids: list) -> dict:
    if not rowids:
        return {}
    placeholders = ",".join("?" * len(rowids))
    rows = search_conn.execute(
        f"SELECT rowid, name FROM name_pool WHERE rowid IN ({placeholders})", rowids
    ).fetchall()
    return {r[0]: r[1] for r in rows}
