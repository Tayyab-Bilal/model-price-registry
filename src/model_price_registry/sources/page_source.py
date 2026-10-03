"""Provider pricing page (HTML table). Scraped pages break silently, so trust checks come first."""
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from ..units import Quote
from .common import SourceResult

MIN_MODELS = 5
MAX_PER_MTOK = Decimal(500)
MIN_COVERAGE = Decimal("0.25")


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.header: list[str] = []
        self._cell: list[str] | None = None
        self._row: list[str] = []
        self._is_header = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row, self._is_header = [], False
        elif tag in ("td", "th"):
            self._cell = []
            self._is_header = self._is_header or tag == "th"

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row:
            if self._is_header:
                self.header = self._row
            else:
                self.rows.append(self._row)


def _axis(header: str) -> str | None:
    h = header.lower()
    if "cached" in h:
        return "cached_input_token"
    if "input" in h:
        return "text_input_token"
    if "output" in h:
        return "text_output_token"
    return None


def from_html(html: str, registry_ids: set[str]) -> SourceResult:
    t = _Table()
    t.feed(html)
    axes = [_axis(h) for h in t.header]
    parsed: list[tuple[str, str, Decimal]] = []  # (model, axis, USD per Mtok)
    for row in t.rows:
        for axis, cell in zip(axes[1:], row[1:], strict=False):
            text = cell.replace("$", "").replace(",", "")
            if axis is None or text in ("", "-"):
                continue
            try:
                parsed.append((row[0], axis, Decimal(text)))
            except InvalidOperation:
                return SourceResult("provider_page", rejected=f"unparseable price {cell!r}")
    models = {m for m, _, _ in parsed}
    if len(models) < MIN_MODELS:
        return SourceResult("provider_page", rejected=f"only {len(models)} models (min {MIN_MODELS})")
    if any(rate > MAX_PER_MTOK for _, _, rate in parsed):
        return SourceResult("provider_page", rejected=f"price above ${MAX_PER_MTOK}/Mtok cap")
    coverage = Decimal(len(models & registry_ids)) / Decimal(max(len(registry_ids), 1))
    if coverage < MIN_COVERAGE:
        return SourceResult("provider_page",
                            rejected=f"covers {coverage:.0%} of registry (min {MIN_COVERAGE:.0%})")
    return SourceResult("provider_page",
                        [Quote(m, a, r / Decimal(1_000_000), "provider_page") for m, a, r in parsed])
