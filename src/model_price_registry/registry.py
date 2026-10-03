"""The single audited write path (`apply_update`) plus the rate cache billing reads through."""
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from .db import (
    RATE_FIELDS,
    from_micro,
    rates_to_json,
    read_rates,
    to_micro,
    tx,
    utcnow_iso,
)
from .validation import PriceUpdate, validate_rates

Rates = dict[str, Decimal | None]


@dataclass(frozen=True)
class Model:
    model_id: str
    provider: str
    rates: Rates
    provenance: str
    updated_at: str


class RateCache:
    """Per-process TTL cache. Without explicit invalidation a price change would take up to
    `ttl` seconds to reach billing, so every write path calls `invalidate`."""

    def __init__(self, load: Callable[[str], Rates], ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._load, self._ttl, self._clock = load, ttl, clock
        self._entries: dict[str, tuple[float, Rates]] = {}

    def get(self, model_id: str) -> Rates:
        hit = self._entries.get(model_id)
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        rates = self._load(model_id)
        self._entries[model_id] = (self._clock(), rates)
        return rates

    def invalidate(self, model_id: str) -> None:
        self._entries.pop(model_id, None)


def _write_history(conn: sqlite3.Connection, model_id: str, before: Rates, after: Rates,
                   reason: str, actor: str, source: str) -> None:
    conn.execute(
        "INSERT INTO price_history(model_id, before_json, after_json, reason, actor, source,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (model_id, rates_to_json(before), rates_to_json(after), reason, actor, source, utcnow_iso()),
    )


def apply_update(conn: sqlite3.Connection, model_id: str, update: PriceUpdate, actor: str,
                 provenance: str) -> None:
    """Caller owns the transaction: rate change and history row commit or roll back together."""
    before = read_rates(conn, model_id)
    after = {**before, **{f: getattr(update, f) for f in update.model_fields_set if f in RATE_FIELDS}}
    validate_rates(after)
    sets = ", ".join(f"{f}=?" for f in RATE_FIELDS)
    conn.execute(
        f"UPDATE models SET {sets}, provenance=?, updated_at=? WHERE model_id=?",
        (*(to_micro(after[f]) for f in RATE_FIELDS), provenance, utcnow_iso(), model_id),
    )
    _write_history(conn, model_id, before, after, update.reason, actor, provenance)


class Registry:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.monotonic,
                 ttl: float = 60.0):
        self.conn = conn
        self.cache = RateCache(lambda m: read_rates(conn, m), ttl, clock)

    def get_model(self, model_id: str) -> Model:
        row = self.conn.execute("SELECT * FROM models WHERE model_id=?", (model_id,)).fetchone()
        if row is None:
            raise KeyError(model_id)
        rates = {f: from_micro(row[f]) for f in RATE_FIELDS}
        return Model(model_id, row["provider"], rates, row["provenance"], row["updated_at"])

    def update_price(self, model_id: str, update: PriceUpdate, actor: str) -> Model:
        with tx(self.conn):
            apply_update(self.conn, model_id, update, actor, "admin")
        self.cache.invalidate(model_id)
        return self.get_model(model_id)

    def cost_for(self, model_id: str, input_tokens: int, output_tokens: int,
                 cached_tokens: int = 0) -> Decimal:
        """USD for one call; this is what billing calls. `input_tokens` excludes cached ones."""
        r = self.cache.get(model_id)
        if r["input_per_mtok"] is None:
            raise ValueError(f"{model_id} has no per-token rates")
        cached_rate = r["cached_input_per_mtok"]
        if cached_rate is None:
            cached_rate = r["input_per_mtok"]
        out_rate = r["output_per_mtok"] or Decimal(0)
        total = (input_tokens * r["input_per_mtok"] + output_tokens * out_rate
                 + cached_tokens * cached_rate)
        return total / Decimal(1_000_000)
