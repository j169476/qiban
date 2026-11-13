"""Desktop administrator confirmation and revocation against the same graph used by model review."""
from .contracts import ActorRef, stable_key
from .errors import AssistantError
from .identity import IdentityService
from .person_baseline import actor_by_key
from .storage import utcnow

PAGE_MAX = 50
EVIDENCE_MAX = 30           # identity_links schema maxItems
EVIDENCE_TEXT_MAX = 12000   # bounded page for the quotes already stored in the link
HISTORY_MAX = 100           # identity_links keeps the newest 100 entries only
REASON_MAX = 2000           # existing identity_links TEXT limit; no new stricter rule is added
STATUS_LABELS = {"suspected": "疑似同一人（待核对）", "hard": "已确认同一人",
                 "disputed": "有争议", "revoked": "已撤销"}
DEFAULT_REVOKE_REASON = "本机管理员撤销了这条账号关联"
MEANING = ("这里只查看和撤销已有的账号关联，不新建关联，也不改身份图规则。"
           "身份关联不授予回复授权、资料共享或基础关系选择。")
REVOKE_NOTICE = ("确认后只撤销这一条账号关联：把它标记为已撤销并锁定，原记录、证据引用和文档历史保留，"
                 "模型不能再自行改写它。按现有核心语义，这条关联的原因字段会改写为撤销说明。"
                 "其他人工关联路径仍可能使这些账号被视作同一人；共用关系设定按原规则动态匹配，"
                 "请重新核对适用账号。这里不会批量拆分其他关联。"
                 "回复授权、资料共享和基础关系选择都不因此改变。")
REVOKED_NOTICE = "这条账号关联已经是撤销状态，原记录与文档历史保留；本页不会重复撤销。"


def inspect_pair(store, left, right):
    actors = [actor_by_key(store, key) for key in (left, right)]
    if actors[0].platform == actors[1].platform:
        raise AssistantError("identity_same_platform", "请选择两个不同平台的账号")
    graph = IdentityService(store)
    links = graph.links()
    if graph.issues:
        raise AssistantError("identity_unavailable", "身份记录有异常，请先检查资料")
    groups = [graph._person(actor, links)["members"] for actor in actors]
    keys = [{ActorRef(**member).key for member in group} for group in groups]
    link_id = stable_key("link", *sorted((left, right)))
    direct = None
    for record in links:
        doc = record["data"]
        pair = {ActorRef(**doc[side]).key for side in ("left", "right")}
        if pair & keys[0] and pair & keys[1] and record["effective_status"] in {"revoked", "disputed"}:
            raise AssistantError("identity_conflict", "这些账号之间已有争议或撤销记录，请先处理原关联")
        if doc["id"] == link_id:
            direct = record
    if keys[0] & keys[1]:
        raise AssistantError("identity_already_linked", "现有身份关联已将两个账号识别为同一人")
    if direct and direct["data"]["locked"]:
        raise AssistantError("identity_locked", "该关联已锁定，请先处理原记录")
    revision = stable_key("preview", left, right, *[sorted(group) for group in keys],
                          direct["version"] if direct else None)
    return {"groups": groups, "expected": revision}, actors, link_id, direct


def manage(admin, request, left, right, expected=None):
    admin.authority.require(request, administrator=True)
    store = admin.store
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        preview, actors, link_id, direct = inspect_pair(store, left, right)
        if expected is None:
            return preview
        if expected != preview["expected"]:
            raise AssistantError("identity_changed", "关联账号已变化，请重新核对")
        old = direct["data"] if direct else {}
        reason = "本机管理员已核对两个平台账号并确认是同一人"
        entry = {"status": "hard", "kind": "manual_confirmation", "reason": reason, "at": utcnow()}
        doc = {"schema_version": 1, "id": link_id, "left": actors[0].as_dict(), "right": actors[1].as_dict(),
               "status": "hard", "manual": True, "locked": True, "reason": reason,
               "evidence": old.get("evidence", []), "history": [*old.get("history", []), entry][-100:]}
        before = direct["version"] if direct else None
        saved = store.write("identity_links", doc, before, "admin:" + request.actor, link_id)
        admin.repository.audit(request.actor, "confirm_identity", "identity_links/" + link_id,
                               before, saved["version"], db=db)
        return {"confirmed": True, "version": saved["version"]}


