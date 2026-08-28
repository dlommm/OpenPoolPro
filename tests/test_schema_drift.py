"""Tripwire against the one schema bug the rest of the suite structurally cannot catch.

Every test builds its database with `Base.metadata.create_all`, which always
produces the CURRENT models. Production databases are provisioned once and then
patched forward by `app/migrate.py`'s ALTER TABLE list. So adding a column to
models.py and forgetting the matching `migrate.COLUMNS` entry passes all 880+
tests and then crashes production with "no such column" on the first request
after deploy.

Nothing can derive that automatically — migrate.py has no historical baseline
to diff against. So we pin the schema instead: when the committed snapshot and
the live schema disagree, this test fails and tells you which side to update.
"""
import json
import pathlib

import pytest
from sqlalchemy import inspect

from app import (
    migrate,
    models,  # noqa: F401  -- registers every table on Base.metadata
)
from app.db import Base, make_engine

SNAPSHOT_PATH = pathlib.Path(__file__).parent / "schema_snapshot.json"


def _live_schema() -> dict[str, list[str]]:
    """The schema a brand-new deployment gets: create_all, then migrate.run."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    migrate.run(engine)
    insp = inspect(engine)
    return {t: sorted(c["name"] for c in insp.get_columns(t))
            for t in sorted(insp.get_table_names())}


@pytest.mark.unit
def test_schema_matches_committed_snapshot():
    live = _live_schema()
    snapshot = json.loads(SNAPSHOT_PATH.read_text())

    added_tables = sorted(set(live) - set(snapshot))
    dropped_tables = sorted(set(snapshot) - set(live))
    changed = {t: (sorted(set(live[t]) - set(snapshot[t])),
                   sorted(set(snapshot[t]) - set(live[t])))
               for t in sorted(set(live) & set(snapshot))
               if live[t] != snapshot[t]}

    if not (added_tables or dropped_tables or changed):
        return

    lines = ["The database schema changed.", ""]
    if added_tables:
        lines += [f"NEW TABLES: {added_tables}",
                  "  New tables are fine — create_all builds them on every database,",
                  "  including existing ones. Just refresh the snapshot.", ""]
    if dropped_tables:
        lines += [f"REMOVED TABLES: {dropped_tables}", ""]
    for table, (added, removed) in changed.items():
        if added:
            in_migrate = {c for c, _ in migrate.COLUMNS.get(table, [])}
            missing = [c for c in added if c not in in_migrate]
            lines.append(f"NEW COLUMNS on existing table {table!r}: {added}")
            if missing:
                lines += [
                    f"  ==> {missing} are NOT in migrate.COLUMNS[{table!r}].",
                    "      An existing production database will never get them:",
                    "      create_all does not ALTER tables that already exist.",
                    "      Add them to app/migrate.py COLUMNS, then refresh the snapshot.",
                ]
            else:
                lines.append("  (all present in migrate.COLUMNS — just refresh the snapshot)")
        if removed:
            lines.append(f"COLUMNS GONE from {table!r}: {removed}")
        lines.append("")
    lines += ["Refresh with:",
              "  python -c \"import json,pathlib,sys; sys.path.insert(0,'.'); "
              "from tests.test_schema_drift import _live_schema, SNAPSHOT_PATH; "
              "SNAPSHOT_PATH.write_text(json.dumps(_live_schema(), indent=2) + chr(10))\""]
    pytest.fail("\n".join(lines))


@pytest.mark.unit
def test_migrate_is_idempotent():
    """migrate.run executes on every boot, so a second pass must be a no-op."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    migrate.run(engine)
    first = {t: sorted(c["name"] for c in inspect(engine).get_columns(t))
             for t in sorted(inspect(engine).get_table_names())}
    migrate.run(engine)
    second = {t: sorted(c["name"] for c in inspect(engine).get_columns(t))
              for t in sorted(inspect(engine).get_table_names())}
    assert first == second


@pytest.mark.unit
def test_every_migrate_column_exists_in_the_models():
    """A migrate.COLUMNS entry naming a column the models no longer declare is
    dead weight at best and a silently-diverging schema at worst."""
    live = _live_schema()
    stale = []
    for table, cols in migrate.COLUMNS.items():
        if table not in live:
            stale.append(f"{table} (table not in schema)")
            continue
        for name, _ddl in cols:
            if name not in live[table]:
                stale.append(f"{table}.{name}")
    assert not stale, f"migrate.COLUMNS references columns absent from the schema: {stale}"


@pytest.mark.unit
def test_declared_indexes_are_created():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    migrate.run(engine)
    insp = inspect(engine)
    for name, table, _cols in migrate.INDEXES:
        existing = {i["name"] for i in insp.get_indexes(table)}
        assert name in existing, f"{name} missing from {table}"
