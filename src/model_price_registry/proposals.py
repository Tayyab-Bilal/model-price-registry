"""The approval queue. Automation files drafts; only `approve` moves a price."""
import json
import sqlite3
from decimal import Decimal

from .db import rates_to_json, read_rates, tx, utcnow_iso
from .registry import Registry, apply_update
from .validation import PriceUpdate, validate_rates

# Only "consensus never overrides a provider-page draft" comes from the original design. Putting
# the API between the two is this repo's own choice: it is a first-party number, but it follows
# the provider's default route, so a page draft still outranks it.
STRENGTH = {"consensus": 1, "api": 2, "provider_page": 3}
MAX_BULK = 200


class StaleProposal(Exception):
    """Rates changed after the draft was filed; the human reviewed numbers that no longer hold."""


class DecisionError(Exception):
    """Decisions are final."""


def file_proposal(reg: Registry, model_id: str, rates: dict[str, Decimal | None],
                  source: str) -> tuple[str, int]:
    """Returns (outcome, id); outcome is filed | replaced | kept. A pending draft is replaced only
    by a stronger source, so a noisy source can't overwrite a better one's draft."""
    conn = reg.conn
    with tx(conn):
        snapshot = read_rates(conn, model_id)
        validate_rates({**snapshot, **rates})
        pending = conn.execute(
            "SELECT id, source FROM proposals WHERE model_id=? AND status='pending'", (model_id,)
        ).fetchone()
        if pending is None:
            cur = conn.execute(
                "INSERT INTO proposals(model_id, proposed_json, snapshot_json, source, created_at)"
                " VALUES (?,?,?,?,?)",
                (model_id, rates_to_json(rates), rates_to_json(snapshot), source, utcnow_iso()),
            )
            return "filed", cur.lastrowid
        if STRENGTH[source] <= STRENGTH[pending["source"]]:
            return "kept", pending["id"]
        conn.execute(
            "UPDATE proposals SET proposed_json=?, snapshot_json=?, source=?, created_at=?"
            " WHERE id=?",
            (rates_to_json(rates), rates_to_json(snapshot), source, utcnow_iso(), pending["id"]),
        )
        return "replaced", pending["id"]


def _load_pending(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise KeyError(f"no proposal {proposal_id}")
    if row["status"] != "pending":
        raise DecisionError(f"proposal {proposal_id} already {row['status']}")
    return row


def _decide(conn: sqlite3.Connection, proposal_id: int, status: str, actor: str) -> None:
    conn.execute("UPDATE proposals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                 (status, actor, utcnow_iso(), proposal_id))


def approve(reg: Registry, proposal_id: int, actor: str) -> None:
    conn = reg.conn
    with tx(conn):
        row = _load_pending(conn, proposal_id)
        current = json.loads(rates_to_json(read_rates(conn, row["model_id"])))
        if current != json.loads(row["snapshot_json"]):
            raise StaleProposal(f"rates for {row['model_id']} changed since proposal {proposal_id}")
        proposed = {k: None if v is None else Decimal(v)
                    for k, v in json.loads(row["proposed_json"]).items()}
        update = PriceUpdate(reason=f"approved proposal {proposal_id} from {row['source']}",
                             **proposed)
        apply_update(conn, row["model_id"], update, actor, "sync")
        _decide(conn, proposal_id, "approved", actor)
    reg.cache.invalidate(row["model_id"])


def reject(reg: Registry, proposal_id: int, actor: str) -> None:
    with tx(reg.conn):
        _load_pending(reg.conn, proposal_id)
        _decide(reg.conn, proposal_id, "rejected", actor)


def bulk_decide(reg: Registry, ids: list[int], decision: str, actor: str) -> dict[int, str]:
    """One transaction per proposal: a stale draft must not roll back the others. Capped at
    MAX_BULK ids so one call cannot hold the write lock for long."""
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    if len(ids) > MAX_BULK:
        raise ValueError(f"at most {MAX_BULK} proposals per call, got {len(ids)}")
    outcomes: dict[int, str] = {}
    for pid in ids:
        try:
            (approve if decision == "approve" else reject)(reg, pid, actor)
            outcomes[pid] = "approved" if decision == "approve" else "rejected"
        except StaleProposal:
            outcomes[pid] = "stale"
        except (DecisionError, KeyError, ValueError) as e:
            outcomes[pid] = f"error: {e}"
    return outcomes


def list_pending(reg: Registry) -> list[sqlite3.Row]:
    return reg.conn.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY id").fetchall()
