"""Opt-in isolated relation graph benchmark with hidden nodes and claims."""
import argparse
import json
import secrets
import statistics
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from benchmark_metrics import Metrics

from companion_core.access import ActorRef
from companion_core.governance import AdminService
from companion_core.postgres import PostgresStore
from companion_core.relations import RelationService


def run(args, metrics):
    options = json.loads(args.connection.read_text(encoding="utf-8"))
    if options.get("dbname") != "companion_test" or options.get("host") != "127.0.0.1":
        raise ValueError("Only the dedicated synthetic database is allowed")
    report = {"synthetic": True, "model_calls": 0, "platform_calls": 0, "scales": []}
    for size in args.edges:
        schema = "graph_" + uuid.uuid4().hex
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('CREATE SCHEMA "' + schema + '"')
        try:
            with tempfile.TemporaryDirectory(prefix="companion-graph-") as folder:
                store = PostgresStore(Path(folder), {**options, "options": "-c search_path=" + schema}, secrets.token_bytes(32))
                try:
                    store.migrate()
                    store.initialize()
                    actors = [ActorRef("fixture", str(i)) for i in range(size // 10)]
                    visible = {actor.key for i, actor in enumerate(actors) if i % 5 != 4}
                    owner = actors[0]
                    with store.db() as db:
                        with db.connection.cursor().copy("COPY actor_nodes FROM STDIN") as cp:
                            for actor in actors:
                                cp.write_row((actor.key, actor.platform, store.ledger_encode("actor_nodes.identity", actor.as_dict())))
                        with db.connection.cursor().copy("COPY relationship_claims FROM STDIN") as cp:
                            for i in range(size):
                                subject, target = actors[i % len(actors)].key, actors[(i * 13 + 1) % len(actors)].key
                                cp.write_row((f"edge-{i:06}", subject, target, owner.key, "claimed", "v1",
                                              store.ledger_encode("relationship_claims.body", {
                                                  "relation": "colleague", "reason": "synthetic", "evidence": [],
                                                  "direction": "subject_to_object"})))
                        with db.connection.cursor().copy("COPY governance_policies FROM STDIN") as cp:
                            for actor in actors:
                                cp.write_row(("actor/" + actor.key, "v1", "public" if actor.key in visible else "secret",
                                              "[]", "", "private", "[]", 1, None, "[]"))
                            for i in range(size):
                                cp.write_row((f"relation/edge-{i:06}", "v1", "personal" if i % 3 == 2 else "public",
                                              '["hidden-owner"]' if i % 3 == 2 else "[]", "", "private", "[]", 1, None, "[]"))
                    admin = AdminService(store)
                    request = admin.authority.issue(actor=owner, conversation="fixture", audience="private", recipients=(owner,), lifetime=600)
                    graph = RelationService(admin.repository, admin.authority)
                    item = {"nodes": len(actors), "edges": size, "runs": []}
                    with metrics.stage(item, "measured"):
                        for depth in (1, 2):
                            timings = []
                            for _ in range(6):
                                started = time.perf_counter()
                                value = graph.query(request, owner.key, depth=depth)
                                timings.append(time.perf_counter() - started)
                                assert len(value["claims"]) <= 40
                                assert all(claim["subject"] in visible and claim["object"] in visible
                                           and int(claim["id"].split("-")[-1]) % 3 != 2 for claim in value["claims"])
                            item["runs"].append({"depth": depth, "median_seconds": statistics.median(timings),
                                                 "p95_seconds": max(timings), "claims": len(value["claims"]),
                                                 "truncated": value["truncated"], "hidden_leaks": 0})
                    report["scales"].append(item)
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    print(json.dumps(item), flush=True)
                finally:
                    store.database.close()
        finally:
            with psycopg.connect(**options, autocommit=True) as db:
                db.execute('DROP SCHEMA "' + schema + '" CASCADE')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edges", type=int, nargs="+", default=[10000, 50000])
    with Metrics() as metrics:
        run(parser.parse_args(), metrics)
