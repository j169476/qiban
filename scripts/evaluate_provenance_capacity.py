"""Isolated real-core continuity benchmark. Model and outbound platform are stubs."""
# ruff: noqa: E402
import argparse
import asyncio
import json
import secrets
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion_core.access import AccessService, GraphLimits
from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository
from companion_core.postgres import PostgresStore
from companion_core.runtime import Companion
from scripts.benchmark_metrics import Metrics, rss
from scripts.capacity_timings import CapacityTimings
from tests_postgres.test_runtime import Adapter, Host, Model


class Growing(Model):
    calls = 0

    async def generate(self, instructions, context, schema, images=(), **options):
        self.calls += 1
        if "queries" in schema["properties"]:
            return {"queries": ["sketch discussion"]}
        value = await super().generate(instructions, context, schema, images, **options)
        self.contexts.clear()
        value["relationship"] = {"delta": 1, "impression": "synthetic continuity", "facts": ["likes sketches"]}
        return value


async def run(store, size, output, *, timings=False, start_time=None):
    now = datetime.fromisoformat(start_time) if start_time else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("start_time must include a timezone")
    adapter, model = Adapter(), Growing()
    # Twenty turns per simulated day respect actual default action/model quotas.
    clock = [now]
    core = Companion(store, model, Host(), [adapter], clock=lambda: clock[0])
    core.start()
    report = {"requested_turns": size, "completed_turns": 0, "failure": None, "synthetic_only": True,
              "model_and_platform_stubs": True, "simulated_turns_per_day": 20,
              "affinity_and_provenance_enabled": True, "limits": {"direct": 256, **asdict(GraphLimits())},
              "turns": [], "checks": {"count": 0, "max_seconds": 0}, "milestones": {},
              "start_time": now.isoformat()}
    original = AccessService._lease

    def measured(access, *args, **kwargs):
        start = time.perf_counter()
        try:
            return original(access, *args, **kwargs)
        finally:
            report["checks"]["count"] += 1
            report["checks"]["max_seconds"] = max(report["checks"]["max_seconds"], time.perf_counter() - start)

    AccessService._lease = measured
    try:
        with Metrics() as metrics, CapacityTimings(timings) as diagnostics, metrics.stage(report, "total"):
            for index in range(size):
                clock[0] = now + timedelta(days=index // 20, seconds=index)
                event = Event("alpha", "bot", "capacity-" + str(index), ActorRef("alpha", "a"), "private-a",
                              clock[0].isoformat(), "sketch discussion " + str(index), "private")
                before = store.version("relationships", event.actor.key)
                started, sql = time.perf_counter(), metrics.statements
                try:
                    outcome = await core.handle(event)
                    assert outcome["action"]["status"] == "succeeded"
                    assert (await core.handle(event))["duplicate"]
                except AssistantError as exc:
                    report["failure"] = {"turn": index + 1, "code": exc.code,
                        "relationship_rolled_back": before == store.version("relationships", event.actor.key)}
                    break
                report["completed_turns"] += 1
                report["turns"].append({"seconds": round(time.perf_counter() - started, 4),
                                        "sql": metrics.statements - sql})
                if (index + 1) % 25 == 0:
                    report["timings"] = diagnostics.report()
                    print(json.dumps({"requested": size, "completed": index + 1, **report["turns"][-1]}), flush=True)
                    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                if index + 1 in {100, 500, 1000}:
                    with store.db() as db:
                        nodes = db.execute("SELECT COUNT(*) FROM governance_policies").fetchone()[0]
                        edges = db.execute("SELECT COUNT(*) FROM source_dependencies").fetchone()[0]
                    relationship = GovernanceRepository(store).policy("relationships/" + event.actor.key)
                    report["milestones"][str(index + 1)] = {"stored_nodes": nodes, "stored_edges": edges,
                        "relationship_direct_sources": len(relationship.sources), **rss()}
        report["timings"] = diagnostics.report()
        report["sent"] = len(adapter.sent)
        report["model_calls"] = model.calls
        report["passed"] = report["completed_turns"] == size and len(adapter.sent) == size
        return report
    finally:
        AccessService._lease = original
        core.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, choices=(100, 500, 1000), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=ROOT / ".dev/postgres-test-v2/connection.json")
    parser.add_argument("--timings", action="store_true", help="Collect bounded aggregate diagnostics")
    parser.add_argument("--start-time", help="Fixed timezone-aware synthetic starting time")
    parser.add_argument("--profile", type=Path, help="Optional cProfile output; do not compare with unprofiled runs")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    options = json.loads(args.database.read_text(encoding="utf-8"))
    assert options["dbname"] == "companion_test" and options["host"] == "127.0.0.1"
    schema = "capacity_" + uuid.uuid4().hex
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute('CREATE SCHEMA "' + schema + '"')
    try:
        with tempfile.TemporaryDirectory(prefix="companion-capacity-") as folder:
            store = PostgresStore(Path(folder), {**options, "options": "-c search_path=" + schema}, secrets.token_bytes(32))
            try:
                store.migrate()
                store.initialize()
                if args.profile:
                    import cProfile

                    profiler = cProfile.Profile()
                    profiler.enable()
                try:
                    report = asyncio.run(run(store, args.turns, args.output,
                                             timings=args.timings, start_time=args.start_time))
                finally:
                    if args.profile:
                        profiler.disable()
                        profiler.dump_stats(args.profile)
            finally:
                store.database.close()
    finally:
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('DROP SCHEMA "' + schema + '" CASCADE')
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "turns"}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
