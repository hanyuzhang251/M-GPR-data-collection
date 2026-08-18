#!/usr/bin/env python3
"""Shared database helpers and ANSI colour palette for MarketGPR tools.

Provides:
    * Canonical contracts table schema (single source of truth)
    * Connection management (writable, read-only with REGEXP)
    * Manifest generation
    * ANSI colour constants (dark-blue palette)
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

PROD_BASE = os.environ.get(
    "KALSHI_API_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
)
MAX_RETRIES = 3
BACKOFF_BASE = 2

DATA_DIR = os.environ.get(
    "MARKETGPR_DATA_DIR",
    os.path.join(os.getcwd(), "data"),
)
os.makedirs(DATA_DIR, exist_ok=True)

# Single source of truth for the contracts schema.  The first five columns are
# the original v2 layout and must keep their order; everything after is added
# by migration on existing databases, so all of it must be nullable.
#
# Snapshot vs static:  open_time, created_time, result and settlement_ts are
# properties of the contract and are correct whenever they are read.  The
# price/volume columns (yes_bid, yes_ask, last_price, volume, open_interest,
# liquidity) are a single observation taken at fetched_at -- they are NOT a
# time series, and for settled markets they are degenerate.  Use the
# candlestick endpoint to build price histories.
CONTRACT_COLUMNS = [
    ("ticker",        "TEXT PRIMARY KEY"),
    ("name",          "TEXT NOT NULL"),
    ("event_ticker",  "TEXT NOT NULL DEFAULT ''"),
    ("expiry_date",   "TEXT NOT NULL"),
    ("fetched_at",    "TEXT NOT NULL"),
    ("open_time",     "TEXT"),
    ("created_time",  "TEXT"),
    ("status",        "TEXT"),
    ("result",        "TEXT"),
    ("settlement_ts", "TEXT"),
    ("title",         "TEXT"),
    ("yes_bid",       "REAL"),
    ("yes_ask",       "REAL"),
    ("last_price",    "REAL"),
    ("volume",        "REAL"),
    ("volume_24h",    "REAL"),
    ("open_interest", "REAL"),
    ("liquidity",     "REAL"),
]

BASE_COLUMN_COUNT = 5  # ticker..fetched_at -- present in every schema version

CREATE_CONTRACTS = "CREATE TABLE IF NOT EXISTS contracts (\n    " + ",\n    ".join(
    f"{n:<14}{t}" for n, t in CONTRACT_COLUMNS
) + "\n)"

CREATE_EXPIRY_IDX  = "CREATE INDEX IF NOT EXISTS idx_expiry ON contracts(expiry_date)"
CREATE_OPEN_IDX    = "CREATE INDEX IF NOT EXISTS idx_open_time ON contracts(open_time)"
CREATE_RESULT_IDX  = "CREATE INDEX IF NOT EXISTS idx_result ON contracts(result)"

BOLD  = "\033[1m"
DIM   = "\033[2m"
RESET = "\033[0m"

BLUE        = "\033[34m"
BRIGHT_BLUE = "\033[94m"
CYAN        = "\033[36m"
GREEN       = "\033[32m"
YELLOW      = "\033[33m"
RED         = "\033[31m"
MAGENTA     = "\033[35m"


def header(text: str) -> str:
    return f"{BOLD}{BRIGHT_BLUE}{text}{RESET}"


def info(text: str) -> str:
    return f"{CYAN}{text}{RESET}"


def ok(text: str) -> str:
    return f"{GREEN}{text}{RESET}"


def warn(text: str) -> str:
    return f"{YELLOW}{text}{RESET}"


def err(text: str) -> str:
    return f"{RED}{text}{RESET}"


def accent(text: str) -> str:
    return f"{BLUE}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def bold(text: str) -> str:
    return f"{BOLD}{text}{RESET}"


def highlight(text: str) -> str:
    return f"{BOLD}{BRIGHT_BLUE}{text}{RESET}"


def migrate_contracts(conn: sqlite3.Connection) -> list[str]:
    """Add any columns from CONTRACT_COLUMNS that the table is missing.
    Returns the names added.  Existing data is preserved -- new columns are
    nullable and backfill on the next collect run."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()}
    added = []
    for name, decl in CONTRACT_COLUMNS[BASE_COLUMN_COUNT:]:
        if name not in cols:
            conn.execute(f"ALTER TABLE contracts ADD COLUMN {name} {decl}")
            added.append(name)
    if "event_ticker" not in cols:
        conn.execute(
            "ALTER TABLE contracts ADD COLUMN event_ticker TEXT NOT NULL DEFAULT ''"
        )
        added.append("event_ticker")
    return added


def init_db(db_path: str) -> sqlite3.Connection:
    """Create / open a writable SQLite connection with performance PRAGMAs.
    Migrates older schemas up to the current CONTRACT_COLUMNS layout."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute(CREATE_CONTRACTS)

    migrate_contracts(conn)

    conn.execute(CREATE_EXPIRY_IDX)
    conn.execute(CREATE_OPEN_IDX)
    conn.execute(CREATE_RESULT_IDX)

    conn.commit()
    return conn


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection.  REGEXP is registered on it."""
    abs_path = os.path.abspath(db_path)
    uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    _register_regex(conn)
    return conn


def _register_regex(conn: sqlite3.Connection) -> None:
    """Register a case-insensitive regexp() user function."""

    def _regexp(pattern: str, text: Optional[str]) -> bool:
        if text is None:
            return False
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False

    conn.create_function("REGEXP", 2, _regexp, deterministic=True)


def write_manifest(db_path: str, start_ts: int, end_ts: int,
                   duration: float, api_base: str) -> str:
    """Write collection_manifest.json into DATA_DIR.  Return path."""
    sha = hashlib.sha256()
    with open(db_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)

    conn = sqlite3.connect(db_path)
    row_count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    conn.close()

    manifest = {
        "api_base":         api_base,
        "start_ts":         start_ts,
        "end_ts":           end_ts,
        "start_iso":        datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        "end_iso":          datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        "total_rows":       row_count,
        "duration_seconds": round(duration, 1),
        "db_sha256":        sha.hexdigest(),
        "finished_at":      datetime.now(timezone.utc).isoformat(),
    }

    manifest_path = os.path.join(DATA_DIR, "collection_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest_path


def build_url(path: str, params: dict | None = None) -> str:
    """Build a full Kalshi API URL with query string."""
    url = f"{PROD_BASE}{path}"
    if params:
        cleaned = {k: v for k, v in params.items() if v is not None}
        if cleaned:
            qs = "&".join(f"{k}={v}" for k, v in cleaned.items())
            url = f"{url}?{qs}"
    return url


def fetch_json(url: str) -> dict:
    """HTTP GET with retry/backoff.  Returns parsed JSON dict."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429 or status >= 500:
                wait = BACKOFF_BASE ** attempt
                time.sleep(wait)
            else:
                raise
        except KeyboardInterrupt:
            raise
        except Exception:
            wait = BACKOFF_BASE ** attempt
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts")
