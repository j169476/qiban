"""Versioned privacy fragments. Original records keep their existing access policies."""

import re
from datetime import datetime, timezone
from uuid import uuid4

from .contracts import ActorRef, Event, stable_key
from .errors import AssistantError

KINDS = (
    "event",
    "memories",
    "knowledge",
    "diaries",
    "conversation_turns",
    "conversation_summaries",
    "relationships",
    "skills",
    "interest_signals",
    "task_results",
    "source_snapshots",
    "knowledge_reviews",
    "task_requests",
    "video_progress",
    "interaction_growth",
    "method_trials",
    "emotion_events",
    "partition",
)
LEVELS = ("credential", "private", "circle", "social")
CATEGORIES = ("unknown", "credential", "name", "home", "venue", "itinerary", "meeting", "ordinary")
SENSITIVE = {"name", "home", "venue", "itinerary", "meeting"}
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\b(?:sk-[\w-]{10,}|gh[pousr]_[\w]{10,}|github_pat_[\w]+|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b"
    r"|(?:\b(?:bearer|basic)\s+)[A-Za-z0-9+/_.=-]{5,}"
    r"|(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|password|passwd|secret|令牌|密钥|密码)"
    r"\s*['\"]?\s*(?:[=:：]|是|为)\s*['\"]?[^\s,;，；。'\"]+",
    re.I,
)
MASK = "〔凭据已遮盖〕"


