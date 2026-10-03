"""OpenRouter-style JSON: per-token price strings keyed by role."""
from decimal import Decimal

from ..units import Quote
from .common import SourceResult

PRICING_AXES = {
    "prompt": "text_input_token",
    "completion": "text_output_token",
    "input_cache_read": "cached_input_token",
    "audio": "audio_input_token",
    "search": "rerank_search",
}


def from_payload(payload: dict) -> SourceResult:
    quotes = [
        Quote(item["id"].split("/")[-1], PRICING_AXES[key], Decimal(str(value)), "api")
        for item in payload["data"]
        for key, value in item["pricing"].items()
        if key in PRICING_AXES
    ]
    return SourceResult("api", quotes)
