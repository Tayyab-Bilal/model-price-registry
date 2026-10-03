"""Admin and trigger HTTP API (FastAPI). Every route needs the injected admin check.

The router is a thin shell: it translates domain errors to HTTP and calls the same functions the
tests and the CLI call. PATCH on a model's pricing is the only route that writes a price directly;
approving a proposal goes through the same audited `apply_update`.
"""
import json
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .proposals import DecisionError, StaleProposal, approve, bulk_decide, reject
from .registry import Model, Registry
from .sources.common import Source
from .sync import Report, SyncAlreadyRunning, run_sync
from .validation import PriceUpdate

AdminCheck = Callable[..., str]  # a FastAPI dependency: returns the actor name or raises 401/403


class BulkRequest(BaseModel):
    ids: list[int]


def _model_json(m: Model) -> dict:
    return {"model_id": m.model_id, "provider": m.provider, "provenance": m.provenance,
            "updated_at": m.updated_at,
            "rates": {k: None if v is None else str(v) for k, v in m.rates.items()}}


def _report_json(r: Report) -> dict:
    return {"proposed": r.proposed, "unchanged": r.unchanged, "not_covered": r.not_covered,
            "rejected_by_trust_checks": r.rejected_by_trust_checks, "error": r.error,
            "dry_run": r.dry_run}


def _rows(rows, json_columns: tuple[str, ...] = ()) -> list[dict]:
    out = []
    for row in rows:
        d = dict(row)
        for c in json_columns:
            d[c] = json.loads(d[c])
        out.append(d)
    return out


def create_router(reg: Registry, require_admin: AdminCheck, sources: Callable[[], list[Source]],
                  clock: Callable[[], datetime]) -> APIRouter:
    """`sources` is called per trigger so each run builds fresh fetchers; `clock` is injected so
    tests control the stale-run reaper."""
    router = APIRouter(dependencies=[Depends(require_admin)])
    conn = reg.conn

    def model_or_404(model_id: str) -> Model:
        try:
            return reg.get_model(model_id)
        except KeyError:
            raise HTTPException(404, f"no model {model_id}") from None

    # Handlers are async on purpose: they all run on the event-loop thread, so the single shared
    # sqlite connection is never used concurrently. Simplification: a real pool replaces this.
    @router.get("/models")
    async def list_models() -> list[dict]:
        ids = [r["model_id"] for r in conn.execute("SELECT model_id FROM models ORDER BY model_id")]
        return [_model_json(reg.get_model(i)) for i in ids]

    @router.get("/models/{model_id}")
    async def get_model(model_id: str) -> dict:
        return _model_json(model_or_404(model_id))

    @router.patch("/models/{model_id}/pricing")
    async def patch_pricing(model_id: str, update: PriceUpdate, actor: str = Depends(require_admin)) -> dict:
        model_or_404(model_id)
        try:
            return _model_json(reg.update_price(model_id, update, actor))
        except ValueError as e:  # the merged row broke an invariant (e.g. cached above input)
            raise HTTPException(422, str(e)) from None

    @router.get("/models/{model_id}/history")
    async def price_history(model_id: str) -> list[dict]:
        model_or_404(model_id)
        rows = conn.execute("SELECT * FROM price_history WHERE model_id=? ORDER BY id", (model_id,))
        return _rows(rows, ("before_json", "after_json"))

    @router.get("/proposals")
    async def list_proposals(status: str | None = None) -> list[dict]:
        if status not in (None, "pending", "approved", "rejected"):
            raise HTTPException(422, f"unknown status {status}")
        rows = conn.execute("SELECT * FROM proposals WHERE (? IS NULL OR status=?) ORDER BY id",
                            (status, status))
        return _rows(rows, ("proposed_json", "snapshot_json"))

    def decide(fn: Callable, proposal_id: int, actor: str) -> dict:
        try:
            fn(reg, proposal_id, actor)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from None
        except (StaleProposal, DecisionError) as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"id": proposal_id, "status": "approved" if fn is approve else "rejected"}

    @router.post("/proposals/{proposal_id}/approve")
    async def approve_proposal(proposal_id: int, actor: str = Depends(require_admin)) -> dict:
        return decide(approve, proposal_id, actor)

    @router.post("/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: int, actor: str = Depends(require_admin)) -> dict:
        return decide(reject, proposal_id, actor)

    def bulk(decision: str, body: BulkRequest, actor: str) -> dict:
        try:
            return {"outcomes": {str(k): v for k, v in bulk_decide(reg, body.ids, decision, actor).items()}}
        except ValueError as e:  # over the cap
            raise HTTPException(422, str(e)) from None

    @router.post("/proposals/bulk-approve")
    async def bulk_approve(body: BulkRequest, actor: str = Depends(require_admin)) -> dict:
        return bulk("approve", body, actor)

    @router.post("/proposals/bulk-reject")
    async def bulk_reject(body: BulkRequest, actor: str = Depends(require_admin)) -> dict:
        return bulk("reject", body, actor)

    @router.get("/sync-runs")
    async def list_sync_runs() -> list[dict]:
        return _rows(conn.execute("SELECT * FROM sync_runs ORDER BY id DESC"))

    @router.post("/sync/trigger")
    async def trigger_sync() -> dict:
        """The run row is the lock: a live run holding it means 409, not a second concurrent run."""
        try:
            return _report_json(run_sync(reg, sources(), clock()))
        except SyncAlreadyRunning as e:
            raise HTTPException(409, str(e)) from None

    return router


def create_app(reg: Registry, require_admin: AdminCheck, sources: Callable[[], list[Source]],
               clock: Callable[[], datetime]) -> FastAPI:
    app = FastAPI(title="model-price-registry")
    app.include_router(create_router(reg, require_admin, sources, clock), prefix="/admin")
    return app