def mask(value):
    if isinstance(value, str):
        return SECRET.sub(MASK, value)
    if isinstance(value, dict):
        return {
            k: (
                MASK
                if re.fullmatch(r"api[_-]?key|access_token|refresh_token|password|passwd|secret", k, re.I)
                else mask(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value


def enqueue(db, kind, identifier, version):
    if kind not in KINDS:
        return
    key = kind + "/" + identifier
    changed = db.execute(
        "INSERT INTO privacy_jobs(resource,source_version) VALUES(?,?) ON CONFLICT(resource) "
        "DO UPDATE SET source_version=excluded.source_version,cursor_pos=0,complete=0,due=0 "
        "WHERE privacy_jobs.source_version<>excluded.source_version",
        (key, version),
    )
    if changed.rowcount:
        db.execute("DELETE FROM privacy_slices WHERE resource=?", (key,))


def invalidate(db, resource):
    affected = [
        resource,
        *(
            r[0]
            for r in db.execute(
                "SELECT resource FROM privacy_review_sources WHERE context_source=?", (resource,)
            )
        ),
    ]
    db.execute("UPDATE privacy_jobs SET complete=0,cursor_pos=0,due=0 WHERE resource=ANY(?)", (affected,))
    db.execute("DELETE FROM privacy_slices WHERE resource=ANY(?)", (affected,))
    db.execute("DELETE FROM privacy_review_sources WHERE resource=ANY(?)", (affected,))


def repair(store, now):
    """Rotate a bounded backfill independently of whether the source had a live writer."""
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if store.state("privacy-backfill-after", 0, db) > now:
            return
        for row in db.execute(
            "SELECT kind,id,version FROM content_records c WHERE kind=ANY(?) AND NOT EXISTS "
            "(SELECT 1 FROM privacy_jobs j WHERE j.resource=c.kind || '/' || c.id AND "
            "j.source_version=c.version) ORDER BY kind,id LIMIT 40",
            (list(KINDS[1:]),),
        ).fetchall():
            enqueue(db, *tuple(row))
        for row in db.execute(
            "SELECT id,version FROM core_events e WHERE deleted=0 AND NOT EXISTS (SELECT 1 FROM "
            "privacy_jobs j WHERE j.resource='event/' || e.id AND j.source_version=e.version) "
            "ORDER BY id LIMIT 40"
        ).fetchall():
            enqueue(db, "event", *tuple(row))
        cursor = store.state("privacy-scope-cursor", "", db)
        page = db.execute(
            "SELECT s.* FROM privacy_slices s JOIN privacy_jobs j USING(resource) WHERE j.complete=1 "
            "AND s.id>? ORDER BY s.id LIMIT 12",
            (cursor,),
        ).fetchall()
        for row in page:
            if not live_source(store, row, unpack(store, row)):
                invalidate(db, row["resource"])
        store.set_state("privacy-scope-cursor", page[-1]["id"] if len(page) == 12 else "", db)
        growth = store.state("public-growth", None, db)
        if isinstance(growth, dict) and isinstance(growth.get("version"), str):
            enqueue(db, "partition", "public-growth", growth["version"])
        store.set_state("privacy-backfill-after", now + 60, db)


def source(store, resource):
    from . import events

    kind, identifier = resource.split("/", 1)
    if kind == "event":
        item = events.get(store, identifier)
        event = Event.from_dict(item["event"])
        return event.text, event, item["version"]
    item = (
        store.state("public-growth")
        if resource == "partition/public-growth"
        else store.read(kind, identifier)
    )
    if not item:
        raise AssistantError("not_found", "原资料已移除")
    data = item["data"]
    origin = data.get("source") or {}
    reference = origin.get("reference", "") if isinstance(origin, dict) else ""
    event = None
    if reference.startswith("event-"):
        try:
            event = Event.from_dict(events.get(store, reference)["event"])
        except AssistantError:
            pass
    if event is None:
        from .governance import GovernanceRepository

        policy = GovernanceRepository(store).policy(resource)
        if policy and policy.conversation:
            with store.db() as db:
                anchor = db.execute(
                    "SELECT id FROM core_events WHERE deleted=0 AND conversation=? AND "
                    "(actor=ANY(?) OR ?='public') ORDER BY occurred DESC,id LIMIT 1",
                    (policy.conversation, list(policy.owners), policy.audience),
                ).fetchone()
            if anchor:
                event = Event.from_dict(events.get(store, anchor[0])["event"])
    parts = []

    def collect(value):
        if isinstance(value, str):
            if value:
                parts.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    for field, value in data.items():
        if field not in {"schema_version", "id", "source", "locked", "occurred_at", "version"}:
            collect(value)
    return "\n".join(parts), event, item["version"]


def add_slice(store, db, job, index, **body):
    identifier = stable_key("privacy", job["resource"], job["source_version"], index)
    category = body["category"]
    fingerprint = None
    value = body.get("value") or body.get("text", "")
    if value:
        import hashlib
        import hmac

        key = store.state("privacy-index-key", None, db)
        if not key:
            key = uuid4().hex + uuid4().hex
            store.set_state("privacy-index-key", key, db)
        fingerprint = hmac.new(key.encode(), value.strip().casefold().encode(), hashlib.sha256).hexdigest()
    correction = db.execute(
        "SELECT level FROM privacy_corrections WHERE resource=? AND fingerprint=?",
        (job["resource"], fingerprint),
    ).fetchone()
    if category == "credential":
        correction = None
    packed = store.ledger_encode("privacy:" + identifier, body)
    db.execute(
        "INSERT INTO privacy_slices VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (
            identifier,
            job["resource"],
            job["source_version"],
            uuid4().hex,
            "corrected" if correction else "reviewed",
            correction[0] if correction else body["level"],
            category,
            fingerprint,
            packed,
        ),
    )


def unpack(store, row):
    return store.ledger_decode("privacy:" + row["id"], row["body"])


def live_source(store, row, body):
    from .governance import GovernanceRepository

    policy = GovernanceRepository(store).policy(row["resource"])
    if (
        not policy
        or not policy.valid
        or policy.version != body["policy"]
        or policy.classification in {"restricted", "unclassified", "secret"}
    ):
        return False
    if policy.expires_at is not None and policy.expires_at <= datetime.now(timezone.utc).timestamp():
        return False
    with store.db() as db:
        job = db.execute(
            "SELECT complete FROM privacy_jobs WHERE resource=? AND source_version=?",
            (row["resource"], row["source_version"]),
        ).fetchone()
    if not job or not job[0]:
        return False
    if body.get("permission") is not None:
        try:
            _, event, version = source(store, row["resource"])
            if (
                not event
                or version != row["source_version"]
                or source_basis(store, event) != body["permission"]
            ):
                return False
        except AssistantError:
            return False
    repository = GovernanceRepository(store)
    for resource, version in body.get("dependencies", ()):
        dep = repository.policy(resource)
        if not dep or not dep.valid or dep.version != version:
            return False
    return True


def source_basis(store, event):
    from .group_modes import observing, settings
    from .reminders import permission_basis, scope_for

    if event.platform in {"qq", "wechat"}:
        target_field = "target_id" if event.platform == "qq" else "target_ref"
        if not event.metadata.get(event.platform, {}).get(target_field):
            return None
        scope = scope_for(store, event)
        if scope.kind == "group" and observing(
            store, scope, datetime.fromisoformat(event.occurred_at).timestamp()
        ):
            return "listening/" + settings(store, scope)["version"]
        return permission_basis(store, scope)
    if event.platform == "bilibili" and event.visibility == "private":
        if event.actor.user_id in store.read("preferences")["data"].get("blocked_uids", []):
            return None
        settings = store.state("desktop-bili-policy:" + event.account, {})
        configured = settings.get("actions", store.read("policy")["data"].get("actions", {}))
        if not configured.get("dm_reply", {}).get("enabled", False):
            return None
    return "original"


def same_people(store, event):
    from .identity import IdentityService

    return {ActorRef(**r).key for r in IdentityService(store).person(event.actor)["members"]}


def peers(store, row):
    """Count first-hand sources once. Copies and synthetic assistant turns have no vote."""
    if not row["fingerprint"]:
        return []
    with store.db() as db:
        rows = db.execute(
            "SELECT * FROM privacy_slices WHERE fingerprint=? AND resource LIKE 'event/%' "
            "ORDER BY id LIMIT 96",
            (row["fingerprint"],),
        ).fetchall()
    entries = []
    for item in rows:
        body = unpack(store, item)
        if (
            item["category"] == row["category"]
            and body["evidence"] in {"natural", "self", "venue", "invitation"}
            and live_source(store, item, body)
            and not (item["status"] == "corrected" and item["level"] == "private")
        ):
            _, actor_event, _ = source(store, item["resource"])
            if actor_event:
                body["person"] = min(same_people(store, actor_event))
            entries.append(body)
    if row["category"] == "name":
        origin = unpack(store, row)
        # Ground a name to one self-introduced person in the group. Homonyms in
        # other circles stay separate unless a verified account link joins them.
        owners = {}
        for b in entries:
            if b["evidence"] == "self":
                owners.setdefault(b["thread"], set()).add(b.get("person", b["actor"]))
        here = owners.get(origin["thread"], set())
        if len(here) != 1:
            return [b for b in entries if b["thread"] == origin["thread"]] if not here else []
        return [b for b in entries if owners.get(b["thread"]) == here]
    return entries


def eligible_levels(store, row, body):
    category = row["category"]
    if category == "credential":
        return ["credential"]
    options = ["private"]
    if body["evidence"] == "invitation":
        if (
            category == "meeting"
            and body.get("occasion")
            and body.get("until")
            and (body.get("invited") or body.get("whole_group"))
        ):
            options.append("circle")
        return options
    if category in {"home", "itinerary", "unknown"} or body["evidence"] in {"relayed", "uncertain"}:
        return options
    if category == "venue" and row["fingerprint"]:
        with store.db() as db:
            warnings = db.execute(
                "SELECT * FROM privacy_slices WHERE fingerprint=? AND category='home'", (row["fingerprint"],)
            ).fetchall()
        for warning in warnings:
            other = unpack(store, warning)
            if (
                other["actor"] == body["actor"]
                or other["thread"] == body["thread"]
                and other["evidence"] == "self"
            ) and (other["at"] >= body["at"] and live_source(store, warning, other)):
                return options
    entries = peers(store, row)
    circles = {
        r["thread"]
        for r in entries
        if r["visibility"] == "group"
        and len({e.get("person", e["actor"]) for e in entries if e["thread"] == r["thread"]}) >= 2
    }
    if circles or body["evidence"] == "invitation" and body.get("until") and body["visibility"] == "group":
        options.append("circle")
    if (
        category == "venue"
        and body["evidence"] == "venue"
        or category == "ordinary"
        and body["evidence"] == "general"
        or category == "name"
        and len(circles) >= 2
        and any(r["evidence"] == "self" for r in entries)
        and len({r["at"][:10] for r in entries}) >= 2
    ):
        options.append("social")
    return options


def level(store, row, body):
    options = eligible_levels(store, row, body)
    requested = row["level"]
    if row["category"] == "name" and row["status"] != "corrected" and body["evidence"] in {"self", "natural"}:
        requested = "social"
    if requested == "credential":
        return requested
    return next(
        (x for x in reversed(LEVELS) if x in options and LEVELS.index(x) <= LEVELS.index(requested)),
        "private",
    )


def model_layer_enabled(store, resource):
    from .context_workspace import revision

    layers = {
        "memories": "memory_records",
        "knowledge": "memory_records",
        "diaries": "memory_records",
        "relationships": "relations",
        "conversation_summaries": "summary",
        "interest_signals": "growth",
        "interaction_growth": "growth",
        "partition": "growth",
        "emotion_events": "growth",
    }
    layer = layers.get(resource.split("/", 1)[0])
    return not layer or layer not in revision(store)["config"].get("disabled", [])


def visible_slice(store, row, body, event):
    if not model_layer_enabled(store, row["resource"]):
        return False
    if not live_source(store, row, body) or row["category"] == "credential" or row["level"] == "credential":
        return False
    if (
        body.get("until")
        and datetime.fromisoformat(body["until"]).timestamp() <= datetime.now(timezone.utc).timestamp()
    ):
        return False
    tier = level(store, row, body)
    if event.visibility == "public":
        return False
    if not row["resource"].startswith("event/"):
        from .governance import GovernedContext

        target = GovernedContext(store, event)
        if not target.access.can_read(target.request, row["resource"]) or not raw_allowed(
            store, target.request, row["resource"], dependencies_only=True
        ):
            return False
    if body["evidence"] == "invitation":
        if tier != "circle" or not body.get("occasion") or body["occasion"] not in event.text:
            return False
        if event.visibility == "group":
            return body.get("whole_group", False) and event.thread == body["thread"]
        return bool(same_people(store, event) & set(body.get("invited", [])))
    if tier == "private":
        return (
            event.visibility == "private"
            and body["evidence"] not in {"relayed", "uncertain"}
            and body["actor"] in same_people(store, event)
            and body.get("subject") in ("", body["actor"])
        )
    if tier == "social":
        return event.platform in {"qq", "wechat"} or event.thread and event.thread == body["thread"]
    members = peers(store, row)
    circle = body["thread"]
    if event.thread == circle:
        return True
    if event.visibility == "group":
        # Whole groups require evidence in that group as well, even across accounts/platforms.
        return (
            body["evidence"] != "invitation"
            and len({r.get("person", r["actor"]) for r in members if r["thread"] == event.thread}) >= 2
        )
    if event.platform not in {"wechat", "qq"}:
        return False
    identities = same_people(store, event)
    with store.db() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM core_events WHERE deleted=0 AND conversation=? AND actor=ANY(?) "
                "AND occurred<=? LIMIT 1",
                (circle, list(identities), body["at"]),
            ).fetchone()
        )


