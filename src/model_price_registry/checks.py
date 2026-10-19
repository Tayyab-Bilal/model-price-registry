"""CI-style check of the registry against published rates, with a provenance rule.

Rows still at `migration` provenance came from reviewed code, so their rates must match the
published list exactly. Rows an admin or a sync edited at runtime have moved on by design, so an
exact comparison would fail every legitimate change. They are exempt from the exact check but stay
subject to every invariant, otherwise "exempt" would mean "unchecked".
"""
import sqlite3
from decimal import Decimal

from .db import RATE_FIELDS, from_micro
from .validation import validate_rates

Rates = dict[str, Decimal | None]


class RegistryCheckError(AssertionError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def check_registry_rates(conn: sqlite3.Connection, expected: dict[str, Rates]) -> None:
    """Raise `RegistryCheckError` listing every problem; return None when the registry is sound."""
    problems: list[str] = []
    seen: set[str] = set()
    for row in conn.execute("SELECT * FROM models ORDER BY model_id"):
        model_id = row["model_id"]
        seen.add(model_id)
        rates = {f: from_micro(row[f]) for f in RATE_FIELDS}
        try:
            validate_rates(rates)
        except ValueError as e:
            problems.append(f"{model_id}: invariant broken ({e})")
        if row["provenance"] != "migration":
            continue  # runtime-edited: exact rates are not expected to match
        if model_id not in expected:
            problems.append(f"{model_id}: migration row missing from the expected list")
        elif rates != {f: expected[model_id].get(f) for f in RATE_FIELDS}:
            problems.append(f"{model_id}: migration rates differ from the expected list")
    problems += [f"{m}: expected model missing from the registry" for m in sorted(set(expected) - seen)]
    if problems:
        raise RegistryCheckError(problems)