def _links(store):
    graph = IdentityService(store)
    links = graph.links()
    if graph.issues:
        raise AssistantError("identity_unavailable", "身份记录有异常，请先检查资料")
    return graph, links


def _registered(store, db, keys):
    """Only registration that really exists may become a readable label."""
    wanted = sorted({key for key in keys if isinstance(key, str) and key})
    result = {}
    for offset in range(0, len(wanted), 400):
        batch = wanted[offset:offset + 400]
        rows = db.execute("SELECT actor_key,identity_body FROM actor_nodes WHERE actor_key IN ("
                          + ",".join("?" for _ in batch) + ")", batch)
        for row in rows:
            try:
                actor = ActorRef(**store.ledger_decode("actor_nodes.identity", row["identity_body"]))
            except (AssistantError, KeyError, TypeError, ValueError):
                continue
            if actor.key == row["actor_key"]:
                result[actor.key] = actor.as_dict()
    return result


def _account(value, registered):
    actor = ActorRef(**value)
    return {**actor.as_dict(), "key": actor.key, "registered": actor.key in registered}


def _event_versions(db, references):
    """The stored version and liveness of each cited message; its body is never read."""
    result, wanted = {}, sorted({ref["event_id"] for ref in references})
    for offset in range(0, len(wanted), 400):
        batch = wanted[offset:offset + 400]
        rows = db.execute("SELECT id,version,deleted FROM core_events WHERE id IN ("
                          + ",".join("?" for _ in batch) + ")", batch)
        for row in rows:
            result[row["id"]] = {"version": row["version"], "deleted": bool(row["deleted"])}
    return result


def _evidence(doc, current):
    items, used = [], 0
    for ref in doc["evidence"][:EVIDENCE_MAX]:
        quote, stored = ref["quote"], current.get(ref["event_id"])
        item = {"event_id": ref["event_id"], "stored_version": ref["version"], "quote": None,
                "current_version": None if stored is None else stored["version"]}
        if stored is None or stored["deleted"]:
            item.update(state="missing", state_label="这条引用记录已不存在或已删除；本页不重建原文")
        elif stored["version"] != ref["version"]:
            item.update(state="changed",
                        state_label="引用记录已更新，当前版本不是当时保存的版本；本页不展示现在的消息正文")
        elif used + len(quote) > EVIDENCE_TEXT_MAX:
            item.update(state="page_limited", state_label="本页引文总量已达上限，这一条正文没有展示")
        else:
            item.update(state="stored", state_label="当时保存的引用原文（保存的版本与当前一致）")
            item["quote"], used = quote, used + len(quote)
        items.append(item)
    return {"items": items, "count": len(items), "limit": EVIDENCE_MAX, "text_limit": EVIDENCE_TEXT_MAX,
            "meaning": ("这里显示的是这条关联里当时保存的引用和它的原版本；"
                        "不读取其他消息，也不用现在的正文冒充当时的原文。")}


def _history(doc):
    items = []
    for entry in doc["history"][-HISTORY_MAX:]:
        status, evidence = entry.get("status"), entry.get("evidence")
        items.append({"at": entry.get("at"), "status": status,
                      "status_label": STATUS_LABELS.get(status, status or "未记录"),
                      "kind": entry.get("kind"),
                      "reason": entry.get("reason") if isinstance(entry.get("reason"), str) else None,
                      "evidence_count": len(evidence) if isinstance(evidence, list) else 0})
    return {"items": items, "count": len(items), "limit": HISTORY_MAX,
            "truncated": len(doc["history"]) >= HISTORY_MAX,
            "meaning": ("这里只列出这条关联的文档内历史，最多保留最近 100 条；"
                        "更早的变更不在文档里，本页不把它说成全部历史。")}