def project(store, event, resources=None, limit=10):
    """Project reviewed fragments for an exact recipient. Derived records retain their source boundary."""
    with store.db() as db:
        if resources is not None:
            if not resources:
                return [], []
            rows = db.execute(
                "SELECT * FROM privacy_slices WHERE resource=ANY(?) ORDER BY id LIMIT 96", (list(resources),)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM privacy_slices WHERE level IN "
                "('social','circle','private') ORDER BY resource DESC,id LIMIT 64"
            ).fetchall()
    values, pins = [], []
    for row in rows:
        body = unpack(store, row)
        if visible_slice(store, row, body, event):
            text = (
                body["value"]
                if row["category"] in {"venue", "home", "name", "meeting"} and body["value"]
                else body["text"]
            )
            values.append(
                {"text": text, "category": row["category"], "said_at": body["at"], "source": row["id"]}
            )
            pins.append((row["id"], row["version"]))
            if len(values) >= limit:
                break
    return values, pins


def check_projection(store, event, pins):
    with store.db() as db:
        for identifier, version in pins:
            row = db.execute("SELECT * FROM privacy_slices WHERE id=?", (identifier,)).fetchone()
            if (
                not row
                or row["version"] != version
                or not visible_slice(store, row, unpack(store, row), event)
            ):
                raise AssistantError("context_changed", "当前受众可用的片段已变化")


