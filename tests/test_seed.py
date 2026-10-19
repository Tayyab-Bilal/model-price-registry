from decimal import Decimal as D

from conftest import count

from model_price_registry.proposals import approve, file_proposal
from model_price_registry.seed import SEED_MODELS, seed
from model_price_registry.validation import PriceUpdate


def test_seed_never_overwrites_admin_rows(reg):
    reg.update_price("acme-chat-large", PriceUpdate(input_per_mtok=D("5"), reason="negotiated"), "a")
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "api")
    approve(reg, pid, "alice")  # provenance=sync
    assert seed(reg.conn) == 0  # a restart inserts nothing
    assert reg.get_model("acme-chat-large").rates["input_per_mtok"] == D("5")
    assert reg.get_model("acme-code-pro").rates["input_per_mtok"] == D("2.5")
    assert count(reg, "models") == len(SEED_MODELS)


def test_seed_inserts_new_models_only(reg):
    extra = [*SEED_MODELS, ("acme-new-1", "acme", D("1"), D("2"), None, None)]
    assert seed(reg.conn, extra) == 1
