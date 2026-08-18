# MarketGPR

**Single CLI for building and filtering a Kalshi prediction-market contract index — designed for geopolitical risk research.**

---

## Quick start

```bash
# 1. Install the package
pip install .

# 2. Collect contracts from the Kalshi API into a SQLite catalog
marketgpr collect --start 2026-01-01 --end 2026-07-01

# 3. Filter an existing catalog by keywords and regex
marketgpr clean --db data/kalshi_catalog.db --config filters.json

# 4. Dry-run to preview what would be removed
marketgpr clean --db data/kalshi_catalog.db --config filters.json --dry-run
```

See `marketgpr collect --help` or `marketgpr clean --help` for all options.

---

## Commands

### `marketgpr collect` — build the catalog

Pulls every Kalshi contract (ticker, name, expiry date, event ticker) from both
the live and historical API endpoints and writes them into a SQLite database.

```bash
# Full 2-year window (default)
marketgpr collect

# Custom date range
marketgpr collect --start 2026-01-01 --end 2026-07-01

# Unix timestamps work too
marketgpr collect --start 1700000000 --end 1710000000

# Custom output path
marketgpr collect --db my_catalog.db

# Skip event-title enrichment (faster, leaves name as event_ticker)
marketgpr collect --no-enrich

# Rate-limit delay between API pages (seconds)
marketgpr collect --delay 0.5
```

| Arg | Default | Description |
|-----|---------|-------------|
| `--start` | 2 years ago | Begin of collection window (YYYY-MM-DD or Unix timestamp) |
| `--end` | today | End of collection window |
| `--db` | `data/kalshi_catalog.db` | Output SQLite database path |
| `--delay` | `0.1` | Pause between API pages (seconds) |
| `--no-enrich` | false | Skip event-title enrichment pass |

**Pipeline phases:**
| Phase | Source | Covers |
|-------|--------|--------|
| **Live** | `GET /markets` | Contracts closing in the ~90-day live window |
| **Historical** | `GET /historical/markets` | Archived (settled) contracts |
| **Enrich** (optional) | `GET /events` | Replaces ticker placeholders with event titles |

Collection is idempotent — `INSERT OR IGNORE` means partial runs resume cleanly.

### `marketgpr clean` — filter the catalog

Filters an existing catalog by keywords and regex patterns. Writes matching rows
to a new database — the original is never modified.

```bash
# Use full config file
marketgpr clean --db data/kalshi_catalog.db --config filters.json

# Inline keywords — keep rows matching ANY keep keyword, drop rows matching ANY remove keyword
marketgpr clean --db data/kalshi_catalog.db --keep "war,election" --remove "NBA,NFL"

# Field-specific filters
marketgpr clean --db data/kalshi_catalog.db --ticker-keep "GOV" --ticker-remove "MENTION"

# Regex patterns
marketgpr clean --db data/kalshi_catalog.db --keep-regex "war|conflict" --remove-regex "above \\d+"

# Dry run — preview counts and samples without writing
marketgpr clean --db data/kalshi_catalog.db --config filters.json --dry-run

# Custom output path
marketgpr clean --db data/kalshi_catalog.db --output cleaned.db

# Skip confirmation prompt
marketgpr clean --db data/kalshi_catalog.db --config filters.json -y
```

| Arg | Default | Description |
|-----|---------|-------------|
| `--db` | *(required)* | Input SQLite database |
| `--config` | *(none)* | JSON file with keep/remove rules |
| `--keep` | *(none)* | Comma-separated keywords — keep rows matching ANY (name LIKE) |
| `--remove` | *(none)* | Comma-separated keywords — drop rows matching ANY (name LIKE) |
| `--keep-regex` | *(none)* | Regex patterns — keep rows matching ANY (name REGEXP) |
| `--remove-regex` | *(none)* | Regex patterns — drop rows matching ANY (name REGEXP) |
| `--ticker-keep` | *(none)* | Keywords for ticker column — keep (ticker LIKE) |
| `--ticker-remove` | *(none)* | Keywords for ticker column — drop (ticker LIKE) |
| `--ticker-keep-regex` | *(none)* | Regex for ticker column — keep (ticker REGEXP) |
| `--ticker-remove-regex` | *(none)* | Regex for ticker column — drop (ticker REGEXP) |
| `--output` | `<input>_cleaned.db` | Output database path |
| `--dry-run` | false | Preview counts and samples without writing |
| `-y` / `--yes` | false | Skip confirmation prompt |

---

## Filter config format (`filters.json`)

```json
{
  "keep": [],
  "remove": [
    "NBA",
    {"pattern": "above \\d+\\.\\d+", "type": "regex"},
    {"pattern": "MENTION", "field": "ticker"}
  ]
}
```

