"""Bilibili task services registered with the platform-neutral stage controller."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from companion_core.orchestration import Flow, Stage, Workflow

from . import delivery
from .cognition import (
    PLAN_PROMPT,
    PLAN_SCHEMA,
    packet,
    save_learning,
    stable_id,
    validate_decision,
    validate_plan,
    verify_decision_context,
    verify_versions,
)
from .errors import AssistantError
from .interests import interest_profile


class BilibiliWorkflow:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.engine = scheduler.engine

    def definition(self):
        return Workflow("bilibili", self.engine.run(check_host=False)["account"], (
            Stage("emotion_recovery", self.emotion_recovery, 10, "prepare"),
            Stage("delivery_recovery", self.delivery_recovery, 20, "prepare"),
            Stage("inbox", self.collect, 30, "prepare"),
            Stage("selection_context", self.prepare, 40, "prepare"),
            Stage("native_tools", self.native_tools, 50, requires_model=True),
            Stage("video_gap", self.video_gap, 60, requires_model=True),
            Stage("goal_learning", self.goal_learning, 70, requires_model=True),
            Stage("exploration", self.explore, 80, requires_model=True),
            Stage("decision_and_delivery", self.decide, 90, requires_model=True),
            Stage("reflection", self.reflect, 100, "finalize", True),
            Stage("maintenance", self.maintain, 110, "finalize", True),
        ))

    def emotion_recovery(self, state):
        from .emotions import recover

        recover(self.engine)

    def delivery_recovery(self, state):
        delivery.recover(self.engine)

    async def collect(self, state):
        engine = self.engine
        policy = engine.store.read("policy")["data"]
        state["policy"] = policy
        local = engine.clock().astimezone(ZoneInfo(policy["timezone"])).strftime("%H:%M")
        start, end = policy["active_hours"]["start"], policy["active_hours"]["end"]
        inside = start <= local < end if start < end else local >= start or local < end
        if not inside:
            self.scheduler.phase("waiting", wait_reason="outside_hours")
            return Flow.STOP
        # The daemon collector runs independently of model work. Explicit cycles
        # still journal their inbox before applying a model-budget limit.
        state["inbox"] = await engine.inbox() if self.scheduler.collector is None else []
        if self.scheduler.collector is not None:
            from .conversations import recover_platform_turns

            recover_platform_turns(engine)

    async def prepare(self, state):
        if not self.scheduler.remaining_model_calls():
            self.scheduler.phase("waiting", wait_reason="model_quota")
            return Flow.STOP
        engine, policy = self.engine, state["policy"]
        settings = engine.store.read("companion")["data"]
        account = engine.run(check_host=False)["account"]
        blocked = engine.store.read("preferences")["data"]
        day = engine.clock().astimezone(ZoneInfo(policy["timezone"])).date().isoformat()
        selection_tag = stable_id("selection", json.dumps([
            day, blocked, engine.store.read("persona")["data"],
            engine.store.read("growth")["data"], settings, interest_profile(engine.store),
        ], ensure_ascii=False, sort_keys=True))
        with engine.store.db() as db:
            used = {row[0] for row in db.execute(
                "SELECT dedupe FROM actions WHERE account=? AND status NOT IN ('failed','prepared','cancelled')", (account,))}
            stamp = "received::timestamptz" if getattr(engine.store, "backend", None) == "postgres" else "julianday(received)"
            cutoff = "?::timestamptz" if getattr(engine.store, "backend", None) == "postgres" else "julianday(?)"
            pending = [engine.store.ledger_decode("events.body", row["body"]) for row in db.execute(
                f"SELECT id,body FROM events WHERE account=? AND {stamp}>={cutoff} ORDER BY {stamp},id",
                (account, engine.run(check_host=False).get("pending_since", engine.run(check_host=False)["started_at"])),
            ) if not engine.store.state(f"processed:{account}:{row['id']}", db=db)]
        inbox = list({event["id"]: event for event in [*pending, *state["inbox"]]}.values())
        if getattr(engine.store, "is_public_workspace", False):
            inbox = [event for event in inbox if event["kind"] != "dm_reply"]
        read_only = engine.run(check_host=False).get("read_only")
        fresh = [
            {**event, "is_guide": bool(settings["guide_uid"]) and event["uid"] == settings["guide_uid"]}
            for event in inbox
            if not read_only and f"{account}:{event['kind']}:{event['id']}" not in used
            and not engine.store.state(f"processed:{account}:{event['id']}")
            and delivery.eligible(engine, event)
            and (not hasattr(engine, "core_event_eligible") or engine.core_event_eligible(event))
            and event["uid"] not in [account, *blocked["blocked_uids"]]
            and not any(topic.casefold() in event.get("text", "").casefold() for topic in blocked["blocked_topics"] if topic)
        ]
        fresh.sort(key=lambda event: (not event["is_guide"], datetime.fromisoformat(event["received"])))
        # Resolve only bounded, actual guide invitations; resolved metadata does
        # not replace the received text or its identity evidence version.
        resolved = 0
        for index, event in enumerate(fresh):
            if event["is_guide"] and resolved < 3:
                fresh[index] = await engine.resolve_invitation(event)
                resolved += 1
        guided_target = next((
            (event["bvid"], event.get("page_index", 0)) for event in fresh
            if event["is_guide"] and re.fullmatch(r"BV[A-Za-z0-9]{10}", event.get("bvid", ""))
        ), None) if policy["items_per_cycle"] >= 2 else None
        guided_bvid, guided_page_index = guided_target or ("", 0)
        if guided_bvid:
            fresh = [event for event in fresh
                     if not event["is_guide"] or not event.get("bvid")
                     or (event["bvid"], event.get("page_index", 0)) == guided_target
                     ][:policy["items_per_cycle"] - 1]
        else:
            fresh = fresh[:policy["items_per_cycle"]]
        room = policy["items_per_cycle"] - len(fresh)
        with engine.store.db() as db:
            watched = db.execute("SELECT COUNT(*) FROM videos WHERE account=? AND day=?", (account, day)).fetchone()[0]
        state.update(settings=settings, account=account, blocked=blocked, selection_tag=selection_tag,
                     fresh=fresh, guided_bvid=guided_bvid, guided_page_index=guided_page_index,
                     room=room if watched < policy["daily_videos"] else 0)

    async def native_tools(self, state):
        from .native_tools import step

        if not state["fresh"] and await step(self.scheduler):
            return Flow.STOP

    async def video_gap(self, state):
        from .video_progress import step

        if not state["fresh"] and await step(self.scheduler):
            return Flow.STOP

    async def goal_learning(self, state):
        from .learning import step

        if not state["fresh"] and await step(self.scheduler):
            return Flow.FINALIZE

    async def explore(self, state):
        from .discovery import diversity, expand, observe

        engine = self.engine
        settings, account, blocked = (state[key] for key in ("settings", "account", "blocked"))
        selection_tag, room, fresh, guided_bvid = (state[key] for key in ("selection_tag", "room", "fresh", "guided_bvid"))
        selections = engine.store.state(f"selections:{account}", 0)
        every = settings["exploration_every"]
        explore = bool(every and (selections + 1) % every == 0) or diversity(engine)["persistent"]
        candidates = await engine.adapter.feed(
            following=settings["feed_source"] == "following" and not explore,
            limit=settings["candidate_limit"],
        ) if room and not guided_bvid else []

        def eligible(items):
            return [candidate for candidate in items
                    if not engine.store.state(f"learned:{account}:{candidate['bvid']}")
                    and not engine.store.state(f"inspected:{account}:{candidate['bvid']}")
                    and engine.store.state(f"uninteresting:{account}:{candidate['bvid']}") != selection_tag
                    and candidate["owner_uid"] not in blocked["blocked_uids"]
                    and re.fullmatch(r"BV[A-Za-z0-9]{10}", candidate["bvid"])
                    and not any(topic.casefold() in (candidate["title"] + " " + candidate.get("description", "")).casefold()
                                for topic in blocked["blocked_topics"] if topic)]

        candidates = eligible(candidates)
        if room and not fresh and not guided_bvid:
            candidates = eligible(await expand(self.scheduler, candidates, explore))
        origin = "guide" if guided_bvid else (
            "following" if settings["feed_source"] == "following" and not explore else "recommended")
        for candidate in candidates:
            observe(engine, candidate, "candidate", candidate.get("origin", origin))
        material, references = {"events": fresh, "video": None, "exploration": explore and not guided_bvid}, []
        if guided_bvid:
            observe(engine, next(event for event in fresh if event.get("bvid") == guided_bvid), "selected", "guide")
            try:
                invited = await engine.video_context(guided_bvid, page_index=state["guided_page_index"])
                if not any(topic.casefold() in (invited["title"] + " " + invited.get("description", "")).casefold()
                           for topic in blocked["blocked_topics"] if topic) and invited["owner_uid"] not in blocked["blocked_uids"]:
                    material["video"] = invited
                else:
                    material["guided_video_status"] = "视频属于配置中的排除项"
            except AssistantError as exc:
                if exc.code != "video_quota":
                    raise
                material["events"] = [event for event in fresh if not event["is_guide"]
                                      or (event.get("bvid"), event.get("page_index", 0))
                                      != (guided_bvid, state["guided_page_index"])]
                engine.store.audit("learning", {"event": "guided_video_deferred", "bvid": guided_bvid, "reason": "video_quota"})
        elif candidates:
            bvid = ""
            if settings["select_by_interest"]:
                from .algorithm_inputs import select

                choice, selection_versions = select(engine, candidates, explore=material["exploration"],
                                                    seed=f"{account}:{selections}")
                eligible_ids = set(choice["eligible_ids"])
                selectable = [c for c in candidates if c["bvid"] in eligible_ids]
                completed = choice["route"] != "model"
                if choice["route"] == "algorithm":
                    bvid = choice["selected"]
                elif choice["route"] == "model" and self.scheduler.remaining_model_calls() >= 2:
                    context, versions = packet(engine, {**material, "candidates": selectable})
                    plan = await self.scheduler.model(PLAN_PROMPT, context, PLAN_SCHEMA)
                    self.scheduler.alive()
                    verify_versions(engine.store, versions)
                    validate_plan(plan, selectable, context["memory"])
                    bvid, references = plan["bvid"], plan["memory_refs"]
                    engine.store.audit("learning", {"event": "selection", **plan})
                    completed = True
                self.scheduler.alive()
                verify_versions(engine.store, selection_versions)
                if completed:
                    engine.store.set_state(f"selections:{account}", selections + 1)
                    if not bvid:
                        for candidate in candidates:
                            engine.store.set_state(f"uninteresting:{account}:{candidate['bvid']}", selection_tag)
            elif not settings["select_by_interest"]:
                bvid = candidates[0]["bvid"]
            if bvid:
                selected = next(candidate for candidate in candidates if candidate["bvid"] == bvid)
                origin = selected.get("origin", origin)
                observe(engine, selected, "selected", origin)
                material["video"] = await engine.video_context(bvid)
        state.update(material=material, references=references, origin=origin)
        if not material["events"] and not material["video"]:
            return Flow.FINALIZE

    async def decide(self, state):
        from .discovery import observe
        from .native_tools import from_decision
        from .video_progress import from_decision as request_gap

        engine, material, account = self.engine, state["material"], state["account"]
        growth_version = engine.store.read("growth")["version"]
        decision = await self.scheduler.decide(material, state["references"])
        self.scheduler.alive()
        action = validate_decision(decision, material)
        saved = save_learning(engine, material, decision, growth_version)
        if from_decision(engine, material, decision):
            action = None
        request_gap(engine, material, decision)
        video = material["video"]
        if video:
            observe(engine, video, "read", state["origin"], decision.get("video_topics", []))
        deferred, outcome = set(), "no_reply"
        if action:
            try:
                prepared = engine.prepare(*action, retry_unsent=True)
                result = await engine.execute(prepared["id"], guard=lambda: verify_decision_context(engine.store, decision))
                engine.store.audit("learning", {"event": "action_result", **result})
                outcome = result["status"]
                if result["status"] == "cancelled":
                    event_id = action[1].get("event_id")
                    for event in material["events"]:
                        if event["id"] == event_id:
                            self.defer(event, "before_send_cancelled")
                            deferred.add(event_id)
            except AssistantError as exc:
                if exc.code not in ("interval", "quota_exhausted", "action_disabled", "reply_loop_limit",
                                    "insufficient_content", "blocked_target", "blocked_topic"):
                    raise
                engine.store.audit("learning", {"event": "action_skipped", **exc.as_dict()})
                outcome = exc.code
                if exc.code in delivery.TEMPORARY:
                    event_id = action[1].get("event_id")
                    for event in material["events"]:
                        if event["id"] == event_id:
                            self.defer(event, exc.code)
                            deferred.add(event_id)
        if decision["note"].strip():
            engine.store.audit("discoveries", {"note": decision["note"][:4000]})
        if video:
            engine.store.set_state(f"inspected:{account}:{video['bvid']}", True)
            if saved["knowledge"]:
                engine.store.set_state(f"learned:{account}:{video['bvid']}", True)
        for event in material["events"]:
            if event["id"] not in deferred:
                targeted = action and action[1].get("event_id") == event["id"]
                reason = outcome if targeted else "no_reply"
                delivery.finish(engine, event, reason)
                if hasattr(engine, "record_core_delivery"):
                    engine.record_core_delivery(event, reason, deferred=False)
        engine.store.audit("learning", {"event": "saved", **saved})
        engine.store.checkpoint()

    def defer(self, event, reason):
        delivery.defer(self.engine, event, reason)
        if hasattr(self.engine, "record_core_delivery"):
            self.engine.record_core_delivery(event, reason, deferred=True)

    async def reflect(self, state):
        await self.scheduler.maybe_reflect()

    async def maintain(self, state):
        await self.scheduler.maintenance()