def bind(store, resource, pins):
    if not pins or getattr(store, "backend", None) != "postgres":
        return
    with store.db() as db:
        db.executemany(
            "INSERT INTO privacy_dependencies VALUES(?,?,?) ON CONFLICT DO NOTHING",
            [(resource, identifier, revision) for identifier, revision in pins],
        )


def raw_allowed(store, request, resource, *, dependencies_only=False):
    """Raw archives are for their source conversation. Cross-context use goes through reviewed fragments."""
    if getattr(store, "backend", None) != "postgres":
        return True
    from .access import GraphLimits

    # A fact with hundreds of revisions can have one direct source and hundreds of ancestors.
    # Stream the full closure up to the governance graph budget, under one cursor snapshot.
    limit = GraphLimits().nodes
    skip = resource if dependencies_only else None
    with store.db() as db, db.stream(
        "WITH RECURSIVE deps(k) AS (VALUES (?::text) UNION SELECT d.source FROM "
        "source_dependencies d JOIN deps ON d.derived=deps.k) "
        "SELECT k,p.resource,p.conversation,p.audience,jsonb_exists(p.owners::jsonb, ?) AS owned, "
        "ARRAY(SELECT ARRAY[fragment,revision] FROM privacy_dependencies WHERE derived=k) AS pins "
        "FROM deps LEFT JOIN governance_policies p ON p.resource=k LIMIT ?",
        (resource, request.actor, limit + 1),
    ) as cursor:
        checked = 0
        while rows := cursor.fetchmany(256):
            checked += len(rows)
            if checked > limit or not _raw_batch_allowed(store, db, request, rows, skip):
                return False
    return True


