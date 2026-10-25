"""Bounded synthetic full-reply evaluation: actual retrieval versus authorized oracle evidence.

No platform connector is constructed. Real model runs require --live; expected
answers are used only after generation. Structural tests inject a deterministic
model and must never be reported as measured model quality.
"""
import argparse
import asyncio
import hashlib
import json
import secrets
import statistics
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from companion_core import conversations, events
from companion_core.contracts import ActionResult, ActorRef, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.governance import GovernanceRepository, GovernedContext, ReadView
from companion_core.reply_trace import detail, listing
from companion_core.runtime import Companion
from companion_core.schema import obj
from companion_core.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class Host:
    def identity(self):
        return "synthetic-answer-evaluation"

    def require_active(self, thread_id=None):
        return None


class CaptureAdapter:
    platform, account = "evaluation", "synthetic-bot"

    def __init__(self):
        self.answers = []

    def capabilities(self):
        return [Capability("message.reply", "回答本轮问题。text 为答案；evidence_ids 引用本轮提供的原文条目或个人事实的 id；"
                           "无依据时明确表示未知，并置 abstain=true。只捕获到本地评估，不连接平台。",
                           obj({"text": {"type": "string", "maxLength": 4000},
                                "evidence_ids": {"type": "array", "maxItems": 20, "uniqueItems": True,
                                                 "items": {"type": "string", "maxLength": 200}},
                                "abstain": {"type": "boolean"}}))]

    async def execute(self, capability, arguments, *, guard):
        guard()
        self.answers.append({key: arguments[key] for key in ("text", "evidence_ids", "abstain")})
        return ActionResult("succeeded", {"id": "synthetic-capture", "external_action": False})


