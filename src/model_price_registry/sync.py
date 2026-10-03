"""Sync run: collect quotes from sources, file drafts where they differ. Never writes a price."""
import argparse
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .db import connect, read_rates, tx
from .proposals import file_proposal
from .registry import Registry
from .seed import seed
from .sources.common import Source
from .units import Outcome, compare


class SyncAlreadyRunning(Exception):
    pass


@dataclass
class Report:
    proposed: list[tuple[str, str, str]] = field(default_factory=list)  # model, source, outcome
    unchanged: list[tuple[str, str]] = field(default_factory=list)  # model, axis
    not_covered: list[tuple[str, str]] = field(default_factory=list)  # model, axis
    rejected_by_trust_checks: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    dry_run: bool = False


def exit_code(report: Report) -> int:
    """Shared by the CLI and any scheduler wrapper: a failed run must page someone."""
    return 1 if report.error else 0


def _acquire(conn: sqlite3.Connection, now: datetime, stale_after: timedelta) -> int:
    """The run row is the lock. A crashed worker leaves 'running' behind, so old ones are reaped."""
    with tx(conn):
        conn.execute(
            "UPDATE sync_runs SET status='failed', finished_at=?, error='reaped: stale running row'"
            " WHERE status='running' AND started_at < ?",
            (now.isoformat(), (now - stale_after).isoformat()),
        )
        if conn.execute("SELECT 1 FROM sync_runs WHERE status='running'").fetchone():
            raise SyncAlreadyRunning("another sync run is in progress")
        return conn.execute("INSERT INTO sync_runs(status, started_at) VALUES ('running', ?)",
                            (now.isoformat(),)).lastrowid


def _collect(reg: Registry, sources: list[Source], dry_run: bool) -> Report:
    report = Report(dry_run=dry_run)
    results = [src() for src in sources]
    drafts: dict[tuple[str, str], dict] = {}
    seen_same, seen_nc = set(), set()
    for res in results:
        if res.rejected:
            report.rejected_by_trust_checks[res.name] = res.rejected
        for q in res.quotes:
            try:
                rates = read_rates(reg.conn, q.model_id)
            except KeyError:
                continue  # not our model
            outcome, column, rate = compare(q, rates)
            if outcome is Outcome.NOT_COVERED:
                seen_nc.add((q.model_id, q.axis))
            elif outcome is Outcome.SAME:
                seen_same.add((q.model_id, q.axis))
            else:
                drafts.setdefault((q.model_id, q.source), {})[column] = rate
    report.unchanged, report.not_covered = sorted(seen_same), sorted(seen_nc)
    for (model_id, source), rates in drafts.items():
        if dry_run:
            report.proposed.append((model_id, source, "would_file"))
            continue
        try:
            outcome, _ = file_proposal(reg, model_id, rates, source)
            report.proposed.append((model_id, source, outcome))
        except ValueError as e:  # a draft that could never be approved is noise, not a proposal
            report.rejected_by_trust_checks[f"{model_id}/{source}"] = str(e)
    return report


def run_sync(reg: Registry, sources: list[Source], now: datetime, dry_run: bool = False,
             stale_after: timedelta = timedelta(minutes=30)) -> Report:
    if dry_run:
        return _collect(reg, sources, dry_run=True)
    run_id = _acquire(reg.conn, now, stale_after)
    try:
        report = _collect(reg, sources, dry_run=False)
    except Exception as e:  # any source blowing up fails the run, visibly
        report = Report(error=f"{type(e).__name__}: {e}")
    reg.conn.execute(
        "UPDATE sync_runs SET status=?, finished_at=?, error=? WHERE id=?",
        ("failed" if report.error else "succeeded", now.isoformat(), report.error, run_id),
    )
    return report


def format_report(r: Report) -> str:
    lines = [f"sync report{' (dry run)' if r.dry_run else ''}"]
    if r.error:
        lines.append(f"  ERROR: {r.error}")
    lines += [f"  proposed     {m:<18} from {s:<13} [{o}]" for m, s, o in r.proposed]
    lines += [f"  not covered  {m:<18} axis={a}" for m, a in r.not_covered]
    lines += [f"  trust check  {n:<18} {why}" for n, why in r.rejected_by_trust_checks.items()]
    lines.append(f"  unchanged    {len(r.unchanged)} model/axis pairs match the registry")
    return "\n".join(lines)


def main(argv: list[str] | None = None,
         clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> int:
    from .sources.fixtures import fixture_sources

    p = argparse.ArgumentParser(prog="python -m model_price_registry.sync")
    p.add_argument("--db", default="registry.db", help="created and seeded if missing")
    p.add_argument("--fixtures", default="tests/fixtures", type=Path)
    p.add_argument("--page", default="provider_page_lowcov.html", help="provider page fixture to use")
    p.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    args = p.parse_args(argv)
    if args.db != ":memory:" and not Path(args.db).exists():
        seed(connect(args.db))  # a fresh path gets the schema and the seed rows, so the CLI just works
    reg = Registry(connect(args.db, read_only=args.dry_run))
    ids = {r["model_id"] for r in reg.conn.execute("SELECT model_id FROM models")}
    report = run_sync(reg, fixture_sources(args.fixtures, ids, args.page), clock(), dry_run=args.dry_run)
    print(format_report(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
