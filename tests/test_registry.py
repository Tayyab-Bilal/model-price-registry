from decimal import Decimal as D

import pytest
from conftest import FIX, NOW, count

from model_price_registry import registry as registry_module
from model_price_registry.proposals import approve, list_pending
from model_price_registry.sources.fixtures import fixture_sources
from model_price_registry.sync import run_sync
from model_price_registry.validation import PriceUpdate


def test_update_writes_history_in_same_transaction(reg, monkeypatch):
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="ok"), "alice")
    assert count(reg, "price_history") == 1
    row = reg.conn.execute("SELECT * FROM price_history").fetchone()
    assert '"8.000000"' in row["before_json"] and '"9.000000"' in row["after_json"]
    assert row["actor"] == "alice" and reg.get_model("acme-code-pro").provenance == "admin"

    def boom(*a, **k):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(registry_module, "_write_history", boom)
    with pytest.raises(RuntimeError):
        reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("20"), reason="x"), "bob")
    assert count(reg, "price_history") == 1  # no extra history row
    assert reg.get_model("acme-code-pro").rates["output_per_mtok"] == D("9")  # and no price change


def test_cache_invalidated_on_update(reg, clock):
    before = reg.cost_for("acme-chat-large", 1_000_000, 0)  # primes the cache
    clock.t += 1  # well inside the 60 s TTL
    reg.update_price("acme-chat-large", PriceUpdate(input_per_mtok=D("4"), reason="r"), "a")
    assert reg.cost_for("acme-chat-large", 1_000_000, 0) == D("4") != before


def test_cache_expires_after_ttl(reg, clock):
    reg.cost_for("acme-chat-large", 1, 0)
    reg.conn.execute("UPDATE models SET input_per_mtok=9000000 WHERE model_id='acme-chat-large'")
    assert reg.cost_for("acme-chat-large", 1_000_000, 0) == D("3")  # stale until TTL
    clock.t += 61
    assert reg.cost_for("acme-chat-large", 1_000_000, 0) == D("9")


def test_changing_price_changes_the_bill(reg):
    call = ("acme-chat-large", 1_000_000, 100_000)
    assert reg.cost_for(*call) == D("4.5")  # 3.00 + 0.1 * 15.00, and the cache is now warm
    run_sync(reg, fixture_sources(FIX, {"acme-chat-large"})[:1], NOW)  # api quote: output is 18
    (draft,) = list_pending(reg)
    assert reg.cost_for(*call) == D("4.5")  # a draft moves no money
    approve(reg, draft["id"], "alice")
    assert reg.cost_for(*call) == D("4.8")
    assert reg.get_model("acme-chat-large").provenance == "sync"
    assert count(reg, "price_history", "actor='alice' AND source='sync'") == 1


def test_two_workers_see_a_change_within_the_ttl(tmp_path):
    from conftest import Clock

    from model_price_registry.db import connect
    from model_price_registry.registry import Registry
    from model_price_registry.seed import seed

    path = str(tmp_path / "shared.db")
    seed(connect(path))
    clock = Clock()
    a, b = Registry(connect(path), clock=clock), Registry(connect(path), clock=clock)
    assert a.cost_for("acme-chat-large", 1_000_000, 0) == D("3")  # worker A caches the rate
    b.update_price("acme-chat-large", PriceUpdate(input_per_mtok=D("4"), reason="r"), "x")
    assert b.cost_for("acme-chat-large", 1_000_000, 0) == D("4")  # B invalidated its own cache
    assert a.cost_for("acme-chat-large", 1_000_000, 0) == D("3")  # A lags: invalidation is local
    clock.t += 60
    assert a.cost_for("acme-chat-large", 1_000_000, 0) == D("4")  # but never longer than the TTL
