# Usage guide

Every snippet here runs; `tests/test_docs.py` executes them in order, one namespace per file.
Run from the repo root.

## Set up

```python
from decimal import Decimal as D

from model_price_registry.db import connect
from model_price_registry.registry import Registry
from model_price_registry.seed import SEED_MODELS, seed

conn = connect()  # ":memory:"; pass a path for a file, created with the schema if missing
assert seed(conn) == len(SEED_MODELS)
assert seed(conn) == 0  # insert-only: a second start changes nothing
reg = Registry(conn)  # owns a 60 s rate cache; pass ttl= and clock= to control it
```

## Change a price (the only direct write path)

`PriceUpdate` separates "omitted" from "explicit null": omitted leaves a rate alone, `None` clears it.

```python
from model_price_registry.validation import PriceUpdate

reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="renegotiated"), "alice")
reg.update_price("acme-code-pro", PriceUpdate(cached_input_per_mtok=None, reason="no cache"), "alice")
rates = reg.get_model("acme-code-pro").rates
assert rates["output_per_mtok"] == D("9") and rates["cached_input_per_mtok"] is None
assert rates["input_per_mtok"] == D("2")  # never mentioned, never touched
```

Bad updates raise `ValueError` (pydantic's `ValidationError` is one) and change nothing:

```python
import pytest

for bad in ({"input_per_mtok": D("-1")}, {"per_minute": D("0.01")}, {"cached_input_per_mtok": D("9")}):
    with pytest.raises(ValueError):
        reg.update_price("acme-chat-small", PriceUpdate(reason="x", **bad), "alice")
```

The last one is only invalid against the *merged* row (cached 9 above the stored input 0.25), which is
why `apply_update` validates the result and not just the request.

## Cached tokens and the backfill

A model with no cached rate bills cached tokens at the full input rate. The backfill fills the gap
for rows still at migration provenance, never overwrites, and never exceeds the input rate.

```python
from model_price_registry.backfill import backfill_cached_rates, downgrade_backfill

conn.execute("UPDATE models SET cached_input_per_mtok=NULL WHERE model_id='acme-chat-small'")
reg.cache.invalidate("acme-chat-small")
assert reg.cost_for("acme-chat-small", 0, 0, cached_tokens=1_000_000) == D("0.25")  # overcharge
assert backfill_cached_rates(conn, {"acme-chat-small": D("0.03")}) == 1
reg.cache.invalidate("acme-chat-small")
assert reg.cost_for("acme-chat-small", 0, 0, cached_tokens=1_000_000) == D("0.03")
assert downgrade_backfill(conn) == 0  # a rollback leaves the fix in place
```

## Drafts and decisions

```python
from model_price_registry.proposals import (
    StaleProposal, approve, bulk_decide, file_proposal, list_pending, reject,
)

_, a = file_proposal(reg, "acme-chat-large", {"output_per_mtok": D("18")}, "api")
assert file_proposal(reg, "acme-chat-large", {"output_per_mtok": D("17")}, "consensus")[0] == "kept"
assert file_proposal(reg, "acme-chat-large", {"output_per_mtok": D("16")}, "provider_page")[0] == "replaced"
assert [p["source"] for p in list_pending(reg)] == ["provider_page"]
approve(reg, a, "bob")
assert reg.get_model("acme-chat-large").provenance == "sync"
```

Source ranking is consensus < api < provider_page. Only "consensus never overrides a provider page"
comes from the original design; the API's place in the middle is this repo's choice.

A hand edit between filing and approving makes the draft stale:

```python
_, b = file_proposal(reg, "acme-embed-v2", {"input_per_mtok": D("0.12")}, "api")
conn.execute("UPDATE models SET input_per_mtok=110000 WHERE model_id='acme-embed-v2'")
try:
    approve(reg, b, "bob")
except StaleProposal as e:
    print("refused:", e)  # HTTP 409 through the API
assert bulk_decide(reg, [b], "reject", "bob") == {b: "rejected"}
```

Bulk calls take at most 200 ids (`ValueError` above that) and report one outcome per id.

## Sync and the CLI

```python
from datetime import UTC, datetime

from model_price_registry.sources.fixtures import fixture_sources
from model_price_registry.sync import exit_code, format_report, run_sync

ids = {r["model_id"] for r in conn.execute("SELECT model_id FROM models")}
now = datetime.now(UTC)
report = run_sync(reg, fixture_sources("tests/fixtures", ids), now, dry_run=True)
print(format_report(report))
assert exit_code(report) == 0 and ("acme-live-1", "audio_input_token") in report.not_covered
```

A real run (no `dry_run`) takes the run-row lock first and raises `SyncAlreadyRunning` if a live run
holds it. A `running` row older than `stale_after` (30 minutes) is marked failed and replaced.

```bash
python -m model_price_registry.sync --db r.db --dry-run                      # fresh path is seeded
python -m model_price_registry.sync --db r.db --page provider_page_good.html  # real run, good page
```

## Write your own source

A source is a zero-argument callable returning `SourceResult`. It never writes. Name the axis of every
quote; it is compared only with the column on that axis.

```python
from model_price_registry.sources.common import SourceResult
from model_price_registry.units import Quote


def my_source() -> SourceResult:
    return SourceResult("api", [Quote("acme-code-pro", "text_input_token", D("0.0000021"), "api")])


report = run_sync(reg, [my_source], now)
assert report.proposed == [("acme-code-pro", "api", "filed")]
```

## Check the registry in CI

```python
from model_price_registry.checks import RegistryCheckError, check_registry_rates

published = {m[0]: dict(zip(("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "per_minute"),
                            m[2:], strict=True)) for m in SEED_MODELS}
try:
    check_registry_rates(conn, published)
except RegistryCheckError as e:
    # acme-code-pro (admin) and acme-chat-large (sync) differ from the list and are exempt. The
    # raw UPDATE on acme-embed-v2 left its provenance at "migration", so the exact check catches it.
    assert e.problems == ["acme-embed-v2: migration rates differ from the expected list"]
else:
    raise AssertionError("expected the exact check to fail")
```

Rows at `migration` provenance must equal the published rates. Rows with `admin` or `sync`
provenance are exempt from that, but every row must pass the invariants.

## Serve the admin API

See [api.md](api.md) for routes. Wiring:

```python
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from model_price_registry.api import create_app


def require_admin(x_actor: str | None = Header(default=None)) -> str:
    if x_actor is None:
        raise HTTPException(401, "not signed in")
    return x_actor  # replace with your real session and role check


app = create_app(reg, require_admin, lambda: [], lambda: now)
client = TestClient(app, headers={"x-actor": "alice"})
assert client.get("/admin/models/acme-code-pro").json()["rates"]["output_per_mtok"] == "9"
```