class ObservedModel:
    def __init__(self, model, allowance, *, call_timeout=60):
        self.model, self.allowance, self.call_timeout = model, allowance, call_timeout
        self.calls, self.inputs = [], []

    async def generate(self, instructions, context, schema, images=(), **options):
        if self.allowance[0] <= 0:
            raise AssistantError("evaluation_limit", "评估已达到模型调用上限")
        self.allowance[0] -= 1
        start = time.perf_counter()
        self.inputs.append(context)
        try:
            return await asyncio.wait_for(self.model.generate(instructions, context, schema, images, **options), self.call_timeout)
        finally:
            self.calls.append({"phase": "answer" if "event" in context else "query_expansion",
                               "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                               "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
                               "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()})


class OracleContext(GovernedContext):
    """Evaluation-only candidate replacement; source leases and the budget are unchanged."""
    def __init__(self, store, event, evidence):
        super().__init__(store, event)
        self.evidence = {stable_key("event", event.platform, event.account, native) for native in evidence}

    def memory(self, *args, **kwargs):
        result = super().memory(*args, **kwargs)
        return {**result, "records": [], "catalog": [], "context_chars": 0, "catalog_chars": 2}

    def conversation(self, account, tid, **kwargs):
        base = super().conversation(account, tid, **kwargs)
        readable = self.repository.readable_resources(self.access, self.request)
        records = conversations.records(ReadView(self.store, readable), account, tid)
        chosen = [row for row in records if row["data"]["message_id"] in self.evidence
                  and row["data"]["speaker"] == "user"]
        current = [row for row in records if row["data"]["message_id"] == self.event.id]
        for row in chosen + current:
            self.resources.add("conversation_turns/" + row["data"]["id"])
            self.versions["conversation_turns", row["data"]["id"]] = row["version"]
        return {**base, "turns": current, "recalled": chosen, "summary": None,
                "truncated": len(chosen) + len(current) < len(records)}


class EvaluationCompanion(Companion):
    oracle = None

    def governed_context(self, event):
        if self.oracle is not None and event.native_id == "question":
            return OracleContext(self.store, event, self.oracle)
        return super().governed_context(event)


def material_evidence(store, material, native_ids):
    """Map only supplied evidence IDs to benchmark message IDs, after generation."""
    mapping = {}
    conversation = material.get("conversation", {})
    for row in [*conversation.get("turns", []), *conversation.get("recalled", [])]:
        try:
            native = events.get(store, row["data"]["message_id"])["event"]["native_id"]
        except AssistantError:
            continue
        if native in native_ids:
            mapping[row["data"]["id"]] = {native}
    for fact in material.get("personal_facts", {}).get("items", []):
        record = store.read("memories", fact["id"])
        source = record["data"]["source"]["reference"]
        event = events.get(store, source)["event"]
        if event["native_id"] in native_ids:
            mapping[fact["id"]] = {event["native_id"]}
    for row in material.get("memory", {}).get("records", []):
        if row["kind"] == "memories":
            try:
                event = events.get(store, row["data"]["source"]["reference"])["event"]
            except AssistantError:
                continue
            if event["native_id"] in native_ids:
                mapping[row["data"]["id"]] = {event["native_id"]}
    for row in material.get("memory", {}).get("catalog", []):
        if row["kind"] == "memories":
            record = store.read("memories", row["id"])
            try:
                event = events.get(store, record["data"]["source"]["reference"])["event"]
            except AssistantError:
                continue
            if event["native_id"] in native_ids:
                mapping[row["id"]] = {event["native_id"]}
    return mapping


def score(case, answer, mapping):
    if answer is None:
        return {"answer_pass": False, "citation_pass": False, "abstention_pass": False, "missing_answer": True}
    expected = case["expected"]
    text, citations = answer["text"], answer["evidence_ids"]
    cited = set().union(*(mapping.get(identifier, set()) for identifier in citations))
    valid = all(identifier in mapping for identifier in citations)
    required = set(case["evidence_ids"])
    grounded = valid and required <= cited
    unknown = expected.get("abstain", False)
    abstention = answer["abstain"] == unknown
    content = all(any(term.casefold() in text.casefold() for term in options) for options in expected.get("required", []))
    content = content and not any(term.casefold() in text.casefold() for term in expected.get("forbidden", []))
    if unknown:
        grounded = valid and required <= cited and (bool(required) or not citations)
        # Structured abstention alone cannot excuse a contradictory/hallucinated text.
        content = content and any(term in text for term in expected.get("unknown_phrases", ["不知道", "未提供", "不确定", "未知"]))
    return {"answer_pass": bool(content and grounded and abstention), "content_pass": content,
            "citation_pass": grounded, "abstention_pass": abstention, "missing_answer": False}


async def run_case(store, model, case, branch, *, allowance=None, call_timeout=60):
    GovernanceRepository(store).initialize()
    store.set_state("governance-enabled", True)
    settings = store.read("companion")
    store.write("companion", {**settings["data"], "conversation_context_turns": 4,
                "reflection_min_memories": 3, **case.get("settings", {})}, settings["version"], "synthetic-evaluation")
    adapter = CaptureAdapter()
    observed = ObservedModel(model, allowance if allowance is not None else [32], call_timeout=call_timeout)
    start_time = datetime.fromisoformat(case.get("start_at", "2026-10-01T00:00:00+00:00"))
    clock = [start_time]
    core = EvaluationCompanion(store, observed, Host(), [adapter], clock=lambda: clock[0])
    core.oracle = case.get("oracle_ids", case["evidence_ids"]) if branch == "oracle" else None
    core.start()
    actor = ActorRef(adapter.platform, "synthetic-user")
    report = {"id": case["id"], "category": case["category"], "split": case.get("split", "regression"),
              "branch": branch, "answer": None, "expected_not_sent_to_model": True}
    started = time.perf_counter()
    try:
        for index, message in enumerate(case["history"]):
            clock[0] = datetime.fromisoformat(message["at"]) if message.get("at") else start_time + timedelta(hours=index)
            sender = actor if message.get("actor", "self") == "self" else ActorRef(adapter.platform, "other-user")
            event = Event(adapter.platform, adapter.account, message["id"], sender, message.get("conversation", "synthetic-dm"),
                          clock[0].isoformat(), message["text"])
            await core.handle(event)
        # Noise is ingested as ordinary source-bound history without paying for irrelevant replies.
        for index in range(case.get("noise_turns", 12)):
            clock[0] += timedelta(minutes=1)
            event = Event(adapter.platform, adapter.account, f"noise-{index}", actor, "synthetic-dm",
                          clock[0].isoformat(), "整理桌面，看看窗外的云，没有新的约定。")
            events.ingest(store, event)
            governed = GovernedContext(store, event)
            governed.classify_event()
            scope = core.conversation_scope(event)
            conversations.append(store, scope["account"], scope["channel"], scope["key"], scope["message_id"],
                event.text, "user", scope["sender_uid"], "received", clock[0].isoformat(), clock[0].isoformat(),
                on_write=lambda key, g=governed, e=event: g.classify_written("conversation_turns", key, sources=["event/" + e.id]))
        clock[0] = datetime.fromisoformat(case["question_at"]) if case.get("question_at") else clock[0] + timedelta(minutes=1)
        question = Event(adapter.platform, adapter.account, "question", actor, "synthetic-dm", clock[0].isoformat(), case["question"])
        before = len(adapter.answers)
        await core.handle(question)
        answer = adapter.answers[-1] if len(adapter.answers) > before else None
        material = next(context for context in reversed(observed.inputs) if context.get("event", {}).get("native_id") == "question")
        mapping = material_evidence(store, material, {message["id"] for message in case["history"]})
        available = set().union(*mapping.values()) if mapping else set()
        report.update(answer=answer, evidence_complete=set(case["evidence_ids"]) <= available,
                      supplied_message_ids=sorted(available), **score(case, answer, mapping))
        report["fact_states"] = [{k: row[k] for k in ("attribute", "value", "status")}
                                 for row in material.get("personal_facts", {}).get("items", [])]
        report["fact_state_pass"] = all(any(all(row.get(k) == v for k, v in wanted.items())
                                           for row in report["fact_states"]) for wanted in case.get("expected_facts", []))
        report["scenario_pass"] = report["answer_pass"] and report["fact_state_pass"]
        governed = GovernedContext(store, question)
        traces = [row for row in listing(store, 100) if row["event_id"] == question.id]
        report["stages"] = detail(store, traces[0]["id"], governed.access, governed.request)["steps"]
    except Exception as exc:
        report.update(error=getattr(exc, "code", type(exc).__name__), answer_pass=False, scenario_pass=False)
    finally:
        core.stop()
        report.update(duration_ms=round((time.perf_counter() - started) * 1000, 3), model_calls=len(observed.calls),
                      calls=observed.calls, role_paused_after=store.state(core.run_key)["status"] == "paused",
                      real_platform_actions=0)
        with store.db() as db:
            usage = db.execute("SELECT SUM(input_tokens),SUM(output_tokens) FROM core_observations WHERE kind='model'").fetchone()
        report["tokens"] = {"input": int(usage[0]) if usage[0] is not None else None,
                            "output": int(usage[1]) if usage[1] is not None else None}
    return report


def summarize(rows, *, real_model):
    branches = {}
    for branch in ("actual", "oracle"):
        chosen = [row for row in rows if row["branch"] == branch]
        branches[branch] = {"cases": len(chosen), "passes": sum(row.get("answer_pass") is True for row in chosen),
            "scenario_passes": sum(row.get("scenario_pass") is True for row in chosen),
            "errors": sum("error" in row for row in chosen),
            "p50_ms": statistics.median(row["duration_ms"] for row in chosen) if chosen else None}
    pairs = []
    for identifier in dict.fromkeys(row["id"] for row in rows):
        pair = {row["branch"]: row for row in rows if row["id"] == identifier}
        actual, oracle = pair.get("actual", {}), pair.get("oracle", {})
        reason = ("incomplete_or_failed_run" if "error" in actual or "error" in oracle or len(pair) != 2 else
                  "passed" if actual.get("answer_pass") and oracle.get("answer_pass") else
                  "retrieval_or_selection" if oracle.get("answer_pass") and not actual.get("evidence_complete") else
                  "context_interference" if oracle.get("answer_pass") else "reader_or_fact_update_or_rubric")
        pairs.append({"id": identifier, "diagnosis": reason})
    return {"synthetic_only": True, "real_model": real_model, "blind_evaluation": False,
            "score_type": "deterministic_phrase_citation_and_fact_state_checks",
            "limits": "Not an independent semantic judge. Scripted model runs validate plumbing only; no claim of model accuracy.",
            "branches": branches, "comparisons": pairs, "model_calls": sum(row["model_calls"] for row in rows), "cases": rows}


@contextmanager
def fresh_store(database=None):
    evaluation_root = ROOT / ".dev" / "answer-evaluation"
    evaluation_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-", dir=evaluation_root) as folder:
        assert Path(folder).resolve().is_relative_to(evaluation_root.resolve())
        if database is None:
            store = Store(Path(folder))
            store.initialize()
            yield store
            return
        import psycopg

        from companion_core.postgres import PostgresStore

        options = json.loads(Path(database).read_text(encoding="utf-8"))
        if options.get("dbname") != "companion_test" or options.get("host") != "127.0.0.1":
            raise ValueError("Only the dedicated local companion_test database is accepted")
        schema = "answer_eval_" + uuid.uuid4().hex
        with psycopg.connect(**options, autocommit=True) as db:
            db.execute('CREATE SCHEMA "' + schema + '"')
        store = None
        try:
            store = PostgresStore(Path(folder), {**options, "options": "-c search_path=" + schema}, secrets.token_bytes(32))
            store.migrate()
            store.initialize()
            yield store
        finally:
            if store:
                store.database.close()
            with psycopg.connect(**options, autocommit=True) as db:
                db.execute('DROP SCHEMA "' + schema + '" CASCADE')


async def main_async(args):
    from companion_codex.model import CodexModel

    raw = args.cases.read_bytes()
    cases = json.loads(raw)["cases"]
    if args.case_id:
        if set(args.case_id) - {case["id"] for case in cases}:
            raise ValueError("Unknown case ID")
        cases = [case for case in cases if case["id"] in args.case_id]
    rows, allowance = [], [args.max_calls]
    deadline = time.monotonic() + args.deadline
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save_report():
        report = {**summarize(rows, real_model=True), "cases_sha256": hashlib.sha256(raw).hexdigest(),
                  "requested_pairs": len(cases), "complete": len(rows) == 2 * len(cases),
                  "call_limit": args.max_calls, "deadline_seconds": args.deadline}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save_report()
    for case in cases:
        for branch in ("actual", "oracle"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or allowance[0] <= 0:
                break
            with fresh_store(args.database) as store:
                before = allowance[0]
                try:
                    row = await asyncio.wait_for(run_case(store, CodexModel(), case, branch, allowance=allowance,
                                                          call_timeout=min(60, remaining)), remaining)
                except asyncio.TimeoutError:
                    row = {"id": case["id"], "branch": branch, "error": "evaluation_deadline", "answer_pass": False,
                           "scenario_pass": False, "duration_ms": round(remaining * 1000), "model_calls": before - allowance[0]}
                rows.append(row)
            save_report()
            print(json.dumps({"id": case["id"], "branch": branch, "passed": row.get("answer_pass"), "error": row.get("error")}), flush=True)
    save_report()
    return len(rows) == len(cases) * 2 and all(row.get("scenario_pass") for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests_core/fixtures/answer_pipeline.json")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=32)
    parser.add_argument("--deadline", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 200 or not 10 <= args.deadline <= 1800:
        parser.error("Use bounded calls (1..200) and wall time (10..1800 seconds)")
    raise SystemExit(0 if asyncio.run(main_async(args)) else 1)


if __name__ == "__main__":
    main()
