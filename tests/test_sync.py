from datetime import timedelta

import pytest
from conftest import FIX, NOW, count

from model_price_registry.db import connect
from model_price_registry.registry import Registry
from model_price_registry.seed import seed
from model_price_registry.sources.common import SourceResult
from model_price_registry.sources.fixtures import fixture_sources
from model_price_registry.sync import SyncAlreadyRunning, exit_code, main, run_sync

IDS = {"acme-chat-large", "acme-chat-small", "acme-code-pro", "acme-embed-v2",
       "acme-rerank-1", "acme-listen-1"}


def _insert_running(reg, started):
    reg.conn.execute("INSERT INTO sync_runs(status, started_at) VALUES ('running', ?)",
                     (started.isoformat(),))


def test_running_row_blocks_second_run(reg):
    _insert_running(reg, NOW - timedelta(minutes=1))
    with pytest.raises(SyncAlreadyRunning):
        run_sync(reg, fixture_sources(FIX, IDS), NOW)
    assert count(reg, "proposals") == 0 and count(reg, "sync_runs") == 1


def test_stale_run_reaped(reg):
    _insert_running(reg, NOW - timedelta(hours=2))
    report = run_sync(reg, fixture_sources(FIX, IDS), NOW)
    assert exit_code(report) == 0
    rows = reg.conn.execute("SELECT status, error FROM sync_runs ORDER BY id").fetchall()
    assert rows[0]["status"] == "failed" and "reaped" in rows[0]["error"]
    assert rows[1]["status"] == "succeeded"
    assert count(reg, "sync_runs", "status='running'") == 0


def test_full_run_report(reg):
    report = run_sync(reg, fixture_sources(FIX, IDS), NOW)
    outcomes = {(m, s): o for m, s, o in report.proposed}
    assert outcomes[("acme-chat-large", "api")] == "filed"
    assert outcomes[("acme-chat-large", "consensus")] == "kept"  # api is the stronger source
    assert outcomes[("acme-code-pro", "consensus")] == "filed"
    assert ("acme-rerank-1", "rerank_search") in report.not_covered
    assert "provider_page" in report.rejected_by_trust_checks
    assert count(reg, "proposals", "status='pending'") == 2


def test_dry_run_writes_nothing(reg):
    report = run_sync(reg, fixture_sources(FIX, IDS), NOW, dry_run=True)
    assert report.proposed and report.dry_run
    assert count(reg, "proposals") == 0 and count(reg, "sync_runs") == 0
    assert count(reg, "price_history") == 0


def test_dry_run_cli_writes_nothing(tmp_path, capsys):
    db = tmp_path / "r.db"
    conn = connect(str(db))
    seed(conn)
    assert main(["--db", str(db), "--fixtures", str(FIX), "--dry-run"]) == 0
    assert "would_file" in capsys.readouterr().out
    check = Registry(connect(str(db)))
    assert count(check, "proposals") == 0 and count(check, "sync_runs") == 0


def test_failed_run_nonzero_exit_code(reg):
    def exploding() -> SourceResult:
        raise RuntimeError("upstream 500")

    report = run_sync(reg, [exploding], NOW)
    assert exit_code(report) != 0 and "upstream 500" in report.error
    row = reg.conn.execute("SELECT status, error FROM sync_runs").fetchone()
    assert row["status"] == "failed" and "upstream 500" in row["error"]
    assert exit_code(run_sync(reg, [], NOW)) == 0


def test_dry_run_cli_on_a_fresh_path_creates_and_seeds(tmp_path, capsys):
    db = tmp_path / "fresh" / "r.db"
    db.parent.mkdir()
    assert main(["--db", str(db), "--fixtures", str(FIX), "--dry-run"]) == 0
    assert "would_file" in capsys.readouterr().out
    check = Registry(connect(str(db)))
    assert count(check, "models") == 7 and count(check, "proposals") == 0


def test_good_provider_page_files_a_draft_from_the_page(reg):
    report = run_sync(reg, fixture_sources(FIX, IDS | {"acme-live-1"}, "provider_page_good.html"), NOW)
    assert ("acme-chat-small", "provider_page", "filed") in report.proposed
    assert "provider_page" not in report.rejected_by_trust_checks
