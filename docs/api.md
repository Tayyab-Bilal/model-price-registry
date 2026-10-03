# API reference

## Money and rates

Rates are `Decimal` USD per million tokens (`input_per_mtok`, `output_per_mtok`,
`cached_input_per_mtok`) or per minute (`per_minute`). Storage is integer micro-dollars. Over HTTP
rates are strings. `Rates = dict[str, Decimal | None]`.

## `model_price_registry.db`

- `connect(path=":memory:", read_only=False) -> sqlite3.Connection`: autocommit connection; creates
  schema unless read-only.
- `tx(conn)`: context manager, `BEGIN IMMEDIATE` ... commit or roll back.
- `to_micro(Decimal | None) -> int | None`, `from_micro(int | None) -> Decimal | None`.
- `read_rates(conn, model_id) -> Rates` (`KeyError` if unknown). `RATE_FIELDS`, `TOKEN_FIELDS`.

## `model_price_registry.validation`

- `validate_rates(rates: Rates) -> None`: raises `ValueError` for a negative rate, a rate that rounds
  to zero, cached above input, or per-token plus per-minute.
- `PriceUpdate(input_per_mtok=None, output_per_mtok=None, cached_input_per_mtok=None, per_minute=None, reason)`:
  pydantic model. Only fields that were set are applied; `None` clears. `reason` is required.

## `model_price_registry.registry`

- `apply_update(conn, model_id, update, actor, provenance) -> None`: the write path. The caller owns the
  transaction. Validates the merged row, writes the row and a history row.
- `Registry(conn, clock=time.monotonic, ttl=60.0)`
  - `get_model(model_id) -> Model` (`model_id`, `provider`, `rates`, `provenance`, `updated_at`)
  - `update_price(model_id, update, actor) -> Model`: transaction, `admin` provenance, cache invalidated.
  - `cost_for(model_id, input_tokens, output_tokens, cached_tokens=0) -> Decimal`: USD. Falls back to
    the input rate for cached tokens when no cached rate is set.
  - `cache: RateCache` with `get(model_id)` and `invalidate(model_id)`.

## `model_price_registry.proposals`

- `file_proposal(reg, model_id, rates, source) -> (outcome, id)`: `outcome` is `filed`, `replaced` or
  `kept`. `source` is one of `STRENGTH`: `consensus` < `api` < `provider_page`. Raises `ValueError` for
  a draft that could never be approved.
- `approve(reg, id, actor)`, `reject(reg, id, actor)`: raise `KeyError` (unknown), `DecisionError`
  (already decided), `StaleProposal` (rates changed since the snapshot).
- `bulk_decide(reg, ids, "approve" | "reject", actor) -> dict[int, str]`: one transaction per id;
  values are `approved`, `rejected`, `stale` or `error: ...`. `ValueError` above `MAX_BULK` (200).
- `list_pending(reg) -> list[sqlite3.Row]`.

## `model_price_registry.sync`

- `run_sync(reg, sources, now, dry_run=False, stale_after=30 min) -> Report`: takes the run-row lock
  (`SyncAlreadyRunning` if held), reaps stale rows, files drafts. A source exception becomes
  `Report.error` and a failed run row.
- `Report`: `proposed`, `unchanged`, `not_covered`, `rejected_by_trust_checks`, `error`, `dry_run`.
- `exit_code(report) -> int`, `format_report(report) -> str`, `main(argv) -> int`
  (`--db`, `--fixtures`, `--page`, `--dry-run`).

## `model_price_registry.sources` and `units`

- A `Source` is `Callable[[], SourceResult]`; `SourceResult(name, quotes, rejected)`.
- `api_source.from_payload(payload)`, `page_source.from_html(html, registry_ids)` (trust checks:
  at least 5 models, no price above $500/Mtok, at least 25% of registry models covered),
  `consensus_source.from_payloads({name: payload})` (two or more exact, no disagreement).
- `Quote(model_id, axis, rate_per_unit, source)`; `compare(quote, rates) -> (Outcome, column, rate)`
  with `Outcome` of `SAME`, `DIFFERENT`, `NOT_COVERED`. Axes: `text_input_token`,
  `text_output_token`, `cached_input_token`, `audio_input_token` (no column), `audio_minute`,
  `audio_second` (x60 into `per_minute`), `rerank_search` (no column).

## `model_price_registry.seed`, `backfill`, `checks`

- `seed(conn, models=SEED_MODELS) -> int`: insert-only; returns rows inserted.
- `backfill_cached_rates(conn, defaults: dict[str, Decimal]) -> int`: fills NULL cached rates on
  migration-provenance, token-billed rows when the default is positive and at most the input rate.
  `downgrade_backfill(conn) -> int` always returns 0 and changes nothing.
- `check_registry_rates(conn, expected) -> None`: raises `RegistryCheckError` (`.problems`). Exact
  match for `migration` rows; every row must pass `validate_rates`.

## HTTP API (`model_price_registry.api`)

`create_router(reg, require_admin, sources, clock) -> APIRouter` and `create_app(...)`, which mounts
the router at `/admin`. `require_admin` is a FastAPI dependency returning the actor name or raising
401/403; it guards every route and its result is recorded as the actor.

| Method and path | What |
|---|---|
| `GET /admin/models` | all models |
| `GET /admin/models/{id}` | one model (404 if unknown) |
| `PATCH /admin/models/{id}/pricing` | the only direct price write; body is a `PriceUpdate`; 422 on invalid |
| `GET /admin/models/{id}/history` | before/after rows, oldest first |
| `GET /admin/proposals?status=` | proposals, optional `pending`, `approved`, `rejected` |
| `POST /admin/proposals/{id}/approve` | approve; 409 if stale or already decided |
| `POST /admin/proposals/{id}/reject` | reject; 409 if already decided |
| `POST /admin/proposals/bulk-approve` | body `{"ids": [...]}`, at most 200 (else 422); per-id outcomes |
| `POST /admin/proposals/bulk-reject` | same |
| `GET /admin/sync-runs` | run rows, newest first |
| `POST /admin/sync/trigger` | run a sync now; 409 while a live run holds the lock |

Error mapping: unknown id 404, `StaleProposal` and `DecisionError` 409, `SyncAlreadyRunning` 409,
validation and the bulk cap 422, missing or non-admin credentials whatever `require_admin` raises.

```python
from fastapi import HTTPException

from model_price_registry.api import create_router
from model_price_registry.db import connect
from model_price_registry.registry import Registry
from model_price_registry.seed import seed

conn = connect()
seed(conn)
router = create_router(Registry(conn), lambda: "me", lambda: [], lambda: None)
assert len(router.routes) == 11
assert HTTPException(409).status_code == 409
```
