"""Price invariants. Checked on the merged result, so a partial update cannot sneak past them."""
from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from .db import RATE_FIELDS, TOKEN_FIELDS, to_micro


def validate_rates(rates: dict[str, Decimal | None]) -> None:
    for name, v in rates.items():
        if v is None:
            continue
        if v < 0:
            raise ValueError(f"{name} must not be negative")
        if v > 0 and to_micro(v) == 0:
            raise ValueError(f"{name}={v} rounds to zero at storage precision")
    cached, inp = rates.get("cached_input_per_mtok"), rates.get("input_per_mtok")
    if cached is not None and inp is not None and cached > inp:
        raise ValueError("cached_input_per_mtok must not exceed input_per_mtok")
    per_token = any(rates.get(f) is not None for f in TOKEN_FIELDS)
    if per_token and rates.get("per_minute") is not None:
        raise ValueError("a model is billed per token or per minute, never both")


class PriceUpdate(BaseModel):
    """Omitted field = leave alone; explicit None = clear. `model_fields_set` tells them apart."""

    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    cached_input_per_mtok: Decimal | None = None
    per_minute: Decimal | None = None
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason is required")
        return v

    @model_validator(mode="after")
    def _check(self) -> "PriceUpdate":
        validate_rates({k: getattr(self, k) for k in RATE_FIELDS if k in self.model_fields_set})
        return self
