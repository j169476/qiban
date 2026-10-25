import json

import psycopg
import pytest

from companion_core.access import AccessService
from scripts.capacity_timings import CapacityTimings


def test_capacity_diagnostics_only_report_aggregate_metadata(pg_store):
    secret = "synthetic-value-never-in-diagnostic-report"
    with CapacityTimings(True) as diagnostics, pg_store.db() as db:
        assert db.execute("SELECT ? AS probe", (secret,)).fetchone()[0] == secret
    report = diagnostics.report()
    assert report["enabled"] and report["sql"]
    assert secret not in json.dumps(report)
    assert all(set(row) == {"calls", "seconds", "max_seconds"} for row in report["sql"].values())


def test_capacity_diagnostics_restore_hooks_after_failure():
    lease, execute = AccessService._lease, psycopg.Cursor.execute
    with pytest.raises(RuntimeError), CapacityTimings(True):
        assert AccessService._lease is not lease and psycopg.Cursor.execute is not execute
        raise RuntimeError("synthetic interruption")
    assert AccessService._lease is lease and psycopg.Cursor.execute is execute
    with CapacityTimings(False) as disabled:
        assert AccessService._lease is lease and psycopg.Cursor.execute is execute
    assert disabled.report()["functions"] == disabled.report()["sql"] == {}
