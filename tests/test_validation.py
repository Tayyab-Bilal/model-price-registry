from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from model_price_registry.validation import PriceUpdate


def test_negative_rejected():
    with pytest.raises(ValidationError, match="negative"):
        PriceUpdate(input_per_mtok=D("-1"), reason="x")


def test_rounds_to_zero_rejected():
    with pytest.raises(ValidationError, match="rounds to zero"):
        PriceUpdate(input_per_mtok=D("0.0000004"), reason="x")
    PriceUpdate(input_per_mtok=D("0"), reason="free tier")  # exact zero is allowed
    PriceUpdate(input_per_mtok=D("0.000001"), reason="smallest unit")


def test_cached_above_input_rejected(reg):
    from model_price_registry.registry import Registry  # merged-state check via the write path

    with pytest.raises(ValidationError, match="exceed"):
        PriceUpdate(input_per_mtok=D("1"), cached_input_per_mtok=D("2"), reason="x")
    # partial update: only cached given, but it exceeds the stored input rate (3.00)
    with pytest.raises(ValueError, match="exceed"):
        Registry.update_price(reg, "acme-chat-large",
                              PriceUpdate(cached_input_per_mtok=D("9"), reason="x"), "alice")


def test_token_and_minute_both_rejected(reg):
    with pytest.raises(ValidationError, match="never both"):
        PriceUpdate(input_per_mtok=D("1"), per_minute=D("1"), reason="x")
    # partial: adding a per-minute rate to a per-token model
    with pytest.raises(ValueError, match="never both"):
        reg.update_price("acme-chat-large", PriceUpdate(per_minute=D("0.01"), reason="x"), "alice")


@pytest.mark.parametrize("reason", ["", "   "])
def test_reason_required(reason):
    with pytest.raises(ValidationError):
        PriceUpdate(input_per_mtok=D("1"), reason=reason)
    with pytest.raises(ValidationError):
        PriceUpdate(input_per_mtok=D("1"))


def test_null_clears_omitted_keeps(reg):
    reg.update_price("acme-chat-large", PriceUpdate(cached_input_per_mtok=None, reason="drop"), "a")
    m = reg.get_model("acme-chat-large")
    assert m.rates["cached_input_per_mtok"] is None  # explicit null cleared
    assert m.rates["input_per_mtok"] == D("3") and m.rates["output_per_mtok"] == D("15")  # kept
