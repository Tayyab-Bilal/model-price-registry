"""SQLite schema and money helpers.

Money is stored as integer micro-dollars (1e-6 USD) per million tokens / per minute. Integers make
equality exact (snapshots, "did the rate change?") with no float drift; Decimal is used at the edges.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

RATE_FIELDS = ("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "per_minute")
TOKEN_FIELDS = RATE_FIELDS[:3]
MICRO = Decimal(1_000_000)

SCHEMA = """
CREATE TABLE IF NOT EXISTS models(
  model_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  input_per_mtok INTEGER, output_per_mtok INTEGER,
  cached_input_per_mtok INTEGER, per_minute INTEGER,
  provenance TEXT NOT NULL DEFAULT 'migration' CHECK (provenance IN ('migration','admin','sync')),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
  reason TEXT NOT NULL, actor TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL, proposed_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  decided_by TEXT, decided_at TEXT, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON proposals(model_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
  started_at TEXT NOT NULL, finished_at TEXT, error TEXT
);
"""


def connect(path: str = ":memory:", read_only: bool = False) -> sqlite3.Connection:
    """Autocommit connection that creates the file and schema if missing (not when read-only).
    Transactions are explicit via `tx` so BEGIN IMMEDIATE is ours."""
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    else:
        # Simplification: one shared connection (check_same_thread off) serves the async API, whose
        # handlers run on a single event-loop thread; use a connection per request on a real pool.
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so read-check-write is race free."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def to_micro(v: Decimal | None) -> int | None:
    # Simplification: rounds half-up at 6 decimals, so sub-micro-dollar rates are rejected by
    # validation rather than stored; widen the scale if rates ever need more precision.
    return None if v is None else int((v * MICRO).to_integral_value(ROUND_HALF_UP))


def from_micro(i: int | None) -> Decimal | None:
    return None if i is None else Decimal(i) / MICRO


def read_rates(conn: sqlite3.Connection, model_id: str) -> dict[str, Decimal | None]:
    row = conn.execute("SELECT * FROM models WHERE model_id=?", (model_id,)).fetchone()
    if row is None:
        raise KeyError(model_id)
    return {k: from_micro(row[k]) for k in RATE_FIELDS}


def rates_to_json(rates: dict[str, Decimal | None]) -> str:
    return json.dumps({k: None if v is None else f"{v:.6f}" for k, v in rates.items()})