def _raw_batch_allowed(store, db, request, rows, skip):
    deps = {tuple(pin) for row in rows for pin in row["pins"]}
    if deps:
        anchors = db.execute(
            "SELECT id FROM core_events WHERE deleted=0 AND conversation=? AND actor=? "
            "ORDER BY occurred DESC LIMIT 1",
            (request.conversation, request.actor or ""),
        ).fetchone()
        if not anchors:
            return False
        from . import events

        event = Event.from_dict(events.get(store, anchors[0])["event"])
        try:
            check_projection(store, event, deps)
        except AssistantError:
            return False
    for row in rows:
        key = row["k"]
        if key == skip:
            continue
        if (
            key.split("/", 1)[0] not in KINDS
            or key.startswith("partition/")
            and key != "partition/public-growth"
        ):
            continue
        if row["resource"] is None:
            return False
        if key == "partition/public-growth":
            saved = store.state("public-growth", {}, db)
            if saved.get("data") == store.profile.defaults["growth"]:
                # Shipped constants carry no user text. Other roots are still checked.
                continue
        if request.audience == "private" and row["owned"] and not row["conversation"]:
            continue
        if (
            row["conversation"]
            and row["conversation"] == request.conversation
            and request.audience == row["audience"]
        ):
            if request.audience == "private":
                continue
            if (
                request.audience == "group"
                and not db.execute(
                    "SELECT 1 FROM privacy_slices WHERE resource=? AND "
                    "level IN ('credential','private') LIMIT 1",
                    (key,),
                ).fetchone()
            ):
                continue
        # Even a published record can contain real names or exact addresses.
        job = db.execute("SELECT complete FROM privacy_jobs WHERE resource=?", (key,)).fetchone()
        if not job or not job[0]:
            return False
        slices = db.execute("SELECT * FROM privacy_slices WHERE resource=?", (key,)).fetchall()
        if not slices or any(
            r["category"] != "ordinary"
            or level(store, r, unpack(store, r)) != "social"
            or not live_source(store, r, unpack(store, r))
            for r in slices
        ):
            return False
    return True


def require_raw(store, request, resources):
    if any(not raw_allowed(store, request, key) for key in resources):
        raise AssistantError("context_changed", "资料适用范围已经改变")
