"""Community price databases in different shapes, normalised to (model, axis, rate).

Believe a rate only when at least two databases state it exactly and none disagrees."""
from decimal import Decimal

from ..units import Quote
from .common import SourceResult

MTOK = Decimal(1_000_000)


def _a(data: dict):  # {"models": {id: {"input_cost_per_token": ...}}}
    keys = {"input_cost_per_token": "text_input_token", "output_cost_per_token": "text_output_token",
            "cache_read_input_token_cost": "cached_input_token",
            "audio_cost_per_second": "audio_second", "cost_per_search": "rerank_search"}
    for model, entry in data["models"].items():
        for k, axis in keys.items():
            if k in entry:
                yield model, axis, Decimal(str(entry[k]))


def _b(data: list):  # [{"name":..., "input_per_million":...}]
    for row in data:
        for k, axis in (("input_per_million", "text_input_token"),
                        ("output_per_million", "text_output_token")):
            if k in row:
                yield row["name"], axis, Decimal(str(row[k])) / MTOK


def _c(data: dict):  # {"entries": {id: {"price": {"in":..,"out":..}, "unit": "usd/Mtok"}}}
    for model, entry in data["entries"].items():
        if entry["unit"] != "usd/Mtok":  # unit we can't map to an axis: skip, don't guess
            continue
        for k, axis in (("in", "text_input_token"), ("out", "text_output_token")):
            if k in entry["price"]:
                yield model, axis, Decimal(str(entry["price"][k])) / MTOK


def _d(data: list):  # [{"model":..., "unit": "second", "price":...}]
    units = {"second": "audio_second", "minute": "audio_minute", "search": "rerank_search"}
    for row in data:
        if row["unit"] in units:
            yield row["model"], units[row["unit"]], Decimal(str(row["price"]))


def _e(data: list):  # [{"model":..., "modality": "audio"|"text", "per": "token", "cents": ...}]
    # This database quotes US cents per token. Convert to dollars here, once, and keep the axis:
    # a cents-per-token audio price must never land on a per-minute or text column.
    axes = {"text": "text_input_token", "audio": "audio_input_token"}
    for row in data:
        if row["per"] == "token" and row["modality"] in axes:
            yield row["model"], axes[row["modality"]], Decimal(str(row["cents"])) / 100


# Simplification: five hand-written shapes stand in for five real databases; adding a source is
# one more normaliser, and the vote logic below does not change.
NORMALISERS = {"community_a": _a, "community_b": _b, "community_c": _c, "community_d": _d,
               "community_e": _e}


def from_payloads(payloads: dict[str, object]) -> SourceResult:
    votes: dict[tuple[str, str], dict[str, Decimal]] = {}
    for name, payload in payloads.items():
        for model, axis, rate in NORMALISERS[name](payload):
            votes.setdefault((model, axis), {})[name] = rate
    quotes = []
    for (model, axis), by_source in votes.items():
        rates = set(by_source.values())
        if len(by_source) >= 2 and len(rates) == 1:
            quotes.append(Quote(model, axis, rates.pop(), "consensus"))
    return SourceResult("consensus", quotes)
