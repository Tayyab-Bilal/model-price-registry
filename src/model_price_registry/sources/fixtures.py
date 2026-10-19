"""Wire the recorded fixtures up as sources (used by the CLI and the demo; no network)."""
from pathlib import Path

from . import api_source, consensus_source, page_source
from .common import Source, load_json


# Simplification: one generic table parser and one page fixture stand in for the three provider
# page extractors; real extractors share the same trust checks and differ only in parsing.
def fixture_sources(fixtures: Path, registry_ids: set[str],
                    page: str = "provider_page_lowcov.html") -> list[Source]:
    fixtures = Path(fixtures)
    return [
        lambda: api_source.from_payload(load_json(fixtures / "api_prices.json")),
        lambda: page_source.from_html((fixtures / page).read_text(),
                                      registry_ids),
        lambda: consensus_source.from_payloads(
            {n: load_json(fixtures / f"{n}.json") for n in consensus_source.NORMALISERS}),
    ]
