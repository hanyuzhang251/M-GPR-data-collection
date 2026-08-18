"""MarketGPR Info — Inspect a contract catalog database.

Reads a SQLite database of Kalshi contracts and prints metadata: file size,
schema version, columns, row count, date range, enrichment status, and more.
Read-only — never modifies anything.
"""

import argparse
import os
import sys

from marketgpr.db import (accent, bold, dim, header, highlight, info,
                          ok, warn, connect_readonly)


def fmt_bytes(n: int) -> str:
    """Format byte count into human-readable units."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def _has_column(conn, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def inspect_database(db_path: str) -> None:
    p = lambda s: print(s, flush=True)

    p("")
    p(header("=== MarketGPR  ·  Database Info ==="))
    p("")

    # ── File ──
    p(bold("File"))
    p(accent(f"  Path:  {db_path}"))
    size = os.path.getsize(db_path)
    p(accent(f"  Size:  {fmt_bytes(size)}"))

    conn = connect_readonly(db_path)

    # ── Schema ──
    p("")
    p(bold("Schema"))

    has_event = _has_column(conn, "contracts", "event_ticker")
    has_open  = _has_column(conn, "contracts", "open_time")
    if has_open:
        p(ok(f"  Version:  v3 (listing dates, resolutions and price snapshot)"))
    elif has_event:
        p(warn(f"  Version:  v2 (event_ticker only — no open_time/result/prices)"))
        p(dim(f"            Re-collect with 'marketgpr collect' to backfill; "
              f"enriched names are preserved."))
    else:
        p(warn(f"  Version:  v1 (no event_ticker column)"))
        p(dim(f"            Re-collect with 'marketgpr collect' for enrichment support."))

    cols = conn.execute("PRAGMA table_info(contracts)").fetchall()
    p(accent(f"  Columns:  {', '.join(r[1] for r in cols)}"))

    # ── Coverage of the v3 columns (blank until a re-collect backfills them) ──
    if has_open:
        total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        if total:
            p("")
            p(bold("Field coverage"))
            for col, label in (("open_time", "Listing date"),
                               ("result", "Resolution"),
                               ("yes_bid", "Price snapshot")):
                n = conn.execute(
                    f"SELECT COUNT(*) FROM contracts WHERE {col} IS NOT NULL AND {col} != ''"
                ).fetchone()[0]
                pct = 100.0 * n / total
                line = f"  {label:<16}{n:>12,} / {total:,}  ({pct:.1f}%)"
                p(ok(line) if pct > 50 else warn(line))
            p(dim("  Prices are a snapshot at fetch time, not a series."))

    # ── Rows ──
    total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    p("")
    p(bold("Rows"))
    p(info(f"  Total contracts:  {total:,}"))

    if total == 0:
        conn.close()
        p("")
        p(dim("  Database is empty."))
        p("")
        return

    # ── Date range ──
    dr = conn.execute(
        "SELECT MIN(expiry_date), MAX(expiry_date) FROM contracts"
    ).fetchone()
    if dr and dr[0]:
        p(accent(f"  Expiry range:     {_format_ts(dr[0])}  →  {_format_ts(dr[1])}"))

    fetch_range = conn.execute(
        "SELECT MIN(fetched_at), MAX(fetched_at) FROM contracts"
    ).fetchone()
    p(accent(f"  Fetched at:        {_format_ts(fetch_range[0])}  →  {_format_ts(fetch_range[1])}"))

    # ── Events ──
    if has_event:
        p("")
        p(bold("Events"))
        event_count = conn.execute(
            "SELECT COUNT(DISTINCT event_ticker) FROM contracts WHERE event_ticker != ''"
        ).fetchone()[0]
        p(accent(f"  Unique events:  {event_count:,}"))

    # ── Enrichment ──
    p("")
    p(bold("Enrichment"))
    enriched = conn.execute(
        "SELECT COUNT(*) FROM contracts WHERE name != ticker"
    ).fetchone()[0]
    pct = (enriched / total * 100) if total else 0
    if pct > 0:
        p(ok(f"  Enriched:  {enriched:,} / {total:,}  ({pct:.1f}%)"))
    else:
        p(warn(f"  Not enriched — all names match tickers"))
        p(dim(f"  Run 'marketgpr enrich --db {db_path}' to fetch event titles."))

    # ── Sample rows ──
    p("")
    p(bold("Sample rows"))
    rows = conn.execute(
        "SELECT ticker, name FROM contracts ORDER BY expiry_date DESC LIMIT 5"
    ).fetchall()
    for ticker, name in rows:
        t = (ticker + " " * 60)[:50]
        p(dim(f"  {t}") + name)

    conn.close()
    p("")


def _format_ts(value: str | None) -> str:
    """Truncate ISO timestamps to YYYY-MM-DD for display."""
    if not value:
        return "?"
    return value[:10]


def register_args(parser: argparse.ArgumentParser):
    parser.add_argument("--db", required=True,
                        help="Path to SQLite database to inspect")


def run(args: argparse.Namespace):
    p = lambda s: print(s, flush=True)

    if not os.path.isfile(args.db):
        p(warn(f"Error: database not found: {args.db}"))
        sys.exit(1)

    inspect_database(args.db)
