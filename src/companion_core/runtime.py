"""One explicit role run, multiple connectors, durable scoped decisions and actions."""
import json
import uuid
from contextlib import AsyncExitStack, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from jsonschema import validate

from . import budget, conversations, delivery, events
from .contracts import ActionResult, Event, PlatformRegistry, stable_key
from .decision import DecisionService
from .errors import AssistantError
from .identity import IdentityService
from .relationships import RelationshipUpdates
from .storage import atomic_write, utcnow


class Companion(RelationshipUpdates):
    def __init__(self, store, model, host, adapters=(), clock=None, *, run_key="core-run"):
        self.run_key = run_key
        self.store, self.host = store, host
        self.decisions = DecisionService(model)
        self.platforms = PlatformRegistry(adapters)
        self.identities = IdentityService(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        for adapter in self.platforms.all():
            if hasattr(adapter, "bind_runtime"):
                adapter.bind_runtime(self)

    def identity(self):
        return self.host.identity()

    def mode(self, required=None):
        self.guard()
        if required not in (None, "role"):
            raise AssistantError("mode_denied", "核心角色运行不能执行管理操作")
        return "role"

    def role_run(self):
        return {**self.guard(), "account": self.store.read("manifest")["data"]["account_uid"]}

    def run(self, generation=None, check_host=True):
        return {**self.guard(generation), "account": self.store.read("manifest")["data"]["account_uid"]}

    def start(self, *, read_only=False, reply_test=None):
        self.host.require_active()
        if not self.store.validate_critical()["ok"]:
            raise AssistantError("critical_state_invalid", "核心配置无效")
        accounts = sorted([a.platform, a.account] for a in self.platforms.all())
        from .reply_test import validate

        reply_test = validate(reply_test, accounts, read_only=read_only)
        with self.store.lock:
            for adapter in self.platforms.all():
                if hasattr(adapter, "validate_start"):
                    adapter.validate_start()
            old = self.store.state(self.run_key)
            if old and old["status"] == "active":
                if (old["owner"] == self.host.identity() and old["accounts"] == accounts
                        and old["read_only"] == read_only and old.get("reply_test") == reply_test
                        and not (self.store.runtime / ("core-stop-" + old["generation"])).exists()):
                    return old
                self.stop("replaced", generation=old["generation"])
            since = (old.get("pending_since", old["started_at"]) if old and old["owner"] == self.host.identity()
                     and old["accounts"] == accounts else self.clock().isoformat())
            run = {"owner": self.host.identity(), "generation": uuid.uuid4().hex, "status": "active",
                   "accounts": accounts, "read_only": read_only, "started_at": self.clock().isoformat(), "pending_since": since}
            if reply_test is not None:
                run["reply_test"] = reply_test
                run["pending_since"] = run["started_at"]
                run["deadline_at"] = (self.clock() + timedelta(minutes=10)).isoformat()
            self.store.set_state(self.run_key, run)
            return run

    def guard(self, generation=None):
        run = self.store.state(self.run_key, {})
        if run.get("status") != "active":
            raise AssistantError("not_running", "核心保持暂停，需显式启动")
        if generation and run["generation"] != generation:
            raise AssistantError("stale_generation", "旧运行已经失效")
        if run.get("deadline_at") and self.clock() >= datetime.fromisoformat(run["deadline_at"]):
            self.stop("duration_complete", generation=run["generation"])
            raise AssistantError("duration_complete", "本次运行已到指定时限，保持暂停")
        if (self.store.runtime / ("core-stop-" + run["generation"])).exists():
            raise AssistantError("run_suspended", "当前核心运行已停止")
        try:
            self.host.require_active(run["owner"])
        except AssistantError as exc:
            self.stop(exc.code, generation=run["generation"])
            raise
        if self.host.identity() != run["owner"]:
            raise AssistantError("not_owner", "仅归属角色聊天可以驱动核心")
        # The host check may yield to a new owner/run in another process.
        with self.store.lock:
            current = self.store.state(self.run_key, {})
            if current.get("generation") != run["generation"]:
                raise AssistantError("stale_generation", "旧运行已经失效")
            if current.get("status") != "active" or (self.store.runtime / ("core-stop-" + run["generation"])).exists():
                raise AssistantError("run_suspended", "当前核心运行已停止")
            for adapter in self.platforms.all():
                if hasattr(adapter, "validate_run"):
                    adapter.validate_run(current)
            return current

    def stop(self, reason="user_stop", *, generation=None):
        run = self.store.state(self.run_key, {})
        if not run or (generation and run.get("generation") != generation):
            return {"stopped": False, **run} if run else {"status": "paused", "stopped": False}
        atomic_write(self.store.runtime / ("core-stop-" + run["generation"]), reason.encode())
        with self.store.lock:
            with self.store.db() as db:
                current = self.store.state(self.run_key, {}, db)
                if current.get("generation") != run["generation"]:
                    return {"stopped": False, **current}
                current.update(status="paused", reason=reason)
                self.store.set_state(self.run_key, current, db)
                db.execute("UPDATE core_actions SET status='cancelled' WHERE generation=? AND status='prepared'", (run["generation"],))
                db.execute("UPDATE core_actions SET status='uncertain' WHERE generation=? AND status='dispatching'", (run["generation"],))
            for adapter in self.platforms.all():
                if hasattr(adapter, "runtime_stopped"):
                    adapter.runtime_stopped(run["generation"])
        return current

    async def model(self, instructions, context, schema, images=(), *, native=None, guard=None, phase=None,
                    interactive=False):
        checkpoint = getattr(self, "interactive_checkpoint", None)
        if checkpoint and not interactive:
            await checkpoint()
        run = self.guard()
        policy = self.store.read("policy")["data"]
        if guard:
            guard()
        budget.reserve(self.store, policy, self.clock())

        def alive():
            self.guard(run["generation"])
            if guard:
                guard()

        result = await self.decisions.generate(instructions, context, schema, images,
                                               store=self.store, policy=policy, native=native,
                                               guard=alive, phase=phase or (lambda value: None))
        alive()
        return result

    def remaining_model_calls(self):
        self.guard()
        return budget.remaining(self.store, self.store.read("policy")["data"], self.clock())

    async def cycle(self):
        from .orchestration import Stage, Workflow, run_cycle

        run = self.guard()
        workflows = []
        for adapter in self.platforms.all():
            if [adapter.platform, adapter.account] not in run["accounts"]:
                continue
            if hasattr(adapter, "workflow"):
                workflows.append(adapter.workflow(self))
                continue

            async def collect(state, adapter=adapter):
                await self._poll_adapter(adapter, run)
                state["events"] = events.pending(
                    self.store, [[adapter.platform, adapter.account]], run["pending_since"],
                    owner=run["owner"], now=self.clock().isoformat(), config_token=delivery.config_token(self.store))

            async def respond(state):
                limit = self.store.read("policy")["data"].get("items_per_cycle", 3)
                for event in state["events"][:limit]:
                    if not self.remaining_model_calls():
                        break
                    await self.handle(event)

            workflows.append(Workflow(adapter.platform, adapter.account, (
                Stage("inbox", collect, 10, "prepare"), Stage("conversation", respond, 20, requires_model=True))))
        return await run_cycle(self.store, workflows, guard=lambda: self.guard(run["generation"]),
                               remaining_model_calls=self.remaining_model_calls,
                               checkpoint=getattr(self, "interactive_checkpoint", None))

    async def _poll_adapter(self, adapter, run):
        key = stable_key("cursor", adapter.platform, adapter.account)
        cursor = self.store.state(key, {"since": run["started_at"]})
        incoming, next_cursor = await adapter.receive(cursor)
        self.guard(run["generation"])
        for event in incoming:
            if (event.platform, event.account) != (adapter.platform, adapter.account):
                raise AssistantError("connector_scope", "接入模块返回了其他账号事件")
            events.ingest(self.store, event)
        self.store.set_state(key, next_cursor)

    async def poll(self):
        run = self.guard()
        for adapter in self.platforms.all():
            if [adapter.platform, adapter.account] not in run["accounts"]:
                continue
            # Commit every event before advancing the cursor; delivery is replay safe.
            await self._poll_adapter(adapter, run)
        # Durable inbox also supplies events collected before a crash/consumer interruption.
        from .reply_test import allows

        return [event for event in events.pending(self.store, run["accounts"], run["pending_since"], limit=100,
                              owner=run["owner"], now=self.clock().isoformat(),
                              config_token=delivery.config_token(self.store)) if allows(run, event)]

    async def _identity_review(self, event):
        from .governance import enabled

        if enabled(self.store):
            # Cross-account correspondence is an internal maintenance operation;
            # never attach its other private conversations to a reply worker.
            return
        return await self.identities.review(event.actor, self.model, self.guard)

    def memory_context(self, event, *, dependencies=None):
        with self.store.db() as db:
            visible_events = dict(db.execute("SELECT id,version FROM core_events WHERE deleted=0 AND conversation=?",
                                             (event.thread,)).fetchall())
        record_sources = {}

        def visible(item):
            source = item["data"].get("source", {})
            reference = source.get("reference", "")
            if reference.startswith("event-"):
                record_sources[item["kind"], item["data"]["id"]] = reference
                return reference in visible_events
            return True

        adapter = self.platforms.get(event.platform, event.account)
        relationship_id = adapter.relationship_id(event.actor) if hasattr(adapter, "relationship_id") else event.actor.key
        context = self.store.context(event.text, uids=[relationship_id], record_filter=visible)
        if dependencies is not None:
            used = [(r["kind"], r["data"]["id"]) for r in context["records"]]
            used.extend((r["kind"], r["id"]) for r in context["catalog"])
            for key in used:
                source = record_sources.get(key)
                if source in visible_events:
                    dependencies[source] = visible_events[source]
        return context

    def conversation_scope(self, event):
        adapter = self.platforms.get(event.platform, event.account)
        values = {"account": event.account, "channel": event.platform, "key": event.thread,
                  "message_id": event.id, "sender_uid": event.actor.key, "reply_to": ""}
        if hasattr(adapter, "conversation_scope"):
            values.update(adapter.conversation_scope(event))
        return values

    def _record_reply(self, event, proposal, result, occurred_at, disclosure=None):
        if result.get("status") != "succeeded":
            return
        adapter = self.platforms.get(event.platform, event.account)
        if hasattr(adapter, "reply_content"):
            content = adapter.reply_content(event, proposal["capability"], proposal["arguments"], result)
        else:
            content = proposal["arguments"].get("text") if proposal["capability"] == "message.reply" else None
        if content is None:
            return
        scope = self.conversation_scope(event)
        from .governance import record_provenance

        conversations.append(self.store, scope["account"], scope["channel"], scope["key"],
                             "action:" + result["id"], content, "assistant", event.account, "sent", utcnow(),
                             occurred_at, reply_to=scope["message_id"],
                             on_write=(lambda item_id: record_provenance(
                                 self.store, "conversation_turns", item_id, disclosure)) if disclosure else None)

    async def handle(self, event):
        run = self.guard()
        try:
            from .reply_trace import point, reply

            with reply(self.store, event, run):
                result = await self._handle(event, run)
                point("outcome", status=(result.get("action") or {}).get("status",
                      "deferred" if result.get("deferred") else "duplicate" if result.get("duplicate") else "no_action"))
                return result
        except AssistantError as exc:
            if exc.code not in delivery.TEMPORARY:
                raise
            # Never apply an old failure's deferral to a corrected source version.
            # Paused runs remain paused; active runs may reconsider when due.
            version = sha256(json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            delivery.defer(self, event, version, run, exc.code)
            raise

    async def governed_memory(self, governed, event, relationship_id, run, configurations, event_version):
        """Reuse scoped Codex query plans before ranking the authorized corpus."""
        from .recall_routing import resolve

        lease = governed.access.lease(governed.request, ["event/" + event.id])

        def guard():
            self.guard(run["generation"])
            adapter = self.platforms.get(event.platform, event.account)
            if hasattr(adapter, "validate_event"):
                adapter.validate_event(event)
            governed.access.revalidate(lease)
            if (events.get(self.store, event.id)["version"] != event_version
                    or any(self.store.version(kind) != value["version"] for kind, value in configurations.items())):
                raise AssistantError("context_changed", "查询扩写期间来源或配置已变化")

        async def generate(instructions, material, schema):
            adapter = self.platforms.get(event.platform, event.account)
            return await self.model(instructions, material, schema, guard=guard,
                                    interactive=bool(getattr(adapter, "interactive", False)))

        readable = governed.repository.readable_resources(governed.access, governed.request)
        count = sum(resource.split("/", 1)[0] in self.store.profile.memory_collections for resource in readable)
        settings = configurations["companion"]["data"]
        eligible = count >= settings.get("reflection_min_memories", 6)
        if not eligible and settings.get("conversation_recall_enabled", True):
            scope = self.conversation_scope(event)
            tid = conversations.thread_id(scope["account"], scope["channel"], scope["key"])
            if getattr(self.store, "backend", None) == "postgres":
                from .conversation_index import candidates

                history_count = sum("conversation_turns/" + key in readable
                                    for key in candidates(self.store, "conversation_turns", scope["account"], tid))
            else:
                from .governance import ReadView

                history_count = len(conversations.records(ReadView(self.store, readable), scope["account"], tid))
            eligible = history_count > settings.get("conversation_context_turns", 24)
        plan = await resolve(self.store, event.text,
            scope="governed:" + governed.request.actor + ":" + event.thread + ":" + event.visibility,
            uids=[relationship_id], eligible=eligible,
            remaining=self.remaining_model_calls(), generate=generate, guard=guard, clock=self.clock)
        references = [ref for ref in plan["references"] if ref["kind"] + "/" + ref["id"] in readable]
        memory = governed.memory(event.text, uids=[relationship_id], queries=plan["queries"], references=references)
        return memory, plan["queries"]

    def governed_context(self, event):
        from .governance import GovernedContext

        return GovernedContext(self.store, event)

    async def _handle(self, event, run):
        from .governance import enabled, resource_id
        from .reply_test import require_event

        require_event(run, event)

        if [event.platform, event.account] not in run["accounts"]:
            raise AssistantError("connector_scope", "该账号不属于当前运行")
        adapter = self.platforms.get(event.platform, event.account)
        if hasattr(adapter, "validate_event"):
            adapter.validate_event(event)
        with self.store.lock:
            with self.store.db() as db:
                current = db.execute("SELECT version,deleted FROM core_events WHERE id=?", (event.id,)).fetchone()
            if current:
                if current["deleted"]:
                    return {"ignored": True, "reason": "event_deleted"}
                version = sha256(json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                if current["version"] != version:
                    raise AssistantError("context_changed", "待处理事件已被更正，请重新读取最新收件")
                receipt = {"id": event.id, "version": current["version"]}
            else:
                # Direct first delivery is supported; later corrections enter through poll/ingest.
                receipt = events.ingest(self.store, event)
        if receipt.get("deleted"):
            return {"ignored": True, "reason": "event_deleted"}
        governed = self.governed_context(event) if enabled(self.store) else None
        if governed:
            governed.classify_event()
        scope = self.conversation_scope(event)
        conversations.append(self.store, scope["account"], scope["channel"], scope["key"], scope["message_id"],
                             event.text, "user", scope["sender_uid"], "received", utcnow(), event.occurred_at,
                             reply_to=scope["reply_to"], attachments=event.attachments,
                             on_write=(lambda item_id: governed.classify_written(
                                 "conversation_turns", item_id, sources=["event/" + event.id])) if governed else None)
        if hasattr(adapter, "recover_conversation"):
            adapter.recover_conversation(event)
        done = "core-processed:" + event.id
        if self.store.state(done) == receipt["version"]:
            return {"duplicate": True}
        if not delivery.eligible(self, event, receipt["version"], run):
            return {"deferred": True, "delivery": self.store.state(delivery.key(event))}
        if getattr(adapter, "reply_quota_preflight", False) and not run["read_only"]:
            from .action_budget import require

            require(self, adapter)
        await self._identity_review(event)
        persona = self.store.read("persona")
        policy = self.store.read("policy")
        companion = self.store.read("companion")
        configurations = {"persona": persona, "policy": policy, "companion": companion,
                          "manifest": self.store.read("manifest"), "preferences": self.store.read("preferences")}
        relationship_id = adapter.relationship_id(event.actor) if hasattr(adapter, "relationship_id") else event.actor.key
        relationship = (self.store.read("relationships", relationship_id)
                        if self.store.exists("relationships", relationship_id) else None)
        relationship_version = relationship["version"] if relationship else None
        memory_sources = {}
        if governed:
            memory, recall_queries = await self.governed_memory(
                governed, event, relationship_id, run, configurations, receipt["version"])
        else:
            memory, recall_queries = self.memory_context(event, dependencies=memory_sources), ()
        identity = ({"identity": {"actor": asdict(event.actor)}, "person_profile": {}, "dependency": None}
                    if governed else self.identities.decision_context(event.actor))
        identity_dependency = identity["dependency"]
        tid = conversations.thread_id(scope["account"], scope["channel"], scope["key"])
        indexed_conversation = governed is not None and getattr(self.store, "backend", None) == "postgres"
        conversation_signature = governed.conversation_signature(scope["account"], tid) if indexed_conversation else None
        conversation = (governed.conversation(scope["account"], tid, query=event.text, queries=recall_queries) if governed
                        else conversations.context(self.store, scope["account"], tid, query=event.text, queries=recall_queries))
        if indexed_conversation and governed.conversation_signature(scope["account"], tid) != conversation_signature:
            raise AssistantError("context_changed", "读取期间对话成员已经变化")
        from .reply_trace import point

        point("context", memory_records=len(memory["records"]), catalog_records=len(memory["catalog"]),
              recent_turns=len(conversation["turns"]), recalled_turns=len(conversation.get("recalled", [])),
              summary_present=bool(conversation.get("summary")),
              conversation_chars=len(json.dumps(conversation, ensure_ascii=False)))
        capabilities = [c for c in adapter.capabilities() if not run["read_only"] or c.effect == "read"]
        context = {"event": event.as_dict(), "memory": memory,
                   "conversation": conversation,
                   "identity": identity["identity"], "person_profile": identity["person_profile"],
                   "identity_evidence": [events.get(self.store, event.id)],
                   "capabilities": [c.as_dict() for c in capabilities],
                   "runtime": {"read_only": run["read_only"]},
                   "relationship_updates": {"enabled": companion["data"]["auto_relationships"],
                                            "locked": bool(relationship and relationship["data"]["locked"])}}
        context.update(governed.persona_context() if governed else {"persona": persona["data"]})
        facts = None
        if governed and event.visibility == "private":
            from .temporal_facts import PersonalFacts

            facts = PersonalFacts(governed, event, self.clock)
            context["personal_facts"] = facts.context()
        growth = None
        if governed and event.visibility == "private":
            from .interaction_growth import InteractionGrowth

            growth = InteractionGrowth(self, governed, event)
            context.update(growth.context())
        if governed:
            if getattr(self.store, "backend", None) == "postgres":
                from .relations import RelationService

                graph = RelationService(governed.repository, governed.authority).query(governed.request, event.actor.key)
                context["social_relations"] = graph
                for claim in graph["claims"]:
                    governed.resources.add("relation/" + claim["id"])
                    governed.resources.update("actor/" + claim[key] for key in ("subject", "object"))
            governed.freeze()
            from .reply_trace import sources

            sources(governed.lease.versions)

        class Budgeted:
            async def generate(_, instructions, material, schema, images=(), **options):
                if facts:
                    facts.supplied(material)
                extra = {"interactive": True} if getattr(adapter, "interactive", False) else {}
                return await self.model(instructions, material, schema, images, guard=check_context, **extra)

        memory_versions = {(item["kind"], item["data"]["id"]): item["version"] for item in memory["records"]}
        # Catalog summaries are also model evidence, including partial excerpts.
        # Freeze only the sources actually supplied, not the rest of the library.
        memory_versions.update({(item["kind"], item["id"]): item["version"] for item in memory["catalog"]})

        def check_context():
            self.guard(run["generation"])
            if hasattr(adapter, "validate_event"):
                adapter.validate_event(event)
            if governed:
                governed.check()
            if growth:
                growth.check()
            if facts:
                facts.check()
            if any(self.store.read(k)["version"] != value["version"] for k, value in configurations.items()):
                raise AssistantError("context_changed", "决策期间配置已经变化")
            current_relationship_version = (self.store.read("relationships", relationship_id)["version"]
                                            if self.store.exists("relationships", relationship_id) else None)
            if current_relationship_version != relationship_version:
                raise AssistantError("context_changed", "决策期间关系档案已经变化")
            if events.get(self.store, event.id)["version"] != receipt["version"]:
                raise AssistantError("context_changed", "决策期间原始事件已经变化")
            from .governance import verify_record_versions

            verify_record_versions(self.store, memory_versions)
            if memory_sources:
                with self.store.db() as db:
                    current_sources = dict(db.execute(
                        "SELECT id,version FROM core_events WHERE deleted=0 AND conversation=?", (event.thread,)
                    ).fetchall())
                if any(current_sources.get(source) != version for source, version in memory_sources.items()):
                    raise AssistantError("context_changed", "决策期间记忆来源已经变化或移出当前会话")
            current_conversation = (governed.conversation_signature(scope["account"], tid) if indexed_conversation else
                                    governed.conversation(scope["account"], tid, query=event.text, queries=recall_queries) if governed else
                                    conversations.context(self.store, scope["account"], tid, query=event.text, queries=recall_queries))
            if current_conversation != (conversation_signature if indexed_conversation else conversation):
                raise AssistantError("context_changed", "决策期间对话或摘要已经变化")
            if not governed:
                self.identities.verify_context(identity_dependency)

        async with AsyncExitStack() as material_scope:
            images = ()
            if hasattr(adapter, "decision_material"):
                material = await material_scope.enter_async_context(adapter.decision_material(event, guard=check_context))
                context["event_material"] = material["context"]
                images = material["images"]
                check_context()
            result = await DecisionService(Budgeted()).decide(context, capabilities, images=images,
                                                            context_policy=companion["data"])
        # PostgreSQL commits derived content, ACLs and dedupe receipts together.
        # A failed permission recheck rolls back the whole local update batch.
        with self.store.lock, (self.store.db() if governed else nullcontext()):
            check_context()
            self.identities.apply_model(result["identity_proposals"], allowed_evidence={event.id}, allowed_kinds={"claim"})
            if (result.get("relationship") and companion["data"]["auto_relationships"]
                    and not (relationship and relationship["data"]["locked"])
                    and (not governed or not relationship or governed.access.can_read(
                        governed.request, resource_id("relationships", relationship_id)))):
                from .people import bind_relationship
                from .provenance import routine_update

                update = result["relationship"]
                with (routine_update(self.store, resource_id("relationships", relationship_id))
                      if governed else nullcontext()):
                    changed = self.relationship(relationship_id, "", update["delta"], event.id,
                                                update["impression"], update["facts"])
                    relationship_version = changed["version"]
                    bind_relationship(self.store, event.actor, relationship_id)
                    if governed:
                        governed.classify_written("relationships", relationship_id, personal=True)
                        governed.rebase_written("relationships", relationship_id, changed["version"])
                if ("relationships", relationship_id) in memory_versions:
                    memory_versions["relationships", relationship_id] = changed["version"]
            if facts:
                fact_result = facts.save(result.get("fact_updates", []))
                point("facts_commit", **fact_result)
            for index, content in enumerate(result["memories"]):
                item_id = stable_key("memory", event.id, index)
                if not self.store.exists("memories", item_id):
                    self.store.write("memories", {
                        "schema_version": 1, "id": item_id, "kind": "experience", "content": content,
                        "source": {"type": "interaction", "recorded_at": event.occurred_at, "reference": event.id},
                        "confidence": "observed", "tags": [], "locked": False,
                    }, None, "core-decision", item_id)
                    if governed:
                        governed.classify_written("memories", item_id, personal=True)
            if growth:
                growth.save(result.get("growth_update"), result["memories"], receipt["version"])
            # Rebase only after verifying and applying our own updates under the same lock.
            if not governed:
                identity_dependency = self.identities.decision_context(event.actor)["dependency"]
        point("memory_commit", memories=len(result["memories"]), facts=len(result.get("fact_updates", [])))
        action = await self.dispatch(event, result["action"], context_guard=check_context,
                                      retry_unsent=True, disclosure=governed.proof() if governed else None) if result["action"] else None
        with self.store.lock:
            if action and action["status"] == "cancelled" and action.get("error") in delivery.TEMPORARY:
                delivery.defer(self, event, receipt["version"], run, action["error"])
            else:
                # Persist confirmed/uncertain outcomes even if the run was paused
                # while the remote call completed; never replay an ambiguous send.
                delivery.finish(self, event, receipt["version"], run)
        return {"decision": result, "action": action}

    async def dispatch(self, event, proposal, *, context_guard=None, retry_unsent=False, disclosure=None):
        run = self.guard()
        from .reply_test import require_event

        require_event(run, event)
        if [event.platform, event.account] not in run["accounts"]:
            raise AssistantError("connector_scope", "该账号不属于当前运行")
        adapter = self.platforms.get(event.platform, event.account)
        capabilities = {c.name: c for c in adapter.capabilities()}
        capability = capabilities.get(proposal["capability"])
        if not capability:
            raise AssistantError("unknown_capability", "平台没有声明该能力")
        if run["read_only"] and capability.effect != "read":
            raise AssistantError("action_disabled", "本次运行只读")
        validate(proposal["arguments"], capability.parameters)
        if context_guard:
            context_guard()
        # Native target fields are validated by the connector; event scope is frozen by the core.
        key = stable_key("dispatch", event.platform, event.account, event.id, capability.name)
        quota_key = None
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM core_actions WHERE dedupe=?", (key,)).fetchone()
            if previous and retry_unsent and delivery.retryable(previous, self.store):
                db.execute("UPDATE core_actions SET dedupe=? WHERE id=? AND status='cancelled'",
                           (key + ":retired:" + previous["id"], previous["id"]))
                previous = None
            if not previous:
                if not getattr(adapter, "manages_action_quota", False):
                    from .action_budget import require

                    allowance = require(self, adapter, db)
                    quota_key = allowance["key"]
                    self.store.set_state(quota_key, allowance["used"] + 1, db)
                action_id = uuid.uuid4().hex
                created = self.clock().isoformat()
                body = {"event": event.as_dict(), "arguments": proposal["arguments"]}
                if disclosure:
                    body["disclosure"] = disclosure
                db.execute("INSERT INTO core_actions VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    action_id, key, run["generation"], event.platform, event.account, capability.name,
                    self.store.ledger_encode("core_actions.body", body), "dispatching", None, created))
        if previous:
            result = {**(self.store.ledger_decode("core_actions.result", previous["result"]) if previous["result"] else {}), "id": previous["id"],
                      "status": previous["status"], "duplicate": True}
            body = self.store.ledger_decode("core_actions.body", previous["body"])
            self._record_reply(Event.from_dict(body["event"]),
                               {"capability": previous["capability"], "arguments": body["arguments"]},
                               result, previous["created"], body.get("disclosure"))
            return result
        policy_version = self.store.read("policy")["version"]

        def guard():
            try:
                self.guard(run["generation"])
                if self.store.read("policy")["version"] != policy_version:
                    raise AssistantError("policy_changed", "派发前策略发生变化")
                if context_guard:
                    context_guard()
            except AssistantError:
                raise AssistantError("core_before_send_cancelled", "派发前运行或策略发生变化") from None

        try:
            guard()
            outcome = await adapter.execute(capability.name, {**proposal["arguments"], "_event": event.as_dict(),
                                                              "_retry_unsent": retry_unsent,
                                                              "_disclosure": disclosure}, guard=guard)
            if not isinstance(outcome, ActionResult):
                outcome = ActionResult("uncertain", error="invalid_connector_result")
        except AssistantError as exc:
            # Only connectors know whether a rejected operation had any side effect.
            outcome = ActionResult("cancelled" if exc.code == "core_before_send_cancelled" else "uncertain", error=exc.code)
        except Exception:
            outcome = ActionResult("uncertain", error="transport_or_unknown")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE core_actions SET status=?,result=? WHERE id=?", (
                outcome.status, self.store.ledger_encode("core_actions.result", asdict(outcome)), action_id))
            if outcome.status == "cancelled" and quota_key is not None:
                # The connector guarantees no remote side effect on cancellation.
                # Keep uncertain receipts charged, but do not spend an allowance
                # solely because the last guard observed a pause/config change.
                self.store.set_state(quota_key, max(0, self.store.state(quota_key, 0, db) - 1), db)
        result = {"id": action_id, **asdict(outcome)}
        self._record_reply(event, proposal, result, created, disclosure)
        return result
