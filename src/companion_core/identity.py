"""Reversible inferred identity graph. This graph never confers authorization."""
import re

from jsonschema import ValidationError, validate

from . import events
from .contracts import ActorRef, stable_key
from .errors import AssistantError
from .schema import IDENTITY_PROPOSALS, obj, validate_identity_link
from .storage import utcnow


class IdentityService:
    REVIEW_TEXT_LIMIT = 32000

    def __init__(self, store):
        self.store = store
        self.issues = []

    def _load_events(self, db, event_ids):
        """Bound the SQL variable count without opening a connection for each proof."""
        ids, result = sorted(set(event_ids)), {}
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            rows = db.execute("SELECT id,body,version,deleted FROM core_events WHERE id IN ("
                              + ",".join("?" for _ in batch) + ")", batch)
            for row in rows:
                result[row["id"]] = {"event": self.store.ledger_decode("core_events.body", row["body"]), "version": row["version"],
                                     "deleted": bool(row["deleted"])}
        return result

    def _evidence(self, references, loaded=None):
        if loaded is None:
            with self.store.db() as db:
                loaded = self._load_events(db, (ref["event_id"] for ref in references))
        result = []
        for ref in references:
            item = loaded.get(ref["event_id"])
            if not item or item["deleted"]:
                raise AssistantError("unknown_evidence", "身份依据的事件不存在或已删除")
            if item["version"] != ref["version"] or len(ref["quote"].strip()) < 4 or ref["quote"] not in item["event"]["text"]:
                raise AssistantError("identity_evidence_changed", "身份依据已改变或引用不是原文")
            result.append({"event": item["event"], "version": item["version"]})
        if len({r["event"]["id"] for r in result}) != len(result):
            raise AssistantError("identity_duplicate_evidence", "重复引用不能增加身份可信度")
        return result

    def _snapshot(self):
        records, issues = self.store.records("identity_links")
        valid = []
        for record in records:
            try:
                # Also check records loaded through an older in-process parsed cache.
                validate_identity_link(record["data"])
            except AssistantError as exc:
                issues.append({"file": str(self.store.path("identity_links", record["data"]["id"])), **exc.as_dict()})
                continue
            valid.append(record)
        with self.store.db() as db:
            loaded = self._load_events(db, (ref["event_id"] for record in valid for ref in record["data"]["evidence"]))
            indexed = {row["id"]: tuple(row) for row in db.execute("SELECT * FROM core_identity_links")}
        result = []
        for record in valid:
            doc = record["data"]
            effective = doc["status"]
            if not doc["manual"] and effective in ("suspected", "hard"):
                try:
                    proof = self._evidence(doc["evidence"], loaded)
                    if effective == "hard" and ({ActorRef(**p["event"]["actor"]).key for p in proof}
                                              != {ActorRef(**doc["left"]).key, ActorRef(**doc["right"]).key}):
                        effective = "disputed"
                except AssistantError:
                    effective = "disputed"
            result.append({**record, "effective_status": effective})
        # A conflicting direct association invalidates an inferred transitive component too.
        blocked = [tuple(ActorRef(**r["data"][side]).key for side in ("left", "right"))
                   for r in result if r["effective_status"] in ("disputed", "revoked")]
        hard = [r for r in result if r["effective_status"] == "hard"]
        adjacency = {}
        for edge in hard:
            left, right = (ActorRef(**edge["data"][side]).key for side in ("left", "right"))
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        components = {}
        for actor in adjacency:
            if actor in components:
                continue
            pending = [actor]
            while pending:
                member = pending.pop()
                if member in components:
                    continue
                components[member] = actor
                pending.extend(adjacency[member] - components.keys())
        conflicts = {components[left] for left, right in blocked
                     if left in components and components[left] == components.get(right)}
        for edge in hard:
            if components[ActorRef(**edge["data"]["left"]).key] in conflicts and not edge["data"]["manual"]:
                edge["effective_status"] = "disputed"
        desired = {r["data"]["id"]: (r["data"]["id"], ActorRef(**r["data"]["left"]).key,
                   ActorRef(**r["data"]["right"]).key, r["effective_status"], r["version"]) for r in result}
        changed = [row for key, row in desired.items() if indexed.get(key) != row]
        stale = [(key,) for key in indexed.keys() - desired.keys()]
        # Hash-checked YAML is authoritative. Only changed rows touch the rebuildable index.
        if changed or stale:
            with self.store.db() as db:
                db.executemany("""INSERT INTO core_identity_links VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET left_actor=excluded.left_actor,right_actor=excluded.right_actor,
                    status=excluded.status,version=excluded.version""", changed)
                db.executemany("DELETE FROM core_identity_links WHERE id=?", stale)
        self.issues = issues
        return {"links": result, "events": loaded, "issues": issues}

    def links(self):
        with self.store.lock:
            return self._snapshot()["links"]

    def apply(self, proposal, *, allowed_evidence=None):
        try:
            validate([proposal], IDENTITY_PROPOSALS)
        except ValidationError as exc:
            raise AssistantError("invalid_identity_proposal", "身份提议不符合结构约定") from exc
        left, right = ActorRef(**proposal["left"]), ActorRef(**proposal["right"])
        if left.platform == right.platform:
            raise AssistantError("identity_same_platform", "此接口用于跨平台身份关联")
        if allowed_evidence is not None and not {e["event_id"] for e in proposal["evidence"]} <= set(allowed_evidence):
            raise AssistantError("identity_unprovided_evidence", "模型只能引用本轮提供的身份材料")
        link_id = stable_key("link", *sorted((left.key, right.key)))
        with self.store.lock:
            proof = self._evidence(proposal["evidence"])
            actors = {ActorRef(**p["event"]["actor"]).key for p in proof}
            if not actors <= {left.key, right.key}:
                raise AssistantError("identity_wrong_actor", "不能使用第三人的发言确认身份")
            try:
                old = self.store.read("identity_links", link_id)
            except AssistantError as exc:
                if exc.code != "not_found":
                    raise
                old = None
            kind = proposal["kind"]
            if old and (old["data"]["locked"] or old["data"]["status"] in ("revoked", "disputed")):
                raise AssistantError("identity_locked", "已撤销、争议或锁定的关联需要管理端裁决")
            old_proof = self._evidence(old["data"]["evidence"]) if old else []
            if kind == "claim":
                identifier = r"(?<![A-Za-z0-9_])" + re.escape(right.user_id) + r"(?![A-Za-z0-9_])"
                if actors != {left.key} or not any(re.search(identifier, r["quote"]) for r in proposal["evidence"]):
                    raise AssistantError("identity_not_self_claim", "单方声明必须来自本人并包含被声明的账号")
                status = old["data"]["status"] if old else "suspected"
            elif kind == "correspondence":
                if (not old or old["data"]["status"] not in ("suspected", "hard")
                        or actors != {left.key, right.key} or not proposal["specific_correspondence"]
                        or any(len(r["quote"].strip()) < 8 for r in proposal["evidence"])):
                    raise AssistantError("identity_insufficient_proof", "硬关联需要已有声明和两平台独立、具体的对应证据")
                status = "hard"
            else:
                if not old:
                    raise AssistantError("identity_missing_link", "没有可标记争议的关联")
                status = "disputed"
            refs = {r["event_id"]: r for r in (old["data"]["evidence"] if old else [])}
            refs.update({r["event_id"]: r for r in proposal["evidence"]})
            # Keep the original self-claim and at least one independent proof per endpoint
            # even after many later proposals fill the bounded evidence collection.
            anchors = {}
            for item in (*old_proof, *proof):
                actor_key = ActorRef(**item["event"]["actor"]).key
                if actor_key in (left.key, right.key):
                    anchors.setdefault(actor_key, item["event"]["id"])
            retained = set(anchors.values())
            recent = [key for key in refs if key not in retained]
            retained.update(recent[-(30 - len(retained)):])
            history = old["data"]["history"] if old else []
            entry = {"status": status, "kind": kind, "reason": proposal["reason"], "evidence": proposal["evidence"]}
            if history and all(history[-1].get(k) == v for k, v in entry.items()):
                return old
            doc = {"schema_version": 1, "id": link_id, "left": left.as_dict(), "right": right.as_dict(),
                   "status": status, "reason": proposal["reason"],
                   "evidence": [ref for key, ref in refs.items() if key in retained],
                   "history": [*history, {**entry, "at": utcnow()}][-100:], "manual": False, "locked": False}
            saved = self.store.write("identity_links", doc, old["version"] if old else None, "identity-core", link_id)
        self.links()
        return saved

    def apply_model(self, proposals, *, allowed_evidence, allowed_kinds=None):
        """Reject unsupported model claims without turning a citation mistake into an outage."""
        rejected_codes = {
            "invalid_identity_proposal", "identity_evidence_changed", "identity_duplicate_evidence",
            "identity_same_platform", "identity_unprovided_evidence", "unknown_evidence", "identity_wrong_actor",
            "identity_locked", "identity_not_self_claim", "identity_insufficient_proof", "identity_missing_link",
            "identity_separate_review",
        }
        accepted, rejected = [], []
        for proposal in proposals:
            try:
                if allowed_kinds and proposal.get("kind") not in allowed_kinds:
                    raise AssistantError("identity_separate_review", "跨平台判断需要独立证据评估")
                accepted.append(self.apply(proposal, allowed_evidence=allowed_evidence))
            except AssistantError as exc:
                if exc.code not in rejected_codes:
                    raise
                rejected.append({"at": utcnow(), "error": exc.code})
        if rejected:
            with self.store.lock:
                previous = self.store.state("identity-rejections", [])
                self.store.set_state("identity-rejections", (previous + rejected)[-20:])
        return accepted

    def revoke(self, link_id, expected_version, reason):
        old = self.store.read("identity_links", link_id)
        doc = old["data"]
        doc.update(status="revoked", reason=reason, manual=True, locked=True)
        doc["history"] = [*doc["history"], {"at": utcnow(), "status": "revoked", "reason": reason}][-100:]
        result = self.store.write("identity_links", doc, expected_version, "identity-admin", link_id, allow_locked=True)
        self.links()
        return result

    @staticmethod
    def _person(actor, links):
        members = {actor.key: actor.as_dict()}
        adjacency = {}
        for item in links:
            if item["effective_status"] != "hard":
                continue
            left, right = (ActorRef(**item["data"][side]) for side in ("left", "right"))
            adjacency.setdefault(left.key, []).append(right)
            adjacency.setdefault(right.key, []).append(left)
        pending = [actor.key]
        while pending:
            for ref in adjacency.get(pending.pop(), []):
                if ref.key not in members:
                    members[ref.key] = ref.as_dict()
                    pending.append(ref.key)
        return {"person_id": stable_key("person", *sorted(members)), "members": [members[k] for k in sorted(members)],
                "inferred": len(members) > 1, "grants_permissions": False}

    def person(self, actor):
        return self._person(actor, self.links())

    @staticmethod
    def _candidates(actor, links, *, include_hard=False):
        statuses = ("suspected", "hard") if include_hard else ("suspected",)
        return [r for r in links if r["effective_status"] in statuses and not r["data"]["locked"]
                and actor.key in {ActorRef(**r["data"][side]).key for side in ("left", "right")}]

    def candidates(self, actor, *, include_hard=False):
        return self._candidates(actor, self.links(), include_hard=include_hard)

    @staticmethod
    def _review_excerpt(item, *, quote=None, limit=800):
        source = item["event"]
        text = source["text"]
        start = max(0, text.index(quote) - (limit - len(quote)) // 2) if quote else 0
        excerpt = text[start:start + limit]
        # Attachments, native metadata and unneeded message bodies are not identity evidence.
        visible = {key: source[key] for key in ("id", "platform", "actor", "occurred_at", "visibility")}
        visible["text"] = excerpt
        return {"event": visible, "version": item["version"], "excerpt": len(excerpt) != len(text),
                "source_text_length": len(text)}

    def _context(self, actor, snapshot, *, include_evidence=False, person=None):
        candidates = self._candidates(actor, snapshot["links"], include_hard=include_evidence)
        result = {"person": person or self._person(actor, snapshot["links"]), "issues": snapshot["issues"]}
        if include_evidence:
            # Existing exact-version anchors precede recent chat, so ordinary conversation cannot
            # evict the self-claim that makes later cross-platform correspondence meaningful.
            candidates = candidates[:5]
            result["candidates"] = []
            proof, provided = [], set()
            used = 0
            actors = {actor.key: actor}
            for item in candidates:
                anchors = {}
                for ref in item["data"]["evidence"]:
                    try:
                        anchor = self._evidence([ref], snapshot["events"])[0]
                    except AssistantError:
                        continue
                    key = ActorRef(**anchor["event"]["actor"]).key
                    anchors.setdefault(key, (ref, anchor))
                selected = []
                for side in ("left", "right"):
                    ref = ActorRef(**item["data"][side])
                    actors[ref.key] = ref
                    if ref.key in anchors:
                        citation, anchor = anchors[ref.key]
                        selected.append({key: citation[key] for key in ("event_id", "version")})
                        identity = citation["event_id"], citation["quote"]
                        if identity not in provided:
                            excerpt = self._review_excerpt(anchor, quote=citation["quote"], limit=2200)
                            proof.append(excerpt)
                            used += len(excerpt["event"]["text"])
                            provided.add(identity)
                data = {k: v for k, v in item["data"].items() if k not in ("history", "evidence", "reason")}
                data.update(reason=item["data"]["reason"][:400], evidence=selected)
                result["candidates"].append({**item, "data": data})
            recent_by_actor = [list(reversed(events.recent(self.store, actor=ref, limit=4))) for ref in actors.values()]
            seen_ids = {item["event"]["id"] for item in proof}
            omitted = 0
            for offset in range(4):
                for recent in recent_by_actor:
                    if offset >= len(recent):
                        continue
                    item = recent[offset]
                    if item["event"]["id"] in seen_ids:
                        continue
                    excerpt = self._review_excerpt(item)
                    if used + len(excerpt["event"]["text"]) > self.REVIEW_TEXT_LIMIT:
                        omitted += 1
                        continue
                    proof.append(excerpt)
                    used += len(excerpt["event"]["text"])
                    seen_ids.add(item["event"]["id"])
            result.update(evidence=proof, evidence_text_characters=used, evidence_omitted=omitted)
        else:
            # Public dialogue context never contains the other platform's private proof quotes.
            result["candidates"] = [{"id": r["data"]["id"], "status": r["effective_status"]} for r in candidates]
        return result

    def context(self, actor, *, include_evidence=False):
        with self.store.lock:
            return self._context(actor, self._snapshot(), include_evidence=include_evidence)

    @staticmethod
    def _component_links(actor, links):
        """Include candidates and blocking edges; they can change a hard component's validity."""
        adjacency = {}
        for item in links:
            left, right = (ActorRef(**item["data"][side]).key for side in ("left", "right"))
            adjacency.setdefault(left, []).append((right, item))
            adjacency.setdefault(right, []).append((left, item))
        members, selected, pending = {actor.key}, {}, [actor.key]
        while pending:
            for other, item in adjacency.get(pending.pop(), []):
                selected[item["data"]["id"]] = item
                if other not in members:
                    members.add(other)
                    pending.append(other)
        return [selected[key] for key in sorted(selected)]

    def _decision_context(self, actor, snapshot, *, input_event_ids=()):
        from .people import profile_snapshot

        person = self._person(actor, snapshot["links"])
        person_profile, relationship_dependencies = profile_snapshot(self.store, person)
        component = self._component_links(actor, snapshot["links"])
        event_ids = sorted({ref["event_id"] for r in component for ref in r["data"]["evidence"]})
        proof = []
        for event_id in event_ids:
            current = snapshot["events"].get(event_id)
            proof.append({"id": event_id, "version": current["version"] if current else None,
                          "deleted": current["deleted"] if current else True})
        dependency = {"schema_version": 1, "actor": actor.as_dict(), "person": person,
                      "links": [{"id": r["data"]["id"], "version": r["version"],
                                 "effective_status": r["effective_status"]} for r in component],
                      "evidence": proof, "relationships": relationship_dependencies}
        inputs = sorted(set(input_event_ids))
        if inputs:
            with self.store.db() as db:
                loaded = self._load_events(db, inputs)
            dependency["input_event_ids"] = inputs
            dependency["input_events"] = [
                {"id": event_id, "version": current["version"] if current else None,
                 "deleted": current["deleted"] if current else True}
                for event_id in inputs for current in (loaded.get(event_id),)
            ]
        return {"identity": self._context(actor, snapshot, person=person),
                "person_profile": person_profile, "dependency": dependency}

    def decision_context(self, actor, *, input_event_ids=()):
        """Freeze all identity and impression inputs without exposing proof text to ordinary replies."""
        with self.store.lock:
            return self._decision_context(actor, self._snapshot(), input_event_ids=input_event_ids)

    def verify_context(self, dependency):
        try:
            current = self.decision_context(ActorRef(**dependency["actor"]),
                                            input_event_ids=dependency.get("input_event_ids", ()))["dependency"]
        except (KeyError, TypeError, AssistantError) as exc:
            raise AssistantError("context_changed", "身份关联或人物资料依赖已不可用，请重新生成决策") from exc
        if current != dependency:
            raise AssistantError("context_changed", "身份关联、来源证据或人物资料已变化，请重新生成决策")

    async def review(self, actor, generate, guard):
        from .decision import IDENTITY_INSTRUCTIONS
        with self.store.lock:
            snapshot = self._snapshot()
            context = self._context(actor, snapshot, include_evidence=True)
            dependency = self._decision_context(actor, snapshot)["dependency"]
        if not context["candidates"] or len({e["event"]["platform"] for e in context["evidence"]}) < 2:
            return []
        evidence = context.pop("evidence")
        output = await generate("只评估跨平台身份候选，证据不足就返回空列表。" + IDENTITY_INSTRUCTIONS,
                                {**context, "identity_evidence": evidence},
                                obj({"identity_proposals": IDENTITY_PROPOSALS}))
        guard()
        allowed = {e["event"]["id"] for e in evidence}
        with self.store.lock:
            self.verify_context(dependency)
            with self.store.db() as db:
                current = self._load_events(db, allowed)
            if any((item := current.get(e["event"]["id"])) is None or item["deleted"]
                   or item["version"] != e["version"] for e in evidence):
                raise AssistantError("context_changed", "本轮身份评估所读消息已变化，请重新评估")
            return self.apply_model(output["identity_proposals"], allowed_evidence=allowed)
