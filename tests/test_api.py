from datetime import timedelta
from decimal import Decimal as D

import pytest
from conftest import FIX, NOW
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from model_price_registry.api import create_app, create_router
from model_price_registry.proposals import file_proposal
from model_price_registry.sources.fixtures import fixture_sources

IDS = {"acme-chat-large", "acme-chat-small", "acme-code-pro", "acme-embed-v2",
       "acme-rerank-1", "acme-listen-1", "acme-live-1"}


def require_admin(x_actor: str | None = Header(default=None), x_role: str | None = Header(default=None)):
    if x_actor is None:
        raise HTTPException(401, "not signed in")
    if x_role != "admin":
        raise HTTPException(403, "admins only")
    return x_actor


@pytest.fixture
def client(reg):
    app = create_app(reg, require_admin, lambda: fixture_sources(FIX, IDS), lambda: NOW)
    return TestClient(app, headers={"x-actor": "alice", "x-role": "admin"})


def test_every_route_is_admin_only(reg):
    app = create_app(reg, require_admin, lambda: [], lambda: NOW)
    anon = TestClient(app)
    member = TestClient(app, headers={"x-actor": "eve", "x-role": "member"})
    router = create_router(reg, require_admin, lambda: [], lambda: NOW)
    routes = [(sorted(r.methods)[0], "/admin" + r.path) for r in router.routes]
    assert len(routes) == 11  # 10 admin routes + the sync trigger
    for method, path in routes:
        path = path.replace("{model_id}", "acme-code-pro").replace("{proposal_id}", "1")
        assert anon.request(method, path, json={}).status_code == 401, path
        assert member.request(method, path, json={}).status_code == 403, path


def test_list_and_get_model(client):
    assert len(client.get("/admin/models").json()) == 7
    body = client.get("/admin/models/acme-code-pro").json()
    assert body["rates"]["output_per_mtok"] == "8" and body["provenance"] == "migration"
    assert client.get("/admin/models/nope").status_code == 404


def test_patch_price_is_audited_and_visible_in_history(client):
    r = client.patch("/admin/models/acme-code-pro/pricing", json={"output_per_mtok": "9", "reason": "deal"})
    assert r.status_code == 200 and r.json()["provenance"] == "admin"
    (h,) = client.get("/admin/models/acme-code-pro/history").json()
    assert h["actor"] == "alice" and h["after_json"]["output_per_mtok"] == "9.000000"


def test_patch_omitted_vs_null_over_http(client):
    body = {"cached_input_per_mtok": None, "reason": "clear"}  # explicit null clears one rate only
    client.patch("/admin/models/acme-code-pro/pricing", json=body)
    rates = client.get("/admin/models/acme-code-pro").json()["rates"]
    assert rates["cached_input_per_mtok"] is None and rates["input_per_mtok"] == "2"


@pytest.mark.parametrize("body", [
    {"input_per_mtok": "-1", "reason": "x"},
    {"input_per_mtok": "0.0000001", "reason": "x"},
    {"input_per_mtok": "1", "reason": "  "},
    {"per_minute": "0.01", "reason": "x"},  # per token and per minute: billed twice
    {"cached_input_per_mtok": "9", "reason": "x"},  # merged row: cached above input
])
def test_invalid_patch_is_422_and_changes_nothing(client, body):
    assert client.patch("/admin/models/acme-code-pro/pricing", json=body).status_code == 422
    assert client.get("/admin/models/acme-code-pro/history").json() == []


def test_proposals_approve_reject_and_final_decisions(client, reg):
    _, a = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    _, b = file_proposal(reg, "acme-chat-small", {"input_per_mtok": D("0.3")}, "api")
    assert [p["id"] for p in client.get("/admin/proposals?status=pending").json()] == [a, b]
    assert client.post(f"/admin/proposals/{a}/approve").json() == {"id": a, "status": "approved"}
    assert client.post(f"/admin/proposals/{b}/reject").json()["status"] == "rejected"
    assert client.post(f"/admin/proposals/{a}/approve").status_code == 409  # decisions are final
    assert client.post("/admin/proposals/999/approve").status_code == 404
    assert client.get("/admin/proposals?status=bogus").status_code == 422
    assert len(client.get("/admin/proposals").json()) == 2


def test_stale_proposal_is_409(client, reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    client.patch("/admin/models/acme-code-pro/pricing", json={"output_per_mtok": "9", "reason": "manual"})
    r = client.post(f"/admin/proposals/{pid}/approve")
    assert r.status_code == 409 and "changed since" in r.json()["detail"]


def test_bulk_approve_and_reject_report_per_proposal(client, reg):
    _, a = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    _, b = file_proposal(reg, "acme-chat-small", {"input_per_mtok": D("0.3")}, "api")
    client.patch("/admin/models/acme-code-pro/pricing", json={"output_per_mtok": "9", "reason": "m"})
    out = client.post("/admin/proposals/bulk-approve", json={"ids": [a, b]}).json()["outcomes"]
    assert out == {str(a): "stale", str(b): "approved"}
    out = client.post("/admin/proposals/bulk-reject", json={"ids": [a]}).json()["outcomes"]
    assert out == {str(a): "rejected"}


def test_bulk_over_200_is_422(client):
    assert client.post("/admin/proposals/bulk-reject", json={"ids": list(range(201))}).status_code == 422
    assert client.post("/admin/proposals/bulk-reject", json={"ids": list(range(200))}).status_code == 200


def test_trigger_runs_sync_and_lists_runs(client, reg):
    r = client.post("/admin/sync/trigger")
    assert r.status_code == 200 and r.json()["error"] is None
    assert any(p[0] == "acme-chat-large" for p in r.json()["proposed"])
    (run,) = client.get("/admin/sync-runs").json()
    assert run["status"] == "succeeded"


def test_trigger_is_409_while_a_live_run_holds_the_lock(client, reg):
    reg.conn.execute("INSERT INTO sync_runs(status, started_at) VALUES ('running', ?)",
                     ((NOW - timedelta(minutes=1)).isoformat(),))
    assert client.post("/admin/sync/trigger").status_code == 409
    assert client.get("/admin/proposals").json() == []  # nothing ran
