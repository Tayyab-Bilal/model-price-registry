"""Cached-rate backfill.

A model with no cached rate bills cached tokens at the full input rate (see `Registry.cost_for`),
which overcharged by a large factor on models whose cached price is a fraction of input. This fills
the gap on databases created before the column was populated.
"""
import sqlite3
from decimal import Decimal

from .db import read_rates, to_micro, tx, utcnow_iso
from .registry import Rates, _write_history


def backfill_cached_rates(conn: sqlite3.Connection, defaults: dict[str, Decimal]) -> int:
    """Fill a missing cached rate from `defaults`; returns how many rows changed.

    Guards, each there because skipping it would corrupt a deliberate price:
    - only rows still at `migration` provenance: an admin who cleared a cached rate meant it;
    - only rows whose cached rate is NULL: an existing value is never overwritten;
    - only token-billed rows, and only when the default does not exceed the input rate.
    Safe to run twice: the second run finds nothing to fill.
    """
    changed = 0
    for model_id, cached in defaults.items():
        with tx(conn):
            row = conn.execute("SELECT provenance, cached_input_per_mtok, input_per_mtok FROM models"
                               " WHERE model_id=?", (model_id,)).fetchone()
            if (row is None or row["provenance"] != "migration" or row["cached_input_per_mtok"] is not None
                    or row["input_per_mtok"] is None or to_micro(cached) > row["input_per_mtok"]
                    or to_micro(cached) == 0):
                continue
            before: Rates = read_rates(conn, model_id)
            conn.execute("UPDATE models SET cached_input_per_mtok=?, updated_at=? WHERE model_id=?",
                         (to_micro(cached), utcnow_iso(), model_id))
            _write_history(conn, model_id, before, read_rates(conn, model_id),
                           "backfill missing cached rate", "backfill", "migration")
            changed += 1
    return changed


def downgrade_backfill(conn: sqlite3.Connection) -> int:
    """Deliberately a no-op. Undoing the backfill would put NULLs back and silently restore the
    overcharge, and it could not tell filled values from ones a human later confirmed. A rollback of
    the release therefore leaves the data alone."""
    return 0
