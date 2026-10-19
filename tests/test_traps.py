"""Units and billing-axis traps, each with a fixture shaped like the real quote."""
from decimal import Decimal as D

from conftest import FIX, NOW, count

from model_price_registry.db import RATE_FIELDS
from model_price_registry.proposals import file_proposal
from model_price_registry.seed import SEED_MODELS
from model_price_registry.sources import api_source, consensus_source
from model_price_registry.sources.common import load_json
from model_price_registry.sources.fixtures import fixture_sources
from model_price_registry.sync import run_sync
from model_price_registry.units import Outcome, Quote, compare

RATES = {m[0]: dict(zip(RATE_FIELDS, m[2:], strict=True)) for m in SEED_MODELS}
IDS = set(RATES)


def test_realtime_audio_rate_is_not_mistaken_for_the_text_rate():
    res = api_source.from_payload(load_json(FIX / "api_prices.json"))
    live = {q.axis: q for q in res.quotes if q.model_id == "acme-live-1"}
    text, audio = live["text_input_token"], live["audio_input_token"]
    assert audio.rate_per_unit == 8 * text.rate_per_unit  # the 8x gap between the two rates
    assert compare(text, RATES["acme-live-1"])[0] is Outcome.SAME
    assert compare(audio, RATES["acme-live-1"])[0] is Outcome.NOT_COVERED
    naive = audio.rate_per_unit * 1_000_000  # what a "price per Mtok" guess would propose
    assert naive == 8 * RATES["acme-live-1"]["input_per_mtok"]


def test_realtime_audio_never_becomes_a_draft(reg):
    report = run_sync(reg, fixture_sources(FIX, IDS), NOW)
    assert ("acme-live-1", "audio_input_token") in report.not_covered
    assert not any(m == "acme-live-1" for m, _, _ in report.proposed)
    assert count(reg, "proposals", "model_id='acme-live-1'") == 0


def test_per_second_quote_misread_as_per_minute_is_off_by_60():
    listen = RATES["acme-listen-1"]  # 0.006 USD per minute
    assert compare(Quote("acme-listen-1", "audio_second", D("0.0001"), "x"), listen)[0] is Outcome.SAME
    misread = Quote("acme-listen-1", "audio_second", D("0.006"), "x")  # a per-minute number, per second
    assert compare(misread, listen)[2] / listen["per_minute"] == 60


def test_rerank_per_search_is_never_compared_with_a_per_token_column():
    rerank = RATES["acme-rerank-1"]  # registry holds a per-token input rate
    quote = Quote("acme-rerank-1", "rerank_search", D("0.002"), "x")
    assert compare(quote, rerank)[0] is Outcome.NOT_COVERED
    assert quote.rate_per_unit * 1_000_000 / rerank["input_per_mtok"] == 40_000  # the naive mix-up


def test_cents_per_token_audio_quote_is_converted_and_stays_on_the_audio_axis():
    rows = list(consensus_source.NORMALISERS["community_e"](load_json(FIX / "community_e.json")))
    audio = next(r for r in rows if r[0] == "acme-live-1" and r[1] == "audio_input_token")
    assert audio[2] == D("0.000032")  # 0.0032 cents is 0.000032 dollars, not 0.0032
    quote = Quote(*audio[:2], audio[2], "consensus")
    assert compare(quote, RATES["acme-live-1"])[0] is Outcome.NOT_COVERED
    listen = next(r for r in rows if r[0] == "acme-listen-1")
    assert compare(Quote(listen[0], listen[1], listen[2], "x"), RATES["acme-listen-1"])[0] is (
        Outcome.NOT_COVERED)  # never read as a per-minute rate


def test_consensus_never_overrides_a_provider_page_draft(reg):
    _, pid = file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.1")}, "provider_page")
    assert file_proposal(reg, "acme-code-pro", {"input_per_mtok": D("2.5")}, "consensus") == ("kept", pid)
    assert reg.conn.execute("SELECT source FROM proposals").fetchone()["source"] == "provider_page"