def _members(person, registered):
    accounts = [_account(value, registered) for value in person["members"]]
    return {"accounts": accounts, "count": len(accounts), "inferred": person["inferred"],
            "person_id": person["person_id"], "grants_permissions": False,
            "notice": ("这是按现有身份图读到的当前成员；撤销一条关联不一定把所有账号拆开，"
                       "其他人工关联路径仍可能使它们被视作同一人。")}


def listing(store, after):
    """One bounded page of existing links, using the core graph read as the only status source."""
    links = _links(store)[1]
    ordered = sorted(links, key=lambda record: record["data"]["id"])
    start = 0
    if after:
        start = next((index for index, record in enumerate(ordered) if record["data"]["id"] > after),
                     len(ordered))
    page = ordered[start:start + PAGE_MAX]
    with store.db() as db:
        registered = _registered(store, db, {ActorRef(**record["data"][side]).key
                                             for record in page for side in ("left", "right")})
    items = []
    for record in page:
        doc, status = record["data"], record["effective_status"]
        items.append({"id": doc["id"], "version": record["version"], "status": status,
                      "status_label": STATUS_LABELS.get(status, status),
                      "manual": doc["manual"], "locked": doc["locked"],
                      "revocable": status != "revoked",
                      "left": _account(doc["left"], registered),
                      "right": _account(doc["right"], registered)})
    return {"links": items, "count": len(items), "total": len(ordered), "page_max": PAGE_MAX,
            "after": after, "next": page[-1]["data"]["id"] if len(ordered) > start + PAGE_MAX else None,
            "meaning": MEANING}


def detail(store, identifier):
    graph, links = _links(store)
    record = next((item for item in links if item["data"]["id"] == identifier), None)
    if record is None:
        raise AssistantError("not_found", "找不到这条账号关联，请重新读取")
    doc, status = record["data"], record["effective_status"]
    person = graph._person(ActorRef(**doc["left"]), links)
    with store.db() as db:
        registered = _registered(store, db, {ActorRef(**doc[side]).key for side in ("left", "right")}
                                 | {ActorRef(**member).key for member in person["members"]})
        current = _event_versions(db, doc["evidence"])
    return {"id": doc["id"], "version": record["version"], "status": status,
            "status_label": STATUS_LABELS.get(status, status), "stored_status": doc["status"],
            "manual": doc["manual"], "locked": doc["locked"],
            "left": _account(doc["left"], registered), "right": _account(doc["right"], registered),
            "reason": doc["reason"], "evidence": _evidence(doc, current), "history": _history(doc),
            "person": _members(person, registered),
            "revocable": status != "revoked", "reason_max": REASON_MAX,
            "notice": REVOKE_NOTICE if status != "revoked" else REVOKED_NOTICE,
            "meaning": MEANING}


def revoke(admin, request, identifier, expected, reason):
    """Revoke one existing link through the core writer inside the desktop transaction."""
    admin.authority.require(request, administrator=True)
    store = admin.store
    if not isinstance(expected, str) or not expected:
        raise AssistantError("identity_changed", "请重新读取这条账号关联后再确认")
    if not isinstance(reason, str) or len(reason.strip()) > REASON_MAX:
        raise AssistantError("identity_reason", "撤销说明最多 " + str(REASON_MAX) + " 字。")
    text = reason.strip() or DEFAULT_REVOKE_REASON
    graph = IdentityService(store)
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = store.read("identity_links", identifier)
        if current["version"] != expected:
            raise AssistantError("identity_changed", "这条账号关联已变化，请重新核对")
        if current["data"]["status"] == "revoked":
            raise AssistantError("identity_already_revoked", "这条账号关联已经是撤销状态")
        saved = graph.revoke(identifier, expected, text)
        admin.repository.audit(request.actor, "revoke_identity", "identity_links/" + identifier,
                               expected, saved["version"], db=db)
    doc = saved["data"]
    return {"revoked": True, "identifier": identifier, "version": saved["version"],
            "status": doc["status"], "status_label": STATUS_LABELS.get(doc["status"], doc["status"]),
            "manual": doc["manual"], "locked": doc["locked"],
            "evidence_count": len(doc["evidence"]), "history_count": len(doc["history"]),
            "reason": doc["reason"], "meaning": MEANING}
