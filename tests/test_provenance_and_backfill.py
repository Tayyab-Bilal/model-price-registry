from decimal import Decimal as D

import pytest
from conftest import count

from model_price_registry.backfill import backfill_cached_rates, downgrade_backfill
from model_price_registry.checks import RegistryCheckError, check_registry_rates
from model_price_registry.db import RATE_FIELDS, to_micro
from model_price_registry.proposals import MAX_BULK, bulk_decide
from model_price_registry.registry import Registry
from model_price_registry.seed import SEED_MODELS
from model_price_registry.validation import PriceUpdate

EXPECTED = {m[0]: dict(zip(RATE_FIELDS, m[2:], strict=True)) for m in SEED_MODELS}


def test_registry_matches_published_rates(reg):
    check_registry_rates(reg.conn, EXPECTED)


def test_migration_row_with_wrong_rate_fails_the_exact_check(reg):
    reg.conn.execute("UPDATE models SET output_per_mtok=1 WHERE model_id='acme-code-pro'")
    with pytest.raises(RegistryCheckError, match="acme-code-pro: migration rates differ"):
        check_registry_rates(reg.conn, EXPECTED)


def test_runtime_edited_rows_are_exempt_from_exact_check(reg):
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="deal"), "alice")
    check_registry_rates(reg.conn, EXPECTED)  # admin row differs from the list, and that is fine


def test_runtime_edited_rows_are_still_held_to_invariants(reg):
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="deal"), "alice")
    reg.conn.execute("UPDATE models SET cached_input_per_mtok=? WHERE model_id='acme-code-pro'",
                     (to_micro(D("5")),))  # cached 5 > input 2, written behind the API's back
    with pytest.raises(RegistryCheckError, match="acme-code-pro: invariant broken"):
        check_registry_rates(reg.conn, EXPECTED)


def test_missing_and_unexpected_models_fail_the_check(reg):
    with pytest.raises(RegistryCheckError, match="missing from the registry"):
        check_registry_rates(reg.conn, {**EXPECTED, "acme-ghost": {}})
    short = {k: v for k, v in EXPECTED.items() if k != "acme-embed-v2"}
    with pytest.raises(RegistryCheckError, match="acme-embed-v2: migration row missing"):
        check_registry_rates(reg.conn, short)


def test_bulk_decisions_capped_at_200(reg):
    assert MAX_BULK == 200
    with pytest.raises(ValueError, match="at most 200"):
        bulk_decide(reg, list(range(201)), "reject", "alice")
    assert len(bulk_decide(reg, list(range(200)), "reject", "alice")) == 200


def _no_cached(reg: Registry, model_id="acme-code-pro") -> None:
    reg.conn.execute("UPDATE models SET cached_input_per_mtok=NULL WHERE model_id=?", (model_id,))


def test_missing_cached_rate_bills_cached_tokens_at_full_input_rate(reg):
    _no_cached(reg)
    assert reg.cost_for("acme-code-pro", 0, 0, cached_tokens=1_000_000) == D("2")  # the overcharge
    assert backfill_cached_rates(reg.conn, {"acme-code-pro": D("0.50")}) == 1
    reg.cache.invalidate("acme-code-pro")
    assert reg.cost_for("acme-code-pro", 0, 0, cached_tokens=1_000_000) == D("0.5")
    assert count(reg, "price_history", "actor='backfill'") == 1


def test_backfill_is_guarded(reg):
    _no_cached(reg)
    _no_cached(reg, "acme-chat-small")
    reg.update_price("acme-chat-small", PriceUpdate(cached_input_per_mtok=None, reason="no cache"), "a")
    out = backfill_cached_rates(reg.conn, {
        "acme-code-pro": D("9"),  # above input 2: refused
        "acme-chat-small": D("0.03"),  # admin cleared it on purpose: left alone
        "acme-chat-large": D("0.01"),  # already has a cached rate: never overwritten
        "acme-embed-v2": D("0.01"),  # not exempt, but NULL and migration: filled
        "acme-ghost": D("0.01"),  # unknown model: ignored
    })
    assert out == 1
    assert reg.get_model("acme-code-pro").rates["cached_input_per_mtok"] is None
    assert reg.get_model("acme-chat-small").rates["cached_input_per_mtok"] is None
    assert reg.get_model("acme-chat-large").rates["cached_input_per_mtok"] == D("0.3")
    assert backfill_cached_rates(reg.conn, {"acme-embed-v2": D("0.01")}) == 0  # idempotent


def test_backfill_downgrade_is_a_no_op(reg):
    _no_cached(reg)
    backfill_cached_rates(reg.conn, {"acme-code-pro": D("0.50")})
    assert downgrade_backfill(reg.conn) == 0
    assert reg.get_model("acme-code-pro").rates["cached_input_per_mtok"] == D("0.5")
