"""End-to-end story: seed -> sync fixtures -> review drafts -> approve -> bill changes."""
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from model_price_registry.db import connect
from model_price_registry.proposals import StaleProposal, approve, list_pending
from model_price_registry.registry import Registry
from model_price_registry.seed import seed
from model_price_registry.sources.fixtures import fixture_sources
from model_price_registry.sync import format_report, run_sync
from model_price_registry.validation import PriceUpdate

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
CALL = ("acme-chat-large", 1_000_000, 100_000)  # model, input tokens, output tokens


def main() -> None:
    reg = Registry(connect())
    print(f"1. seeded {seed(reg.conn)} models")
    print(f"   bill for 1M in + 100k out on acme-chat-large: ${reg.cost_for(*CALL)}\n")

    ids = {r["model_id"] for r in reg.conn.execute("SELECT model_id FROM models")}
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    print("2. sync over recorded fixtures (api + provider page + community consensus)")
    print("   the page is a low-coverage one, so its trust check rejects it")
    print(format_report(run_sync(reg, fixture_sources(FIXTURES, ids), now)), "\n")

    print("2b. the page layout is fixed: same sync, now with the full provider page")
    good = fixture_sources(FIXTURES, ids, "provider_page_good.html")
    print(format_report(run_sync(reg, good, now)), "\n")

    print("3. pending drafts (nothing has moved money yet)")
    drafts = {d["model_id"]: d for d in list_pending(reg)}
    for d in drafts.values():
        print(f"   #{d['id']} {d['model_id']:<16} {d['source']:<14} {d['proposed_json']}")
    print(f"   bill is still ${reg.cost_for(*CALL)}\n")

    print("4. a human approves the acme-chat-large draft")
    approve(reg, drafts["acme-chat-large"]["id"], "alice")
    print(f"   bill is now ${reg.cost_for(*CALL)}")
    h = reg.conn.execute("SELECT * FROM price_history").fetchone()
    print(f"   history: {h['actor']} via {h['source']}: {h['before_json']} -> {h['after_json']}\n")

    print("5. someone edits acme-code-pro by hand, then the older draft is approved")
    reg.update_price("acme-code-pro", PriceUpdate(output_per_mtok=D("9"), reason="renegotiated"),
                     "bob")
    try:
        approve(reg, drafts["acme-code-pro"]["id"], "alice")
    except StaleProposal as e:
        print(f"   conflict, refused: {e}")


if __name__ == "__main__":
    main()