| Entry form | Field | Match type | SQL equivalent |
|-----------|-------|-----------|----------------|
| `"Bitcoin"` | name | LIKE | `name LIKE '%Bitcoin%'` |
| `{"pattern":"BTC"}` | name | LIKE | `name LIKE '%BTC%'` |
| `{"pattern":"above \\d+\\.\\d+","type":"regex"}` | name | REGEXP | `name REGEXP 'above \\d+\\.\\d+'` |
| `{"pattern":"MENTION","field":"ticker"}` | ticker | LIKE | `ticker LIKE '%MENTION%'` |
| `{"pattern":"^KX.*T$","field":"ticker","type":"regex"}` | ticker | REGEXP | `ticker REGEXP '^KX.*T$'` |

All matching is case-insensitive. Inline keywords from CLI flags are merged with
the config file entries.

---

## Built-in filter categories

`filters.json` ships with 76 exclusion patterns across 7 categories:

| Category | Examples |
|----------|----------|
| Mention Markets | ticker `MENTION`, regex `What will .+ say` |
| Sports | NBA, NFL, MLB, NHL, UFC, ATP, WTA, F1, Valorant, CS2 |
| Crypto | Bitcoin, Ethereum, BTC, ETH, SOL, DOGE, NFT, DeFi |
| Entertainment | Oscar, Grammy, Netflix, Spotify, TikTok, album, concert |
| Weather | hurricane, tornado, earthquake, temperature, snowfall |
| Commodities | WTI, Brent, gold, silver, crude oil, settlement price |
| Financial | CPI, PPI, GDP, unemployment, NASDAQ, VIX, SOFR |

---

## Database schema

```sql
contracts (
    ticker         TEXT PRIMARY KEY, -- e.g. KXBTC-26JUN21-T100000
    name           TEXT NOT NULL,    -- event title after enrichment
    event_ticker   TEXT NOT NULL,    -- e.g. KXBTC-26JUN21
    expiry_date    TEXT NOT NULL,    -- close_time, e.g. "2026-06-21T12:00:00Z"
    fetched_at     TEXT NOT NULL,    -- when this row was last written

    -- static contract properties (correct whenever read)
    open_time      TEXT,             -- LISTING date — when Kalshi opened it
    created_time   TEXT,
    status         TEXT,             -- active / finalized / settled
    result         TEXT,             -- 'yes' / 'no' once settled
    settlement_ts  TEXT,
    title          TEXT,             -- market-level (strike) title

    -- POINT-IN-TIME SNAPSHOT as of fetched_at — not a time series
    yes_bid        REAL,
    yes_ask        REAL,
    last_price     REAL,
    volume         REAL,
    volume_24h     REAL,
    open_interest  REAL,
    liquidity      REAL
)

INDEX idx_expiry ON contracts(expiry_date)
INDEX idx_open_time ON contracts(open_time)
INDEX idx_result ON contracts(result)
```

**`expiry_date` vs `open_time`.** `expiry_date` is when a contract *resolves*;
`open_time` is when it was *listed*. Frequency measures in the style of
Caldara-Iacoviello count documents by publication date, which is `open_time`.
Binning on `expiry_date` answers a different question.

**Prices are a snapshot, not a series.** The price and volume columns record a
single observation taken at `fetched_at`. For settled markets they are
degenerate (bid/ask collapse to 0/1). Building a price history requires
repeated snapshots or the candlestick endpoint.

**Schema versions.** v1 had no `event_ticker`; v2 added it; v3 adds everything
below `fetched_at`. `marketgpr info` reports the version. Re-running
`marketgpr collect` on a v1/v2 database migrates it in place and backfills the
new columns by upsert — **enriched `name` values are preserved**, so a
re-collect does not cost you an enrichment pass.

---

## File structure

```
M-GPR-data-collection/
├── pyproject.toml                # Package metadata + entry point
├── marketgpr/
│   ├── __init__.py
│   ├── __main__.py               # python -m marketgpr
│   ├── cli.py                    # CLI entry point (argparse subcommands)
│   ├── db.py                     # Shared module (schema, connections, logging)
│   └── commands/
│       ├── collect.py            # Catalog builder (Kalshi API → DB)
│       └── clean.py              # Contract cleaner (keyword + regex filters)
├── filters.json                  # Curated exclusion rules (76 patterns)
├── CLI_GUIDE.md                  # Detailed CLI walkthrough
├── data/                         # Runtime output (DBs, logs, manifests)
│   └── .gitkeep
├── README.md
└── .gitignore
```

---

## Requirements

- Python 3.10+ (zero external dependencies — stdlib only)

Scale estimates for collection:

| Range | Est. rows | Est. DB size | Est. time | Est. traffic |
|-------|-----------|-------------|-----------|-------------|
| 1 day | ~40,000 | ~13 MB | ~40 s | ~50 MB |
| 1 month | ~1.2M | ~400 MB | ~20 min | ~350 MB |
| 1 year | ~15M | ~4.5 GB | ~4 hours | ~7.5 GB |
| 2 years | ~30M | ~9 GB | ~8 hours | ~15 GB |

MVE (multivariate event) combo markets are excluded by default — they account for ~89% of raw API volume.
