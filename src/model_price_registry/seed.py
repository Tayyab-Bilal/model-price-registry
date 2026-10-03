"""Startup seed. Insert-only: a restart must never revert a price a human or sync changed."""
import sqlite3
from decimal import Decimal as D

from .db import RATE_FIELDS, to_micro, utcnow_iso

# (model_id, provider, input, output, cached_input, per_minute)
SEED_MODELS = [
    ("acme-chat-large", "acme", D("3.00"), D("15.00"), D("0.30"), None),
    ("acme-chat-small", "acme", D("0.25"), D("1.25"), D("0.03"), None),
    ("acme-code-pro", "acme", D("2.00"), D("8.00"), D("0.50"), None),
    ("acme-embed-v2", "acme", D("0.10"), None, None, None),
    ("acme-rerank-1", "acme", D("0.05"), None, None, None),
    ("acme-live-1", "acme", D("4.00"), D("16.00"), D("0.40"), None),  # realtime: text rates only
    ("acme-listen-1", "acme", None, None, None, D("0.006")),
]


def seed(conn: sqlite3.Connection, models=SEED_MODELS) -> int:
    """Returns how many rows were inserted. ON CONFLICT DO NOTHING is the whole safety story."""
    inserted = 0
    for model_id, provider, *rates in models:
        cur = conn.execute(
            f"INSERT INTO models(model_id, provider, {', '.join(RATE_FIELDS)}, provenance,"
            " updated_at) VALUES (?,?,?,?,?,?, 'migration', ?) ON CONFLICT(model_id) DO NOTHING",
            (model_id, provider, *(to_micro(r) for r in rates), utcnow_iso()),
        )
        inserted += cur.rowcount
    return inserted
