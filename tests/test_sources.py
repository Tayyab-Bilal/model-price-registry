from decimal import Decimal as D

from conftest import FIX

from model_price_registry.db import RATE_FIELDS
from model_price_registry.seed import SEED_MODELS
from model_price_registry.sources import api_source, consensus_source, page_source
from model_price_registry.sources.common import load_json
from model_price_registry.units import Outcome, Quote, compare

IDS = {m[0] for m in SEED_MODELS}
RATES = {m[0]: dict(zip(RATE_FIELDS, m[2:], strict=True)) for m in SEED_MODELS}


def _quote(res, model, axis):
    return next(q for q in res.quotes if q.model_id == model and q.axis == axis)


def test_api_per_token_converted_to_per_million():
    res = api_source.from_payload(load_json(FIX / "api_prices.json"))
    q = _quote(res, "acme-chat-large", "text_output_token")
    assert q.rate_per_unit == D("0.000018")  # per token, provider prefix stripped
    outcome, column, rate = compare(q, RATES["acme-chat-large"])
    assert (outcome, column, rate) == (Outcome.DIFFERENT, "output_per_mtok", D("18"))
    same = _quote(res, "acme-chat-large", "text_input_token")
    assert compare(same, RATES["acme-chat-large"])[0] is Outcome.SAME


def test_page_trust_checks_block_low_coverage():
    res = page_source.from_html((FIX / "provider_page_lowcov.html").read_text(), IDS)
    assert res.quotes == [] and "covers" in res.rejected  # 6 models parsed, 1 of 6 in registry
    ok = page_source.from_html((FIX / "provider_page_good.html").read_text(), IDS)
    assert ok.rejected is None
    out = _quote(ok, "acme-chat-small", "text_output_token")
    assert compare(out, RATES["acme-chat-small"])[1:] == ("output_per_mtok", D("1.35"))


def test_page_trust_checks_block_too_few_models_and_implausible_prices():
    good = (FIX / "provider_page_good.html").read_text()
    few = "\n".join(line for line in good.splitlines() if "embed" not in line and "rerank" not in line)
    assert "models" in page_source.from_html(few, IDS).rejected
    insane = good.replace("$15.00", "$1500.00")
    assert "cap" in page_source.from_html(insane, IDS).rejected


def _consensus():
    names = consensus_source.NORMALISERS
    return consensus_source.from_payloads({n: load_json(FIX / f"{n}.json") for n in names})


def test_consensus_needs_two_exact():
    res = _consensus()
    assert _quote(res, "acme-code-pro", "text_input_token").rate_per_unit == D("0.0000025")
    assert not any(q.model_id == "acme-chat-large" and q.axis == "cached_input_token"
                   for q in res.quotes)  # only one database states it
    near = consensus_source.from_payloads({
        "community_b": [{"name": "m", "input_per_million": 2.5}],
        "community_c": {"entries": {"m": {"price": {"in": "2.51"}, "unit": "usd/Mtok"}}},
    })
    assert near.quotes == []  # 2.50 vs 2.51 is not "exact"


def test_consensus_disagreement_proposes_nothing():
    res = _consensus()
    # a and b say 1.25, c says 1.50: two agree but one disagrees -> nothing
    assert not any(q.model_id == "acme-chat-small" and q.axis == "text_output_token"
                   for q in res.quotes)
    assert any(q.model_id == "acme-chat-small" and q.axis == "text_input_token"
               for q in res.quotes)  # all three agree on input


def test_per_second_vs_per_minute_not_confused():
    listen = RATES["acme-listen-1"]  # 0.006 USD per minute
    per_sec = Quote("acme-listen-1", "audio_second", D("0.0001"), "x")  # == 0.006 per minute
    assert compare(per_sec, listen)[0] is Outcome.SAME
    wrong = Quote("acme-listen-1", "audio_second", D("0.006"), "x")  # a per-minute number, per second
    assert compare(wrong, listen)[2] == D("0.36")  # converted, never compared raw
    per_min = Quote("acme-listen-1", "audio_minute", D("0.006"), "x")
    assert compare(per_min, listen)[0] is Outcome.SAME


def test_audio_vs_text_axis_not_confused():
    audio_tok = Quote("acme-chat-large", "audio_input_token", D("0.0000001"), "x")
    assert compare(audio_tok, RATES["acme-chat-large"])[0] is Outcome.NOT_COVERED
    text_tok = Quote("acme-listen-1", "text_input_token", D("0.000003"), "x")
    assert compare(text_tok, RATES["acme-listen-1"])[0] is Outcome.NOT_COVERED  # per-minute model
    assert compare(Quote("m", "mystery_axis", D(1), "x"), RATES["acme-chat-large"])[0] is (
        Outcome.NOT_COVERED)
