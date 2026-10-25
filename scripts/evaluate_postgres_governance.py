"""Synthetic PostgreSQL scale/quality replay. Never accepts a production database."""
import argparse
import hashlib
import json
import secrets
import statistics
import sys
import time
import uuid
from pathlib import Path

import psycopg

from companion_core.access import ActorRef
from companion_core.governance import AdminService, ContextService, ReadView
from companion_core.postgres import PostgresStore
from companion_core.postgres_transfer import create, fingerprint, restore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_metrics import Metrics  # noqa: E402
from evaluate_algorithm_routing import exploration  # noqa: E402
from evaluate_recall import DISTRACTORS, evaluate  # noqa: E402


def seed(store, documents, total, begin=0):
    rows, policies = [], []
    for i in range(begin, total):
        doc = documents[i] if i < len(documents) else {"id": f"noise-{i:06}", "content": DISTRACTORS[i % len(DISTRACTORS)], "tags": ["日常"]}
        value = {"schema_version": 1, **doc, "kind": "experience", "confidence": "observed", "locked": False,
                 "source": {"type": "conversation", "reference": "synthetic-benchmark",
                            "recorded_at": "2025-01-01T00:00:00Z" if doc["id"].startswith("m") else "2026-10-20T00:00:00Z"}}
        raw = json.dumps(value, ensure_ascii=False).encode()
        version = hashlib.sha256(raw).hexdigest()
        rows.append(("memories", doc["id"], version, 1, store.cipher.encrypt(raw, "memories", doc["id"], version), False, "conversation", "synthetic-benchmark"))
        # Half of unrelated noise is another person's private corpus. It must
        # never be decrypted/ranked for this authenticated requester.
        public = i < len(documents) or i % 2 == 0
        policies.append(("memories/" + doc["id"], secrets.token_hex(16), "public" if public else "personal",
                         "[]" if public else '["actor-hidden-owner"]', "", "private", "[]", 1, None, "[]"))
    with store.db() as db:
        with db.connection.cursor().copy("COPY content_records(kind,id,version,revision,body,locked,source_type,source_ref) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
        with db.connection.cursor().copy("COPY governance_policies FROM STDIN") as cp:
            for row in policies:
                cp.write_row(row)


def main(args, metrics):
    options = json.loads(args.connection.read_text(encoding="utf-8"))
    if options.get("dbname") != "companion_test" or options.get("host") != "127.0.0.1":
        raise ValueError("Only the dedicated synthetic test database is allowed")
    schema = "scale_" + uuid.uuid4().hex
    with psycopg.connect(**options, autocommit=True) as db:
        db.execute('CREATE SCHEMA "' + schema + '"')
    store = None
    try:
        store = PostgresStore(args.output.parent / (schema + "-data"), {**options, "options": "-c search_path=" + schema}, secrets.token_bytes(32))
        store.migrate()
        store.initialize()
        dataset = json.loads((ROOT / "tests_core/fixtures/retrieval/cases.json").read_text(encoding="utf-8"))
        expansions = json.loads((ROOT / "tests_core/fixtures/retrieval/queries.json").read_text(encoding="utf-8"))
        long = json.loads((ROOT / "tests/fixtures/recall_cases_v060.json").read_text(encoding="utf-8"))
        long_expansions = json.loads((ROOT / "tests_core/fixtures/retrieval/heldout-expansions.json").read_text(encoding="utf-8"))
        if args.expansions:
            expansions = long_expansions = json.loads(args.expansions.read_text(encoding="utf-8"))
        documents = {doc["id"]: doc for doc in [*dataset["documents"], *([] if args.existing_corpus else long["documents"]) ]}
        admin = AdminService(store)
        actor = ActorRef("fixture", "owner")
        request = admin.authority.issue(actor=actor, conversation="fixture", audience="private", recipients=(actor,), lifetime=3600)
        report = {"synthetic": True, "model_calls": 0, "platform_calls": 0, "scales": [],
                  "corpus": "existing64" if args.existing_corpus else "mixed64_and_long12"}
        begin = 0
        for count in args.records:
            started = time.perf_counter()
            seed(store, list(documents.values()), count, begin)
            seeded = time.perf_counter() - started
            begin = count
            started = time.perf_counter()
            allowed = admin.repository.readable_resources(admin.access, request)
            authorization_seconds = time.perf_counter() - started
            view = ReadView(store, allowed)
            measured = {}
            with metrics.stage(measured, "cold_queries"):
                cold = evaluate(view, dataset, expansions)
            with metrics.stage(measured, "warm_queries"):
                warm = evaluate(view, dataset, expansions)
            with metrics.stage(measured, "long_queries"):
                extra = evaluate(view, long, long_expansions) if not args.existing_corpus else None
            started = time.perf_counter()
            context, guard = ContextService(store, admin.access).memory(request, "记忆召回")
            guard()
            context_seconds = time.perf_counter() - started
            assert not any(row["data"]["id"].startswith("noise-") and int(row["data"]["id"].split("-")[1]) % 2
                           for row in context["records"])
            writes = []
            for _ in range(6):
                record = store.read("companion")
                started = time.perf_counter()
                store.write("companion", record["data"], record["version"], "synthetic-cas")
                writes.append(time.perf_counter() - started)
            started = time.perf_counter()
            with metrics.stage(measured, "backup"):
                backup = create(store, store._backup_key)
            backup_seconds = time.perf_counter() - started
            started = time.perf_counter()
            with metrics.stage(measured, "restore"):
                restore(store, backup, store._backup_key, expected=fingerprint(store))
            restore_seconds = time.perf_counter() - started
            request = admin.authority.issue(actor=actor, conversation="fixture", audience="private", recipients=(actor,), lifetime=3600)
            item = {"records": count, "seed_seconds": seeded, "readable_count": len(allowed), "acl_seconds": authorization_seconds,
                    "cold": cold, "warm": warm, "long_context": extra, "context_seconds": context_seconds,
                    "write_median_seconds": statistics.median(writes), "backup_seconds": backup_seconds,
                    "backup_bytes": len(backup), "restore_seconds": restore_seconds, "measured": measured}
            report["scales"].append(item)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: value for key, value in item.items() if key not in {"cold", "warm", "long_context"}}, ensure_ascii=False), flush=True)
        report["exploration"] = exploration()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if store:
            store.database.close()
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('DROP SCHEMA "' + schema + '" CASCADE')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, nargs="+", default=[10000, 50000])
    parser.add_argument("--existing-corpus", action="store_true")
    parser.add_argument("--expansions", type=Path)
    with Metrics() as metrics:
        main(parser.parse_args(), metrics)
