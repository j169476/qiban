"""Administrator-only sharing scope for ordinary records.

A share page allows an explicit administrative widening or narrowing of one
record's usable range, and it reuses the existing governance rules instead of
adding a second authorization system:

* the read decision stays ``AccessService`` (``RecordPolicy`` + ``AccessGrant``);
* a private-chat grant still goes through ``AdminService.authorize``, so the
  granted purposes are only ``reply``/``maintenance`` and the lifetime stays
  inside the core 365-day bound;
* a classification change reuses ``GovernanceRepository.set_policy`` with
  ``dataclasses.replace``, so ``owners``/``sources``/``source_versions``/
  ``valid``/``expires_at``/``conversation``/``audience`` keep their exact stored
  values. ``AdminService.classify`` is deliberately not used: it re-pins sources
  and defaults ``valid=True``/``expires_at=None``, which would launder a revoked
  or expired source.

Nothing here rewrites record content, personal-fact history, relationship
adjudication or the shared-baseline material: persona and foundation memories
belong to the dedicated baseline flow and are refused here, and a personal fact
stays in the fact timeline. A broken/revoked/expired/rotated source still denies
the record after sharing, and every preview reports that honestly instead of
claiming that sharing bypasses a source.

Every widening is preview -> commit. The preview returns a server-side,
short-lived token bound to the management session, the content and permission
revisions, the dependency fingerprint, the exact grant state, the covered scope
of a revoked grant and the source report. The commit re-reads all of it inside
the caller's transaction and refuses a stale token.

Bounds are deliberate rather than cosmetic: a listing page always advances even
when every scanned row is excluded, grants are counted with SQL aggregates so a
long history of expired/revoked grants can neither hide a current grant nor be
mistaken for the whole set, ``sharing.grants`` pages through every current grant
by id cursor (``detail`` reports ``grants_next``), a grant is only revocable when
its entire resource list and source/dependant closure fit on the preview, and
pending previews are pruned and capped.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import replace

from . import source_report
from .access import AccessGrant, AccessService
from .contracts import ActorRef
from .errors import AssistantError
from .provenance import reserved
from .record_inspection import impact

# The same manageable kinds as the existing advanced record import; ``persona``
# is a configuration file and is never a share target.
KINDS = ("diaries", "knowledge", "memories")
KIND_LABELS = {"diaries": "日记", "knowledge": "知识", "memories": "记忆"}
PAGE_MAX = 50
# Rows examined per listing page (a page limit of 50 plus the excluded ones).
SCAN_MAX = 400
# Active grants shown per page, resources shown for one revocation, and the whole
# graph a revocation preview agrees to inspect (the same bound ``impact`` uses).
GRANT_ROWS = 50
GRANT_RESOURCES = 100
SCOPE_ROWS = 10_000
MAX_SOURCES_SHOWN = 30
MAX_DEPENDENTS_SHOWN = 50
# Exactly the core grant bounds (``AdminService.authorize``).
PURPOSES = ("reply", "maintenance")
PURPOSE_LABELS = {"reply": "回复", "maintenance": "维护"}
DEFAULT_DAYS = 30
MAX_DAYS = 365
PREVIEW_SECONDS = 600
MAX_PREVIEWS = 64
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
GRANT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Display order only. Classification is not a total order: a change between two
# of these values can add one audience and remove another, so every direction
# label is computed from real check answers instead of these positions.
CLASSIFICATIONS = ("public", "conversation", "personal", "restricted", "secret", "unclassified")
CLASSIFICATION_LABELS = {
    "public": "公开（任何会话都可使用）",
    "conversation": "仅原会话（按原会话与可见范围）",
    "personal": "仅归属账号私聊",
    "restricted": "仅显式授权（默认无人可读）",
    "secret": "保密（任何模型上下文都不使用）",
    "unclassified": "未分类（隔离，需先确认分类）",
}
AUDIENCE_LABELS = {"private": "私聊", "group": "群聊", "public": "公开"}
# Request shapes the effect check actually issues. The names are what the page
# renders, so they stay stable.
SHAPE_LABELS = {
    "private_reply": "所选账号私聊",
    "conversation": "原会话",
    "public_read": "公开场合",
}
MEANING = (
    "这里只调整一条资料可以被谁使用，以及是否允许某个已登记账号在私聊中使用。"
    "它不是“允许回复该联系人”：不会授权任何会话、不启动平台、不改变回复额度。"
    "资料的正文、归属账号、真实来源与版本、有效状态和到期时间保持原样；"
    "来源被停用、过期或版本改变后，即使共享也不会生效。"
)


def _fail(code, message):
    raise AssistantError(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def actor_label(value):
    data = value if isinstance(value, dict) else value.as_dict()
    platform = {"qq": "QQ", "wechat": "微信", "bilibili": "B站",
                "local": "本机"}.get(data["platform"], data["platform"])
    scope = ("（接入账号：" + data["scope"] + "）") if data.get("scope") else ""
    return platform + " · " + data["user_id"] + scope


def _policy_view(policy):
    """Metadata only: never the record body."""
    pins = list(policy.source_versions)
    parsed = dict(policy.source_versions)
    return {
        "classification": policy.classification,
        "classification_label": CLASSIFICATION_LABELS[policy.classification],
        "owners": sorted(policy.owners),
        "owner_count": len(policy.owners),
        "conversation": policy.conversation,
        "audience": policy.audience,
        "audience_label": AUDIENCE_LABELS.get(policy.audience, policy.audience),
        "valid": bool(policy.valid),
        "expires_at": policy.expires_at,
        "source_count": len(policy.sources),
        "sources": list(policy.sources[:MAX_SOURCES_SHOWN]),
        "source_versions": [list(pin) for pin in pins[:MAX_SOURCES_SHOWN]],
        "sources_limited": len(policy.sources) > MAX_SOURCES_SHOWN,
        "pins_complete": len(parsed) == len(policy.source_versions) and set(parsed) == set(policy.sources),
        "version": policy.version,
    }


def _policy_state(policy):
    """Everything a preview promises to keep: used for the commit re-check."""
    return source_report.policy_state(policy)


def _grant_view(row, now, epoch):
    purposes = sorted(json.loads(row["purposes"]))
    current = row["epoch"] == epoch
    expired = row["expires_at"] <= now
    return {"id": row["id"], "grantee": row["grantee"], "owner": row["owner"],
            "resource_count": len(json.loads(row["resources"])),
            "resources": sorted(json.loads(row["resources"]))[:MAX_SOURCES_SHOWN],
            "purposes": purposes,
            "purpose_labels": [PURPOSE_LABELS.get(value, value) for value in purposes],
            "basis": row["basis"], "expires_at": row["expires_at"],
            "active": (not row["revoked"]) and current and not expired,
            "revoked": bool(row["revoked"]), "expired": expired,
            "epoch_current": current}


def _grant_state(row):
    """Everything that identifies one grant, so a revocation cannot hit another."""
    return {"id": row["id"], "grantee": row["grantee"], "owner": row["owner"],
            "resources": sorted(json.loads(row["resources"])),
            "purposes": sorted(json.loads(row["purposes"])), "basis": row["basis"],
            "evidence_id": row["evidence_id"], "expires_at": row["expires_at"],
            "epoch": row["epoch"], "revoked": bool(row["revoked"])}


class _PreviewRepository:
    """Read-only view with one hypothetical policy or grant, for a real check."""

    def __init__(self, repository, *, policy=None, grant=None, drop=None, neutral=False):
        self.repository, self.policy_override = repository, policy
        self.grant, self.drop = grant, drop
        # ``neutral`` drops source pins, validity/expiry and every grant so the
        # answer describes the classification's own audience only. That is what
        # separates the direction of a classification change from the state of
        # its sources; it is never used to decide access.
        self.neutral = neutral

    def policy(self, resource):
        policy = (self.policy_override
                  if self.policy_override is not None and resource == self.policy_override.resource
                  else self.repository.policy(resource))
        if policy is None or not self.neutral:
            return policy
        # Secrets stay secrets: neutralising means "ignore the source report",
        # never "guess a different classification".
        return replace(policy, sources=(), source_versions=(), valid=True, expires_at=None)

    def grants(self, grantee):
        if self.neutral:
            return ()
        rows = tuple(grant for grant in self.repository.grants(grantee)
                     if self.drop is None or grant.id != self.drop)
        if self.grant is not None and self.grant.grantee == grantee:
            rows = (*rows, self.grant)
        return rows

    def provenance_target(self, resource):
        return self.repository.provenance_target(resource)


class RecordSharing:
    """Preview/commit sharing operations bound to one management instance."""

    def __init__(self, admin, actor=None, conversation=None):
        self.admin = admin
        self.store = admin.store
        self.repository = admin.repository
        self.authority = admin.authority
        self.access = admin.access
        # The current administrator's own sealed identity. It is used for the
        # effect check only when it really owns the record; it is never offered
        # as a grant target.
        self.actor = actor
        # The exact conversation the desktop management entry point issues its
        # sealed request with. A merely signed administrator request from another
        # entry point is not this page.
        self.conversation = conversation
        # Restarting the app builds a new instance, so every token dies with it.
        self.previews = {}
        self._foundations = None

    # --- read-only helpers ---------------------------------------------------

    def _require(self, request):
        self.authority.require(request, administrator=True)
        # The seal proves an administrator, not which surface asked. Binding the
        # desktop conversation keeps this page from being driven by any other
        # sealed administrator context.
        if self.conversation is not None and request.conversation != self.conversation:
            _fail("access_denied", "请求身份、授权或管理会话已失效")
        if getattr(self.store, "backend", None) != "postgres":
            _fail("sharing_backend", "资料共享设置需要事务化 PostgreSQL 核心")

    def _cursor(self, after):
        if not isinstance(after, str) or len(after) > 300:
            _fail("sharing_query", "分页位置无效，请重新读取。")
        if not after:
            return "", ("", "")
        kind, _, identifier = after.partition("/")
        if kind not in KINDS or not IDENTIFIER.fullmatch(identifier) or after != kind + "/" + identifier:
            _fail("sharing_query", "分页位置无效，请重新读取。")
        return after, (kind, identifier)

    def _resource(self, kind, identifier):
        if not isinstance(kind, str) or kind not in KINDS:
            _fail("sharing_record", "共享设置仅支持普通记忆、知识和日记；共用人物设定请用原来的登记流程。")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            _fail("sharing_record", "资料标识格式无效，请重新选择。")
        return kind + "/" + identifier

    def _foundation(self, kind, identifier, card=None):
        if kind != "memories":
            return False
        card = card if card is not None else self.store.read("persona")
        cached = self._foundations
        if cached is not None and cached[0] == card["version"]:
            return identifier in cached[1]
        from . import baseline

        self._foundations = (card["version"], frozenset(baseline.identifiers(card["data"])))
        return identifier in self._foundations[1]

    def _record(self, kind, identifier):
        """The manageable record and its current policy, or a precise refusal."""
        resource = self._resource(kind, identifier)
        if reserved(resource):
            _fail("sharing_record", "历史来源不能单独编辑或授权。")
        try:
            item = self.store.read(kind, identifier)
        except AssistantError as error:
            if error.code in ("not_found", "invalid_path", "missing_id", "invalid_id", "id_mismatch"):
                _fail("sharing_record", "找不到这条资料，请重新选择。")
            raise
        if kind == "memories" and item["data"].get("fact"):
            _fail("sharing_record", "个人事实请在事实时间线中处理，不能在这里改变共享范围。")
        if self._foundation(kind, identifier):
            _fail("sharing_record", "共用基础设定仍在原来的登记流程中管理，从此页修改会破坏共用设定。")
        policy = self.repository.policy(resource)
        if policy is None:
            _fail("sharing_record", "当前资料的来源与权限记录缺失，请先检查恢复。")
        return item, policy

    # --- grant reads: exact totals, bounded rows ----------------------------

    def _grant_totals(self, db, resource, now, epoch):
        """One aggregate: an old expired/revoked grant never hides a current one."""
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN g.revoked=0 AND g.expires_at>? AND g.epoch=? THEN 1 ELSE 0 END),0) AS active, "
            "COALESCE(SUM(CASE WHEN g.revoked<>0 THEN 1 ELSE 0 END),0) AS revoked, "
            "COALESCE(SUM(CASE WHEN g.revoked=0 AND g.expires_at>? AND g.epoch<>? THEN 1 ELSE 0 END),0) AS stale "
            "FROM grant_resources gr JOIN governance_grants g ON g.id=gr.grant_id WHERE gr.resource=?",
            (now, epoch, now, epoch, resource)).fetchone()
        return {"total": row["total"], "active": row["active"], "revoked": row["revoked"],
                "stale_epoch": row["stale"]}

    def _active_grants(self, db, resource, now, epoch, after=""):
        """One bounded page of currently usable grants, ordered and paged by id."""
        rows = db.execute(
            "SELECT g.* FROM grant_resources gr JOIN governance_grants g ON g.id=gr.grant_id "
            "WHERE gr.resource=? AND g.revoked=0 AND g.expires_at>? AND g.epoch=? AND g.id>? "
            "ORDER BY g.id LIMIT ?",
            (resource, now, epoch, after, GRANT_ROWS + 1)).fetchall()
        return rows[:GRANT_ROWS], (rows[GRANT_ROWS - 1]["id"] if len(rows) > GRANT_ROWS else None)

    def _grants_digest(self, db, resource, now, epoch):
        """Exact fingerprint of every grant row for this resource, not the first N."""
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN g.revoked=0 AND g.expires_at>? AND g.epoch=? THEN 1 ELSE 0 END),0) AS active, "
            "md5(COALESCE(string_agg(g.id||'|'||g.grantee||'|'||g.owner||'|'||g.resources||'|'||g.purposes||'|'||"
            "g.basis||'|'||g.evidence_id||'|'||g.expires_at||'|'||g.epoch||'|'||g.revoked, ',' ORDER BY g.id),'')) "
            "AS state "
            "FROM grant_resources gr JOIN governance_grants g ON g.id=gr.grant_id WHERE gr.resource=?",
            (now, epoch, resource)).fetchone()
        return _digest([row["total"], row["active"], row["state"]])

    def _grant_row(self, db, grant_id, *, lock=False):
        return db.execute("SELECT * FROM governance_grants WHERE id=?" + (" FOR UPDATE" if lock else ""),
                          (grant_id,)).fetchone()

    # --- source report -------------------------------------------------------

    def _report(self, resource):
        """Bounded, read-only walk of the whole source graph.

        The walk itself lives in ``source_report`` so the claim-adjudication page
        can bind the same closure state; it reports a cycle, an over-long chain
        or a truncated walk instead of silently accepting it.
        """
        return source_report.closure(self.repository, self.access.limits, resource)["report"]

    def _blockers(self, policy, report):
        """Human-readable reasons why sharing cannot take effect right now."""
        text = []
        if policy.classification in ("secret", "unclassified"):
            text.append("分类是" + CLASSIFICATION_LABELS[policy.classification] + "，任何回复都不会使用它；"
                        "请先把分类调整为可用范围。")
        if not policy.valid:
            text.append("资料已被停用，共享不会让它恢复。")
        if policy.expires_at is not None and policy.expires_at <= self.repository.clock():
            text.append("资料已经过期，共享不会延长它的有效期。")
        if not _policy_view(policy)["pins_complete"]:
            text.append("来源版本记录不完整，请先检查恢复。")
        # The record's own revoked/expired/quarantined state has its own line
        # above; a cycle, a depth overrun or a truncated walk never does.
        sources = sorted({issue["detail"] for issue in report["issues"]
                          if issue["resource"] != policy.resource
                          or issue["reason"] in ("cycle", "depth", "budget")})
        if sources:
            text.append("来源受限：" + "；".join(sources) + "。共享不会绕过这些来源限制。")
        if report["limited"]:
            text.append("阻断项超过 200 条，只显示前 200 条。")
        return text

    # --- effect checks -------------------------------------------------------

    def _target_actor(self, grantee):
        """Only a registered platform account can be a grant target."""
        from .person_baseline import actor_by_key

        return actor_by_key(self.store, grantee)

    def _label(self, key):
        try:
            return actor_label(self._target_actor(key))
        except AssistantError:
            return "未登记账号（" + key[:8] + "…）"

    def _registered_actor(self, key):
        """Any registered identity node, whatever its platform.

        Owner eligibility and grantee eligibility are different questions: the
        effect check asks what this record's own owners can read, so a
        local-room owner must not be rejected for not being a login account.
        Nothing is invented when the key has no registered node.
        """
        with self.store.db() as db:
            row = db.execute("SELECT identity_body FROM actor_nodes WHERE actor_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            actor = ActorRef(**self.store.ledger_decode("actor_nodes.identity", row["identity_body"]))
        except (AssistantError, KeyError, TypeError):
            return None
        return actor if actor.key == key else None

    def _owner_actor(self, policy):
        """One owner identity that really exists, or None for a genuine ownerless record."""
        for key in sorted(policy.owners):
            if self.actor is not None and key == self.actor.key:
                return self.actor
            actor = self._registered_actor(key)
            if actor is not None:
                return actor
        return None

    def _shapes(self, policy, purposes, actor):
        """Every concrete request shape worth checking, mirrored from ``_direct``."""
        shapes = []
        for purpose in purposes:
            if actor is not None:
                shapes.append(("private_reply", "private", (actor,), "sharing-private-reply", purpose))
                if policy.conversation:
                    shapes.append(("conversation", policy.audience,
                                   (actor,) if policy.audience == "private" else (),
                                   policy.conversation, purpose))
            elif policy.audience == "public" and policy.conversation:
                shapes.append(("conversation", "public", (), policy.conversation, purpose))
            shapes.append(("public_read", "public", (), "sharing-public", purpose))
        return shapes

    def _effect(self, repository, resource, policy, purposes, actor):
        """Ask the real ``AccessService`` what each concrete request shape reads.

        ``purposes`` is exactly the set at stake: a maintenance-only grant is
        never described as a failed reply grant. The stored audience is checked
        as its own shape so the original conversation is not silently dropped.
        """
        reader = AccessService(repository, self.authority, limits=self.access.limits)
        result = []
        for name, audience, recipients, conversation, purpose in self._shapes(policy, purposes, actor):
            request = self.authority.issue(actor=actor, conversation=conversation, audience=audience,
                                           recipients=recipients, purpose=purpose, lifetime=60)
            try:
                allowed = reader.can_read(request, resource)
            except AssistantError as error:
                if error.code != "access_denied":
                    raise
                allowed = False
            result.append({"shape": name, "label": SHAPE_LABELS[name], "audience": audience,
                           "audience_label": AUDIENCE_LABELS.get(audience, audience),
                           "purpose": purpose, "purpose_label": PURPOSE_LABELS[purpose],
                           "allowed": allowed})
        return result

    def _effect_note(self, policy, actor):
        if actor is not None:
            return ""
        if policy.owners:
            return ("这条资料的归属账号没有可核对的登记身份，因此只核对了不依赖身份的公开范围；"
                    "没有为它构造任何账号身份。")
        return "这条资料没有登记归属账号，因此只核对了不依赖身份的公开范围。"

    def _will_apply(self, checks):
        return any(check["allowed"] for check in checks)

    def _grant_effective(self, checks, purposes):
        """A grant is only effective when the granted purpose is readable in private."""
        return any(check["allowed"] and check["shape"] == "private_reply" and check["purpose"] in purposes
                   for check in checks)

    def _direction(self, before, after):
        """Compare real check answers; classification is not a total order."""
        def allowed(checks):
            return {(check["shape"], check["purpose"]): check["allowed"] for check in checks}

        old, new = allowed(before), allowed(after)
        widened = [(shape, purpose) for (shape, purpose), value in new.items()
                   if value and not old.get((shape, purpose), False)]
        narrowed = [(shape, purpose) for (shape, purpose), value in old.items()
                    if value and not new.get((shape, purpose), False)]
        if widened and narrowed:
            label = "同时扩大与收窄"
        elif widened:
            label = "扩大"
        elif narrowed:
            label = "收窄"
        else:
            label = "没有改变可读范围"

        def names(pairs):
            return sorted({SHAPE_LABELS[shape] + " · " + PURPOSE_LABELS[purpose] for shape, purpose in pairs})

        return {"label": label, "widened": names(widened), "narrowed": names(narrowed),
                "basis": "按分类语义核对（来源、有效状态与已有授权另行核对）"}

    # --- paged reads ---------------------------------------------------------

    def listing(self, request, after, limit):
        self._require(request)
        cursor, keys = self._cursor(after)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            _fail("sharing_query", f"每次只能查看 1 至 {PAGE_MAX} 条资料。")
        scan = min(limit * 3 + 3, SCAN_MAX)
        now, epoch = self.repository.clock(), self.repository.epoch()
        with self.store.db() as db:
            # One sentinel row beyond the scan window, so "we scanned everything
            # we fetched" is never confused with "the table is exhausted".
            rows = db.execute(
                "SELECT r.kind AS kind, r.id AS rid, r.version AS content_version, f.id AS fact_id, p.* "
                "FROM content_records r "
                "LEFT JOIN governance_policies p ON p.resource = r.kind || '/' || r.id "
                "LEFT JOIN personal_fact_index f ON f.kind = r.kind AND f.id = r.id "
                "WHERE r.kind = ANY(?) AND (r.kind, r.id) > (?, ?) ORDER BY r.kind, r.id LIMIT ?",
                (list(KINDS), keys[0], keys[1], scan + 1)).fetchall()
            items, examined, following = [], 0, None
            card = None
            for row in rows[:scan]:
                if len(items) >= limit:
                    break
                resource = row["kind"] + "/" + row["rid"]
                examined, following = examined + 1, resource
                policy = None if row["resource"] is None else self.repository._policy(row)
                fact = row["fact_id"] is not None
                if row["kind"] == "memories" and card is None:
                    card = self.store.read("persona")
                foundation = self._foundation(row["kind"], row["rid"], card)
                item = {"kind": row["kind"], "identifier": row["rid"],
                        "label": KIND_LABELS[row["kind"]] + " · " + row["rid"], "cursor": resource,
                        "version": row["content_version"],
                        "classification": policy.classification if policy else None,
                        "classification_label": (CLASSIFICATION_LABELS[policy.classification]
                                                 if policy else "权限记录缺失"),
                        "owners": sorted(policy.owners) if policy else [],
                        "audience": policy.audience if policy else None,
                        "valid": bool(policy.valid) if policy else None,
                        "expires_at": policy.expires_at if policy else None,
                        "source_count": len(policy.sources) if policy else 0,
                        "policy_version": policy.version if policy else None,
                        "usable": policy is not None, "fact": fact, "foundation": foundation}
                item["manageable"] = bool(item["usable"] and not fact and not foundation)
                if item["manageable"]:
                    items.append(item)
            active = {}
            if items:
                active = {row["resource"]: row["count"] for row in db.execute(
                    "SELECT gr.resource AS resource, COUNT(*) AS count FROM grant_resources gr "
                    "JOIN governance_grants g ON g.id=gr.grant_id WHERE gr.resource=ANY(?) "
                    "AND g.revoked=0 AND g.expires_at>? AND g.epoch=? GROUP BY gr.resource",
                    ([item["cursor"] for item in items], now, epoch)).fetchall()}
            for item in items:
                item["grants"] = active.get(item["cursor"], 0)
        return {"records": items, "count": len(items), "limit": limit, "offset": cursor,
                "scanned": examined, "next": following if len(rows) > examined else None,
                "page_max": PAGE_MAX, "kinds": list(KINDS),
                "kind_labels": dict(KIND_LABELS), "meaning": MEANING}

    def detail(self, request, kind, identifier):
        self._require(request)
        now, epoch = self.repository.clock(), self.repository.epoch()
        with self.store.db() as db:
            item, policy = self._record(kind, identifier)
            rows, following = self._active_grants(db, policy.resource, now, epoch)
            totals = self._grant_totals(db, policy.resource, now, epoch)
            report = self._report(policy.resource)
            affected = impact(self.store, policy.resource)
        grants = [{**_grant_view(row, now, epoch), "target": self._label(row["grantee"])} for row in rows]
        return {"kind": kind, "identifier": identifier, "resource": policy.resource,
                "record_version": item["version"], "locked": bool(item["data"].get("locked")),
                "policy": _policy_view(policy), "owner_options": sorted(policy.owners),
                "grants": grants, "grants_limited": following is not None,
                "grants_next": following, "grants_limit": GRANT_ROWS,
                "grants_active": totals["active"], "grants_total": totals["total"],
                "grants_revoked": totals["revoked"], "grants_stale_epoch": totals["stale_epoch"],
                "source": report, "blockers": self._blockers(policy, report),
                "impact": affected,
                "classifications": [{"value": value, "label": CLASSIFICATION_LABELS[value]}
                                    for value in CLASSIFICATIONS],
                "purposes": [{"value": value, "label": PURPOSE_LABELS[value]} for value in PURPOSES],
                "days": {"default": DEFAULT_DAYS, "max": MAX_DAYS}, "meaning": MEANING}

    def grants(self, request, kind, identifier, after):
        """Bounded page over the currently usable grants of one record.

        Only current-epoch, unrevoked, unexpired grants are listed, so a long
        history can never hide a grant that still works; the exact totals stay
        available in ``counts`` and the commit re-check keeps using them.
        """
        self._require(request)
        if not isinstance(after, str) or len(after) > 64 or (after and not GRANT_ID.fullmatch(after)):
            _fail("sharing_query", "授权分页位置无效，请重新读取。")
        now, epoch = self.repository.clock(), self.repository.epoch()
        with self.store.db() as db:
            item, policy = self._record(kind, identifier)
            rows, following = self._active_grants(db, policy.resource, now, epoch, after)
            totals = self._grant_totals(db, policy.resource, now, epoch)
        return {"kind": kind, "identifier": identifier, "resource": policy.resource,
                "grants": [{**_grant_view(row, now, epoch), "target": self._label(row["grantee"])}
                           for row in rows],
                "next": following, "count": len(rows), "after": after, "limit": GRANT_ROWS,
                "counts": totals,
                "meaning": ("这里只列出当前有效的私聊授权；已撤销、已过期或属于早先权限代次的授权不会出现，"
                            "但会计入总数为撤销判断保留依据。")}

    def targets(self, request, after):
        """Only accounts the durable registration already proves."""
        self._require(request)
        from .person_baseline import accounts

        if not isinstance(after, str) or len(after) > 256:
            _fail("sharing_query", "账号分页位置无效，请重新读取。")
        page = accounts(self.store, after)
        return {"targets": [{**row, "label": actor_label(row)} for row in page["accounts"]],
                "next": page["next"],
                "meaning": "这里只列出已经登记过的账号；没有登记过的账号无法在这里被选中。"}

    # --- preview tokens ------------------------------------------------------

    def _prune(self):
        now = time.monotonic()
        for token in [token for token, pending in self.previews.items() if pending["expires"] <= now]:
            self.previews.pop(token, None)

    def _remember(self, state):
        """Bound the pending set: stale previews first, then the soonest to expire."""
        self._prune()
        while len(self.previews) >= MAX_PREVIEWS:
            oldest = min(self.previews, key=lambda token: self.previews[token]["expires"])
            self.previews.pop(oldest, None)
        token = secrets.token_urlsafe(24)
        self.previews[token] = state
        return token

    def _state(self, request, *, action, kind, identifier, item, policy, payload, db, report, affected,
               scope=None):
        now, epoch = self.repository.clock(), self.repository.epoch()
        return {"session": request.session_id, "expires": time.monotonic() + PREVIEW_SECONDS,
                "action": action, "kind": kind, "identifier": identifier,
                "resource": policy.resource, "record_version": item["version"],
                "policy_version": policy.version,
                "policy": _digest(_policy_state(policy)),
                "grants": self._grants_digest(db, policy.resource, now, epoch),
                "impact": affected["fingerprint"], "sources": _digest(report),
                "scope": scope, "payload": payload}, now

    # --- previews ------------------------------------------------------------

    def preview_grant(self, request, kind, identifier, grantee, owner, purposes, days):
        self._require(request)
        item, policy = self._record(kind, identifier)
        if (not isinstance(grantee, str) or not grantee or len(grantee) > 256
                or not isinstance(owner, str) or not owner or len(owner) > 256):
            _fail("sharing_grant", "请选择已登记的目标账号和资料归属账号。")
        if (not isinstance(purposes, list) or not purposes
                or any(not isinstance(value, str) or value not in PURPOSES for value in purposes)
                or len(set(purposes)) != len(purposes)):
            _fail("sharing_grant", "授权用途只能选择“回复”和“维护”。")
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            _fail("sharing_grant", f"有效期必须是 1 至 {MAX_DAYS} 天的整数。")
        if reserved(policy.resource):
            _fail("sharing_grant", "历史来源不能单独授予读取权限。")
        if policy.classification in ("secret", "unclassified"):
            _fail("sharing_grant", "保密或未分类的资料不能授权；请先确认分类。")
        if owner not in policy.owners:
            _fail("sharing_grant", "归属账号必须来自这条资料当前登记的归属清单。")
        actor = self._target_actor(grantee)
        chosen = tuple(sorted(purposes))
        with self.store.db() as db:
            report, affected = self._report(policy.resource), impact(self.store, policy.resource)
            state, now = self._state(request, action="grant", kind=kind, identifier=identifier,
                                     item=item, policy=policy,
                                     payload={"grantee": grantee, "owner": owner,
                                              "purposes": list(chosen), "days": days},
                                     db=db, report=report, affected=affected)
            totals = self._grant_totals(db, policy.resource, now, self.repository.epoch())
            grant = AccessGrant(id="preview", grantee=grantee, owner=owner,
                                resources=frozenset((policy.resource,)), purposes=frozenset(chosen),
                                basis="owner_admin", evidence_id=request.session_id or "trusted-admin",
                                expires_at=now + days * 86400, epoch=self.repository.epoch())
            before = self._effect(self.repository, policy.resource, policy, chosen, actor)
            after = self._effect(_PreviewRepository(self.repository, grant=grant),
                                 policy.resource, policy, chosen, actor)
        token = self._remember(state)
        blocked = self._blockers(policy, report)
        applied = self._grant_effective(after, chosen)
        return {"token": token, "action": "grant", "resource": policy.resource,
                "kind": kind, "identifier": identifier,
                "before": {"grants": totals["active"], "grants_total": totals["total"],
                           "classification_label": CLASSIFICATION_LABELS[policy.classification],
                           "scope": _policy_view(policy)},
                "after": {"grants": totals["active"] + 1,
                          "grant": {"grantee": grantee, "owner": owner, "purposes": list(chosen),
                                    "purpose_labels": [PURPOSE_LABELS[value] for value in chosen],
                                    "days": days, "expires_at": now + days * 86400,
                                    "target": actor_label(actor)}},
                "source": report, "blockers": blocked, "impact": affected,
                "effect": after, "effect_before": before, "effect_actor": actor_label(actor),
                "effect_note": self._effect_note(policy, actor), "effect_basis": "实际核对（含来源与授权）",
                "configured_purposes": list(chosen), "will_apply": applied,
                "notice": ("确认后只新增一条私聊授权：该账号在私聊中使用所选用途时可以读取这条资料；"
                           "不会授权任何会话、不启动平台、不改变回复额度。"
                           + ("" if not blocked else " 请注意：现在共享仍不能生效。")
                           + ("" if applied or blocked else " 请注意：按当前状态这条资料在私聊里仍不可读。")),
                "written": False}

    def preview_classify(self, request, kind, identifier, classification):
        self._require(request)
        item, policy = self._record(kind, identifier)
        if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
            _fail("sharing_classify", "请选择现有的资料分类。")
        if classification == policy.classification:
            _fail("sharing_classify", "新分类与当前分类相同，没有需要提交的更改。")
        updated = replace(policy, version=secrets.token_hex(16), classification=classification)
        with self.store.db() as db:
            report, affected = self._report(policy.resource), impact(self.store, policy.resource)
            state, _ = self._state(request, action="classify", kind=kind, identifier=identifier,
                                   item=item, policy=policy,
                                   payload={"classification": classification},
                                   db=db, report=report, affected=affected)
            owner = self._owner_actor(updated)
            shapes = PURPOSES
            after = self._effect(_PreviewRepository(self.repository, policy=updated),
                                 policy.resource, updated, shapes, owner)
            # Direction ignores sources/validity/grants on purpose: it describes
            # the classification's own audience, which is not a total order.
            semantic_before = self._effect(
                _PreviewRepository(self.repository, policy=policy, neutral=True),
                policy.resource, policy, shapes, owner)
            semantic_after = self._effect(
                _PreviewRepository(self.repository, policy=updated, neutral=True),
                policy.resource, updated, shapes, owner)
        blocked = self._blockers(updated, report)
        direction = self._direction(semantic_before, semantic_after)
        applied = self._will_apply(after)
        token = self._remember(state)
        return {"token": token, "action": "classify", "resource": policy.resource,
                "kind": kind, "identifier": identifier,
                "direction": direction["label"], "direction_detail": direction,
                "target_basis": direction["basis"],
                "before": _policy_view(policy), "after": _policy_view(updated),
                "source": report, "blockers": blocked, "impact": affected,
                "effect": after, "effect_before": semantic_before,
                "effect_actor": None if owner is None else actor_label(owner),
                "effect_note": self._effect_note(updated, owner),
                "effect_basis": "实际核对（含来源与授权）", "configured_purposes": list(PURPOSES),
                "will_apply": applied,
                "owner_checked": None if owner is None else actor_label(owner),
                "notice": ("确认后只改变这条资料的分类；归属账号、真实来源与版本、有效状态和到期时间保持原样。"
                           + ("" if not blocked else " 请注意：现在共享仍不能生效。")),
                "written": False}

    def preview_revoke(self, request, kind, identifier, grant_id):
        self._require(request)
        item, policy = self._record(kind, identifier)
        if not isinstance(grant_id, str) or not GRANT_ID.fullmatch(grant_id):
            _fail("sharing_revoke", "请选择要撤销的授权。")
        now, epoch = self.repository.clock(), self.repository.epoch()
        with self.store.db() as db:
            row = self._grant_row(db, grant_id)
            if row is None or policy.resource not in json.loads(row["resources"]):
                _fail("sharing_revoke", "该授权不属于这条资料，无法在这里撤销。")
            if row["revoked"]:
                _fail("sharing_revoke", "这条授权已经被撤销，无需再次操作。")
            resources = sorted(json.loads(row["resources"]))
            # Revocation keeps the core meaning (the whole grant is revoked), so
            # every covered record must be shown before it is committed.
            if len(resources) > GRANT_RESOURCES:
                _fail("sharing_revoke",
                      f"这条授权覆盖 {len(resources)} 条资料，超过本页可完整核对的 {GRANT_RESOURCES} 条上限；"
                      "为避免撤销未展示的资料，请先在原授权流程中处理。")
            scope = self._scope(db, resources, grantee=row["grantee"], grant_id=grant_id, now=now, epoch=epoch)
            report, affected = self._report(policy.resource), impact(self.store, policy.resource)
            state, _ = self._state(request, action="revoke", kind=kind, identifier=identifier,
                                   item=item, policy=policy,
                                   payload={"grant_id": grant_id, "grant": _grant_state(row),
                                            "resources": resources, "scope": scope["digest"]},
                                   db=db, report=report, affected=affected,
                                   scope=scope["digest"])
            totals = self._grant_totals(db, policy.resource, now, epoch)
            try:
                actor = self._target_actor(row["grantee"])
            except AssistantError:
                actor = None
            purposes = tuple(sorted(json.loads(row["purposes"])))
            before = (self._effect(self.repository, policy.resource, policy, purposes, actor)
                      if actor is not None else [])
            after = (self._effect(_PreviewRepository(self.repository, drop=grant_id),
                                  policy.resource, policy, purposes, actor)
                     if actor is not None else [])
        remaining = [check for check in after if check["allowed"]]
        token = self._remember(state)
        return {"token": token, "action": "revoke", "resource": policy.resource,
                "kind": kind, "identifier": identifier,
                "before": {"grants": totals["active"],
                           "grant": {**_grant_view(row, now, epoch),
                                     "target": self._label(row["grantee"])}},
                "after": {"grants": max(0, totals["active"] - 1)},
                "scope": scope["view"], "scope_count": len(resources),
                "scope_dependents": scope["dependents"], "scope_dependents_count": scope["dependents_count"],
                "scope_dependents_limited": scope["dependents_limited"],
                "source": report, "blockers": [], "effect": after, "effect_before": before,
                "effect_actor": None if actor is None else actor_label(actor),
                "effect_note": ("撤销会移除整条授权；下面核对的是这个账号在撤销后还能读到什么。"
                                if actor is not None else
                                "这条授权的目标账号没有可核对的登记身份，因此只核对了不依赖身份的公开范围；"
                                "没有为它构造任何账号身份。"),
                "effect_basis": "实际核对（含来源与授权）",
                "remaining_readable": [check["label"] + " · " + check["purpose_label"]
                                       for check in remaining],
                "impact": affected,
                "will_apply": not remaining,
                "notice": ("确认后只撤销这一条授权，并同时影响上面列出的全部资料；其他授权、分类、来源和"
                           "回复授权保持原样。"
                           + (" 撤销后这个账号不能再通过这条授权使用这些资料。"
                              if not remaining else
                              " 注意：这个账号仍有其他可读依据，撤销这条授权不会让它完全读不到。")),
                "written": False}

    # --- grant scope for revocation -----------------------------------------

    def _scope(self, db, resources, *, grantee, grant_id, now, epoch):
        """Everything one grant touches, fingerprinted as a whole.

        Revocation keeps its core meaning (the whole grant is revoked), so the
        previewed scope is the grant's complete resource list *and* the complete
        source/dependant closure those records live in. The digest therefore
        covers, for every node: the full stored policy, its content version, its
        provenance registration; and for every covered record additionally its
        other grants (rows, active count and epoch). A new downstream record, a
        rotated source, a record crossing its expiry or a different grant of the
        same count all change it, so the commit refuses instead of revoking
        something the administrator did not see.
        """
        closure, content, other, present = [], {}, {}, set()
        if resources:
            pairs = [resource.split("/", 1) for resource in resources]
            content = {row["kind"] + "/" + row["id"]: row["version"] for row in db.execute(
                "SELECT c.kind, c.id, c.version FROM content_records c "
                "JOIN unnest(?::text[],?::text[]) AS wanted(kind,id) ON c.kind=wanted.kind AND c.id=wanted.id",
                ([pair[0] for pair in pairs], [pair[1] for pair in pairs])).fetchall()}
            present = set(content)
            other = {row["resource"]: [row["count"], row["active"], row["grantee_count"],
                                       row["grantee_active"], row["state"]]
                     for row in db.execute(
                "SELECT gr.resource AS resource, COUNT(*) AS count, "
                "COALESCE(SUM(CASE WHEN g.revoked=0 AND g.expires_at>? AND g.epoch=? THEN 1 ELSE 0 END),0) AS active, "
                "COALESCE(SUM(CASE WHEN g.grantee=? THEN 1 ELSE 0 END),0) AS grantee_count, "
                "COALESCE(SUM(CASE WHEN g.grantee=? AND g.revoked=0 AND g.expires_at>? AND g.epoch=? "
                "THEN 1 ELSE 0 END),0) AS grantee_active, "
                "md5(COALESCE(string_agg(g.id||'|'||g.grantee||'|'||g.owner||'|'||g.purposes||'|'||g.basis||'|'||"
                "g.evidence_id||'|'||g.expires_at||'|'||g.epoch||'|'||g.revoked, ',' ORDER BY g.id),'')) AS state "
                "FROM grant_resources gr JOIN governance_grants g ON g.id=gr.grant_id "
                "WHERE gr.resource=ANY(?) AND g.id<>? GROUP BY gr.resource",
                (now, epoch, grantee, grantee, now, epoch, resources, grant_id)).fetchall()}
            closure = db.execute(
                # Same shape as ``record_inspection.impact`` (derived graph plus
                # the virtual provenance targets), extended upwards so a rotated
                # *source* of a covered record is part of the fingerprint too.
                "WITH RECURSIVE affected(resource) AS (SELECT unnest(?::text[]) "
                "UNION SELECT d.derived FROM source_dependencies d JOIN affected a ON d.source=a.resource), "
                "virtual(resource) AS (SELECT resource FROM governance_provenance WHERE target=ANY(?::text[])), "
                "down(resource) AS (SELECT resource FROM affected UNION SELECT resource FROM virtual "
                "UNION SELECT d.derived FROM source_dependencies d JOIN down a ON d.source=a.resource), "
                "up(resource) AS (SELECT unnest(?::text[]) "
                "UNION SELECT d.source FROM source_dependencies d JOIN up a ON d.derived=a.resource) "
                "SELECT n.resource AS resource, p.version AS version, p.classification AS classification, "
                "p.owners AS owners, p.conversation AS conversation, p.audience AS audience, "
                "p.sources AS sources, p.source_versions AS source_versions, p.valid AS valid, "
                "p.expires_at AS expires_at, v.target AS target, v.kind AS kind, "
                "v.origin_version AS origin_version, "
                "(n.resource IN (SELECT resource FROM down)) AS downstream "
                "FROM (SELECT resource FROM down UNION SELECT resource FROM up) n "
                "LEFT JOIN governance_policies p ON p.resource=n.resource "
                "LEFT JOIN governance_provenance v ON v.resource=n.resource "
                "ORDER BY n.resource LIMIT ?",
                (resources, resources, resources, SCOPE_ROWS + 1)).fetchall()
        if len(closure) > SCOPE_ROWS:
            _fail("impact_limit", "这条授权的影响范围超过本页可以完整核对的上限，请先整理资料后再撤销。")
        policies = {}
        for row in closure:
            if row["version"] is not None:
                policies[row["resource"]] = self.repository._policy(row)
        view = []
        for resource in resources:
            policy = policies.get(resource)
            counts = other.get(resource)
            view.append({"resource": resource, "present": resource in present,
                         "content_version": content.get(resource),
                         "policy": None if policy is None else _policy_view(policy),
                         "other_grants": 0 if counts is None else counts[0],
                         "other_grants_active": 0 if counts is None else counts[1],
                         "grantee_grants": 0 if counts is None else counts[2],
                         "grantee_grants_active": 0 if counts is None else counts[3],
                         "remaining_basis": self._basis(
                             policy, grantee, counts is not None and counts[3] > 0, now)})
        graph = [[row["resource"], row["version"], row["classification"],
                  None if row["owners"] is None else sorted(json.loads(row["owners"])),
                  row["conversation"], row["audience"],
                  None if row["sources"] is None else list(json.loads(row["sources"])),
                  None if row["source_versions"] is None else list(json.loads(row["source_versions"])),
                  row["valid"], row["expires_at"],
                  # Validity *at the previewed instant*: a covered record crossing
                  # its expiry between preview and commit is a change as well.
                  bool(row["valid"]) and (row["expires_at"] is None or row["expires_at"] > now),
                  row["target"], row["kind"], row["origin_version"]]
                 for row in closure]
        roots = set(resources)
        dependents = [row["resource"] for row in closure
                      if row["downstream"] and row["resource"] not in roots]
        return {"digest": _digest([graph, sorted([entry for entry in
                                                  ([resource,
                                                    None if policies.get(resource) is None
                                                    else _policy_state(policies[resource]),
                                                    content.get(resource), other.get(resource)]
                                                   for resource in resources)], key=str)]),
                "view": {"resources": view, "count": len(resources),
                         "state_labels": {"public": "记录的分类是公开（仍取决于来源与有效状态）",
                                          "owner": "该账号登记为归属账号（仍取决于来源与有效状态）",
                                          "other_grant": "另有一条当前有效的授权记录（仍取决于来源与有效状态）",
                                          "conversation": "分类限定在原会话（仍取决于来源与有效状态）",
                                          "blocked": "记录已停用、过期或处于隔离分类",
                                          "missing": "没有权限记录",
                                          "none": "没有查到其他归属或授权记录"},
                         "basis_meaning": ("这些只是数据库里现存的依据提示，不是最终可读结论；"
                                           "最终是否可读由来源版本、有效状态与授权共同决定。")},
                "dependents": dependents[:MAX_DEPENDENTS_SHOWN],
                "dependents_count": len(dependents),
                "dependents_limited": len(dependents) > MAX_DEPENDENTS_SHOWN}

    def _basis(self, policy, grantee, other_grant, now):
        """A recorded hint about remaining bases, never a final access verdict."""
        if policy is None:
            return "missing"
        if not policy.valid or (policy.expires_at is not None and policy.expires_at <= now):
            return "blocked"
        if policy.classification in ("secret", "unclassified"):
            return "blocked"
        if policy.classification == "public":
            return "public"
        if policy.classification == "personal" and grantee in policy.owners:
            return "owner"
        if other_grant:
            return "other_grant"
        if policy.classification == "conversation":
            return "conversation"
        return "none"

    # --- commit / cancel -----------------------------------------------------

    def _entry(self, request, token):
        self._require(request)
        if not isinstance(token, str) or not token:
            _fail("sharing_preview_expired", "请重新预览后再确认。")
        self._prune()
        pending = self.previews.get(token)
        if not pending or pending["session"] != request.session_id or pending["expires"] <= time.monotonic():
            _fail("sharing_preview_expired", "预览已过期或管理会话已改变，请重新预览。")
        return pending

    def cancel(self, request, token):
        pending = self._entry(request, token)
        self.previews.pop(token, None)
        return {"cancelled": True, "action": pending["action"], "written": False}

    def _recheck(self, pending, db):
        """Every previewed fact is re-read inside the committing transaction."""
        item, policy = self._record(pending["kind"], pending["identifier"])
        if item["version"] != pending["record_version"] or policy.version != pending["policy_version"]:
            _fail("edit_conflict", "资料或权限在预览后已变化，请重新预览。")
        if _digest(_policy_state(policy)) != pending["policy"]:
            _fail("edit_conflict", "资料权限在预览后已变化，请重新预览。")
        now, epoch = self.repository.clock(), self.repository.epoch()
        if self._grants_digest(db, policy.resource, now, epoch) != pending["grants"]:
            _fail("edit_conflict", "已有授权在预览后已变化，请重新预览。")
        if impact(self.store, policy.resource)["fingerprint"] != pending["impact"]:
            _fail("edit_conflict", "资料依赖在预览后已变化，请重新预览。")
        if _digest(self._report(policy.resource)) != pending["sources"]:
            _fail("edit_conflict", "资料与来源状态在预览后已变化，请重新预览。")
        return item, policy

    def commit(self, request, token):
        self._require(request)
        self._entry(request, token)
        pending = self.previews.pop(token, None)
        try:
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.authority.require(request, administrator=True)
                item, policy = self._recheck(pending, db)
                if pending["action"] == "grant":
                    return self._commit_grant(request, pending, policy, db)
                if pending["action"] == "classify":
                    return self._commit_classify(request, pending, policy, db)
                return self._commit_revoke(request, pending, policy, db)
        except BaseException:
            # A commit refused inside this transaction kept nothing (the nested
            # unit of work rolls back), so the preview may be retried after the
            # administrator re-reads the record. A failure after this returns —
            # for example the outer request receipt — is outside this scope: the
            # token stays consumed and the administrator previews again.
            self.previews[token] = pending
            raise

    def _commit_grant(self, request, pending, policy, db):
        payload = pending["payload"]
        actor = self._target_actor(payload["grantee"])
        if self._foundation(pending["kind"], pending["identifier"]):
            _fail("sharing_grant", "共用基础设定仍在原来的登记流程中管理。")
        if payload["owner"] not in policy.owners:
            _fail("sharing_grant", "归属账号已变化，请重新预览。")
        purposes = tuple(payload["purposes"])
        written = self.admin.authorize(request, grantee=payload["grantee"], owner=payload["owner"],
                                       resources=[policy.resource], purposes=purposes,
                                       lifetime=payload["days"] * 86400)
        self.repository.audit(request.actor, "desktop_sharing_grant", policy.resource,
                              policy.version, written["id"], db=db)
        effect = self._effect(self.repository, policy.resource, policy, purposes, actor)
        applied = self._grant_effective(effect, purposes)
        return {"action": "grant", "resource": policy.resource, "grant_id": written["id"],
                "expires_at": written["expires_at"], "resources": written["resources"],
                "target": actor_label(actor), "purposes": list(purposes),
                "effect": effect, "effect_actor": actor_label(actor),
                "effective": applied, "changed": True,
                "notice": ("已新增一条私聊授权，并已按当前资料核对：这个账号在私聊中使用所选用途时可以读取它。"
                           "没有授权会话、没有启动平台、没有改变回复额度。" if applied else
                           "已记录一条私聊授权，但按当前来源或分类它在私聊里仍不能生效；请先处理来源或分类。")}

    def _commit_classify(self, request, pending, policy, db):
        classification = pending["payload"]["classification"]
        if classification == policy.classification:
            _fail("edit_conflict", "分类在预览后已变化，请重新预览。")
        # ``replace`` keeps owners/sources/pins/valid/expires_at/conversation/audience.
        updated = replace(policy, version=secrets.token_hex(16), classification=classification)
        self.repository.set_policy(updated, policy.version, request.actor, db=db)
        self.repository.audit(request.actor, "desktop_sharing_classify", policy.resource,
                              policy.version, updated.version, db=db)
        owner = self._owner_actor(updated)
        effect = self._effect(self.repository, updated.resource, updated, PURPOSES, owner)
        applied = self._will_apply(effect)
        semantic_before = self._effect(
            _PreviewRepository(self.repository, policy=policy, neutral=True),
            policy.resource, policy, PURPOSES, owner)
        semantic_after = self._effect(
            _PreviewRepository(self.repository, policy=updated, neutral=True),
            policy.resource, updated, PURPOSES, owner)
        direction = self._direction(semantic_before, semantic_after)
        return {"action": "classify", "resource": policy.resource, "classification": classification,
                "version": updated.version, "owners": sorted(updated.owners),
                "sources": list(updated.sources), "valid": bool(updated.valid),
                "expires_at": updated.expires_at, "effect": effect, "effective": applied,
                "direction": direction["label"], "direction_detail": direction,
                "owner_checked": None if owner is None else actor_label(owner),
                "effect_actor": None if owner is None else actor_label(owner),
                "changed": True,
                "notice": ("已改变分类并保留原归属、来源、有效状态与到期时间。"
                           if applied else
                           "已改变分类并保留原归属、来源、有效状态与到期时间，但按当前来源它仍不能生效。")}

    def _commit_revoke(self, request, pending, policy, db):
        grant_id = pending["payload"]["grant_id"]
        row = self._grant_row(db, grant_id, lock=True)
        if row is None:
            _fail("edit_conflict", "授权在预览后已不存在，请重新预览。")
        if policy.resource not in json.loads(row["resources"]):
            _fail("sharing_revoke", "该授权不属于这条资料，已停止撤销。")
        if row["revoked"]:
            _fail("edit_conflict", "授权在预览后已被撤销，请重新预览。")
        if _grant_state(row) != pending["payload"]["grant"]:
            _fail("edit_conflict", "授权在预览后已变化，请重新预览。")
        resources = sorted(json.loads(row["resources"]))
        if resources != pending["payload"]["resources"]:
            _fail("edit_conflict", "授权覆盖的资料在预览后已变化，请重新预览。")
        now, epoch = self.repository.clock(), self.repository.epoch()
        scope = self._scope(db, resources, grantee=row["grantee"], grant_id=grant_id, now=now, epoch=epoch)
        if scope["digest"] != pending["payload"]["scope"]:
            _fail("edit_conflict", "这条授权覆盖的资料在预览后已变化，请重新预览。")
        # The core semantics stay exactly as they are: one grant, revoked whole.
        self.admin.revoke(request, grant_id)
        self.repository.audit(request.actor, "desktop_sharing_revoke", policy.resource, grant_id, None, db=db)
        try:
            actor = self._target_actor(row["grantee"])
        except AssistantError:
            actor = None
        purposes = tuple(sorted(json.loads(row["purposes"])))
        effect = self._effect(self.repository, policy.resource, policy, purposes, actor) if actor else []
        remaining = [check for check in effect if check["allowed"]]
        return {"action": "revoke", "resource": policy.resource, "grant_id": grant_id,
                "revoked": True, "changed": True, "effect": effect,
                "resources": resources, "effect_actor": None if actor is None else actor_label(actor),
                "remaining_readable": [check["label"] + " · " + check["purpose_label"] for check in remaining],
                "effective": not remaining,
                "notice": ("已撤销这一条授权，并同时影响这条授权覆盖的全部资料。"
                           + ("" if not remaining else
                              " 注意：这个账号仍有其他可读依据，撤销这条授权不会让它完全读不到。"))}
