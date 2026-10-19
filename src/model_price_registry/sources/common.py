import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..units import Quote


@dataclass
class SourceResult:
    name: str
    quotes: list[Quote] = field(default_factory=list)
    rejected: str | None = None  # set when a trust check blocked the whole source


Source = Callable[[], SourceResult]


def load_json(path: Path):
    """Parse numbers as Decimal straight from the text, so 3e-06 never passes through a float."""
    return json.loads(Path(path).read_text(), parse_float=Decimal)
