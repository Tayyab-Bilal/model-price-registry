"""Billing axes. A quote is only ever compared to the registry column on the same axis."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .db import MICRO, from_micro, to_micro


@dataclass(frozen=True)
class Quote:
    model_id: str
    axis: str
    rate_per_unit: Decimal  # USD per ONE unit of the axis (one token, one second, ...)
    source: str


# axis -> (registry column or None, multiplier from per-unit to the column's unit)
AXES: dict[str, tuple[str | None, Decimal]] = {
    "text_input_token": ("input_per_mtok", MICRO),
    "text_output_token": ("output_per_mtok", MICRO),
    "cached_input_token": ("cached_input_per_mtok", MICRO),
    "audio_input_token": (None, MICRO),  # no registry column: audio tokens are not text tokens
    "audio_minute": ("per_minute", Decimal(1)),  # a minute is 60 seconds: see audio_second
    "audio_second": ("per_minute", Decimal(60)),
    "rerank_search": (None, Decimal(1)),
}


class Outcome(Enum):
    SAME = "same"
    DIFFERENT = "different"
    NOT_COVERED = "not_covered"


def compare(quote: Quote, rates: dict[str, Decimal | None]) -> tuple[Outcome, str | None, Decimal | None]:
    """Returns (outcome, column, rate in the column's unit). Unknown axis, an axis with no
    column, or a model that does not bill on that column -> NOT_COVERED, never a guess."""
    column, factor = AXES.get(quote.axis, (None, Decimal(1)))
    current = rates.get(column) if column else None
    if column is None or current is None:
        return Outcome.NOT_COVERED, None, None
    rate = from_micro(to_micro(quote.rate_per_unit * factor))
    return (Outcome.SAME if rate == current else Outcome.DIFFERENT), column, rate
