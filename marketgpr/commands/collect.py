"""Kalshi Contract Catalog Builder.

Collects every Kalshi contract (ticker, name, event ticker, expiry date)
within a specified date range from the public API.  Uses both the live and
historical endpoints to cover the full time window.  Outputs a SQLite database
suitable as a lightweight index for downstream deep-data puller scripts.

Streams data page-by-page into SQLite to keep memory use low regardless
of dataset size.  Event-title enrichment is handled separately by the
``marketgpr enrich`` command.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from marketgpr.db import (DATA_DIR, PROD_BASE,
                          accent, dim, header, highlight, info, init_db, ok, warn,
                          write_manifest, build_url, fetch_json)

DEFAULT_START = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
DEFAULT_END   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
PAGE_LIMIT    = 1000
DELAY_SECONDS = 0.1
DB_PATH       = os.path.join(DATA_DIR, "kalshi_catalog.db")
LOG_PATH      = os.path.join(DATA_DIR, "collection.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("collect")


def parse_date(value: str) -> int:
    """Accept ISO date (YYYY-MM-DD) or Unix timestamp, return Unix seconds."""
    try:
        return int(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(value, fmt)
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Unrecognized date format: {value}")


INSERT_COLUMNS = [
    "ticker", "name", "event_ticker", "expiry_date", "fetched_at",
    "open_time", "created_time", "status", "result", "settlement_ts",
    "title", "yes_bid", "yes_ask", "last_price", "volume", "volume_24h",
    "open_interest", "liquidity",
]

# On re-run, refresh everything except ticker (the key) and name -- name holds
# the enriched event title and must survive a re-collect untouched.
_UPDATE_COLUMNS = [c for c in INSERT_COLUMNS if c not in ("ticker", "name")]

UPSERT_SQL = (
    f"INSERT INTO contracts({','.join(INSERT_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(INSERT_COLUMNS))}) "
    f"ON CONFLICT(ticker) DO UPDATE SET "
    + ",".join(f"{c}=excluded.{c}" for c in _UPDATE_COLUMNS)
)


def _num(market: dict, key: str):
    """Kalshi returns numerics as strings ('0.0040').  Empty -> None."""
    raw = market.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def stream_insert(conn, markets: list[dict], fetched_at: str) -> tuple[int, int]:
    """Upsert a page of markets.  On first insert, name is set to the ticker as
    a placeholder; enrichment fills it in later and later runs preserve it.

    Price and volume columns are a point-in-time snapshot as of fetched_at,
    not a series.  open_time, created_time, result and settlement_ts are
    static properties of the contract.

    Returns (rows_written, rows_newly_inserted)."""
    rows = []
    for m in markets:
        t = m.get("ticker")
        if not t:
            continue
        close = m.get("close_time", "") or m.get("expected_expiration_time", "") or ""
        rows.append((
            t, t,
            m.get("event_ticker", ""),
            close,
            fetched_at,
            m.get("open_time") or None,
            m.get("created_time") or None,
            m.get("status") or None,
            m.get("result") or None,
            m.get("settlement_ts") or None,
            m.get("title") or None,
            _num(m, "yes_bid_dollars"),
            _num(m, "yes_ask_dollars"),
            _num(m, "last_price_dollars"),
            _num(m, "volume_fp"),
            _num(m, "volume_24h_fp"),
            _num(m, "open_interest_fp"),
            _num(m, "liquidity_dollars"),
        ))
    if not rows:
        return 0, 0
    before = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    return len(rows), after - before


def get_cutoff() -> float:
    data = fetch_json(build_url("/historical/cutoff"))
    ts = data.get("market_settled_ts")
    if ts is None:
        raise RuntimeError("market_settled_ts missing from /historical/cutoff response")
    if isinstance(ts, (int, float)):
        cutoff = float(ts)
    else:
        cutoff = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    log.info("Historical cutoff: %s (%s)",
             cutoff, datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat())
    return cutoff


def _parse_close_ts(market: dict) -> float:
    raw = market.get("close_time", "")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def collect_live(conn, start_ts: int, end_ts: int,
                 fetched_at: str, delay: float) -> int:
    """Stream live markets into the DB.  Returns number newly inserted."""
    inserted = 0
    written = 0
    cursor: str | None = None
    page = 0
    while True:
        params: dict = {"limit": PAGE_LIMIT,
                        "min_close_ts": start_ts,
                        "max_close_ts": end_ts,
                        "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = fetch_json(build_url("/markets", params))
        items = data.get("markets", [])
        page += 1
        cursor = data.get("cursor")
        w, n = stream_insert(conn, items, fetched_at)
        inserted += n
        written += w
        if page % 10 == 0 or not cursor:
            log.info("Live: page %4s  |  %4s items  |  %6s new  |  %6s refreshed  |  total_in_db %s",
                     page, len(items), n, w - n,
                     conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])
        if not cursor:
            break
        time.sleep(delay)
    log.info("Live complete — %s pages, %s new, %s refreshed",
             page, inserted, written - inserted)
    return inserted


def collect_historical(conn, start_ts: int,
                       fetched_at: str, delay: float) -> int:
    """Stream historical markets into the DB.  Because the historical endpoint
    has no date filters, we paginate through *all* archived markets and stop
    when close_time drops below start_ts (results are ordered desc by date)."""
    inserted = 0
    written = 0
    cursor: str | None = None
    page = 0
    while True:
        params: dict = {"limit": PAGE_LIMIT, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = fetch_json(build_url("/historical/markets", params))
        items = data.get("markets", [])
        page += 1
        cursor = data.get("cursor")

        filtered = [m for m in items if _parse_close_ts(m) >= start_ts]
        skipped = len(items) - len(filtered)
        w, n = stream_insert(conn, filtered, fetched_at)
        inserted += n
        written += w
        earliest = min((_parse_close_ts(m) for m in items), default=0)

        if page % 10 == 0 or not cursor:
            log.info("Hist: page %4s  |  %4s items  |  kept %4s  |  %6s new  |  earliest %s  |  total_in_db %s",
                     page, len(items), len(filtered), n,
                     datetime.fromtimestamp(earliest, tz=timezone.utc).strftime("%Y-%m-%d") if earliest else "?",
                     conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])

        if not cursor:
            break
        if skipped == len(items) and len(items) > 0:
            log.info("Historical: all remaining markets are before %s, stopping",
                     datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d"))
            break
        time.sleep(delay)

    log.info("Historical complete — %s pages, %s new, %s refreshed",
             page, inserted, written - inserted)
    return inserted


def register_args(parser: argparse.ArgumentParser):
    parser.add_argument("--start", type=parse_date, default=parse_date(DEFAULT_START),
                        help="Beginning of collection window (ISO date or Unix timestamp)")
    parser.add_argument("--end",   type=parse_date, default=parse_date(DEFAULT_END),
                        help="End of collection window (ISO date or Unix timestamp)")
    parser.add_argument("--db",    default=DB_PATH,
                        help="Path to output SQLite database")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS,
                        help="Pause between API page requests (seconds)")


def run(args: argparse.Namespace):
    p = lambda s: print(s, flush=True)

    delay = args.delay
    start_ts = int(args.start)
    end_ts   = int(args.end)

    p("")
    p(header("=== MarketGPR  ·  Contract Catalog Builder ==="))
    p(accent(f"API:      {PROD_BASE}"))
    p(accent(f"Range:    {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d')}"
             f"  →  {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d')}"))
    p(accent(f"DB:       {args.db}"))
    p(accent(f"Delay:    {delay}s/page"))
    p("")

    started = time.time()
    conn = init_db(args.db)
    existing = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    p(info(f"Existing rows in DB: {existing:,}"))

    cutoff = get_cutoff()
    fetched_at = datetime.now(timezone.utc).isoformat()
    total_inserted = 0

    if end_ts >= cutoff:
        live_start = max(start_ts, int(cutoff))
        live_end   = end_ts
        p("")
        p(highlight(">>> Phase 1: LIVE   "
                    f"{datetime.fromtimestamp(live_start, tz=timezone.utc).strftime('%Y-%m-%d')}"
                    f" → {datetime.fromtimestamp(live_end, tz=timezone.utc).strftime('%Y-%m-%d')}"))
        total_inserted += collect_live(conn, live_start, live_end, fetched_at, delay)

    if start_ts < cutoff:
        p("")
        p(highlight(f">>> Phase 2: HIST  "
                    f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d')}"
                    f" → cutoff"))
        total_inserted += collect_historical(conn, start_ts, fetched_at, delay)

    row_count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    conn.close()

    duration = time.time() - started

    p("")
    p(ok(f"Done: {row_count:,} total rows  |  {total_inserted:,} inserted this run  |  {duration:.1f}s"))
    manifest_path = write_manifest(args.db, start_ts, end_ts, duration, PROD_BASE)
    p(dim(f"Manifest: {manifest_path}"))
    p("")
