import sqlite3
from decimal import Decimal as D

import pytest
from conftest import count

from model_price_registry.proposals import (
    DecisionError,
    StaleProposal,
    approve,
    bulk_decide,
    file_proposal,
    list_pending,
    reject,
)
from model_price_registry.validation import PriceUpdate


def test_one_pending_per_model_enforced_by_index(reg):
    file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    with pytest.raises(sqlite3.IntegrityError):
        reg.conn.execute(
            "INSERT INTO proposals(model_id, proposed_json, snapshot_json, source, created_at)"
            " VALUES ('acme-code-pro', '{}', '{}', 'api', 'now')")
    # a decided row does not block a new pending one
    reject(reg, list_pending(reg)[0]["id"], "alice")
    assert file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.6")}, "api")[0] == "filed"


def test_stale_approval_refused(reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="manual"), "bob")
    with pytest.raises(StaleProposal):
        approve(reg, pid, "alice")
    m = reg.get_model("acme-code-pro")
    assert m.rates["input_per_mtok"] == D("2")  # draft not applied
    assert count(reg, "proposals", "status='pending'") == 1  # still reviewable (reject it)


def test_bulk_one_stale_does_not_roll_back_others(reg):
    _, good = file_proposal(reg, "acme-chat-small", {"input_per_mtok": D("0.30")}, "api")
    _, stale = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="manual"), "bob")
    out = bulk_decide(reg, [good, stale], "approve", "alice")  # stale comes after good
    assert out == {good: "approved", stale: "stale"}
    assert reg.get_model("acme-chat-small").rates["input_per_mtok"] == D("0.3")
    assert count(reg, "price_history", "model_id='acme-chat-small'") == 1


def test_decision_is_final(reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    approve(reg, pid, "alice")
    for decide in (approve, reject):
        with pytest.raises(DecisionError):
            decide(reg, pid, "alice")
    assert count(reg, "price_history") == 1
    assert bulk_decide(reg, [pid], "reject", "alice")[pid].startswith("error")


def test_weaker_source_does_not_replace_stronger_draft(reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.4")}, "provider_page")
    assert file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api") == ("kept", pid)
    assert file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.6")}, "provider_page") == (
        "kept", pid)  # equal strength is a no-op too
    (row,) = list_pending(reg)
    assert row["source"] == "provider_page" and '"2.400000"' in row["proposed_json"]


def test_stronger_source_replaces_weaker_draft(reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "consensus")
    assert file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.4")}, "api") == (
        "replaced", pid)
    assert list_pending(reg)[0]["source"] == "api" and count(reg, "proposals") == 1


def test_invalid_draft_is_not_filed(reg):
    with pytest.raises(ValueError):
        file_proposal(reg, "acme-chat-large", {"cached_input_per_mtok": D("9")}, "api")
    assert count(reg, "proposals") == 0


def test_failed_apply_leaves_proposal_pending(reg, monkeypatch):
    from model_price_registry import proposals

    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")

    def boom(*a, **k):
        raise RuntimeError("apply failed")

    monkeypatch.setattr(proposals, "apply_update", boom)
    with pytest.raises(RuntimeError):
        approve(reg, pid, "alice")
    assert count(reg, "proposals", "status='pending'") == 1  # decision rolled back with the apply
    assert reg.get_model("acme-code-pro").rates["input_per_mtok"] == D("2")
