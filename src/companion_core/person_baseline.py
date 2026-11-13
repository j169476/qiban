"""One manual role relationship; identity membership remains owned by IdentityService."""
from . import baseline
from .contracts import ActorRef
from .errors import AssistantError
from .identity import IdentityService
from .storage import utcnow

SELECTION = "baseline-person-selection"
PLATFORMS = ("bilibili", "qq", "wechat")


def eligible(actor):
    return actor.platform in PLATFORMS and not actor.scope.startswith("wechat-provisional-")


def actor_by_key(store, key):
    with store.db() as db:
        row = db.execute("SELECT identity_body FROM actor_nodes WHERE actor_key=?", (key,)).fetchone()
    if row:
        actor = ActorRef(**store.ledger_decode("actor_nodes.identity", row["identity_body"]))
        if actor.key == key and eligible(actor):
            return actor
    raise AssistantError("unknown_actor", "该账号未登记或身份尚未核实，请重新选择")


def accounts(store, after):
    with store.db() as db:
        rows = db.execute("SELECT actor_key,identity_body FROM actor_nodes WHERE actor_key>? "
                          "AND platform IN ('bilibili','qq','wechat') ORDER BY actor_key LIMIT 51", (after,)).fetchall()
    result = []
    for row in rows[:50]:
        actor = ActorRef(**store.ledger_decode("actor_nodes.identity", row["identity_body"]))
        if eligible(actor):
            result.append({"key": actor.key, **actor.as_dict()})
    return {"accounts": result, "next": rows[49]["actor_key"] if len(rows) > 50 else None}


def members(store, anchor):
    actor = actor_by_key(store, anchor)
    group = IdentityService(store).person(actor)
    return [{"key": ref.key, **ref.as_dict()} for value in group["members"]
            if eligible(ref := ActorRef(**value))]


def read(store):
    card = store.read("persona")
    setting = card["data"].get("relationship_baseline")
    selection = store.state(SELECTION, {}) if setting and setting.get("target") == "person" else {}
    return {"expected": card["version"], "shared": baseline.enabled(store), "setting": setting,
            "anchor": selection.get("anchor"), "selected": selection.get("selected", [])}


def save(admin, request, *, anchor, selected, expected, setting):
    admin.authority.require(request, administrator=True)
    store = admin.store
    if not baseline.enabled(store):
        raise AssistantError("baseline_unavailable", "请先登记共用人物设定")
    if not isinstance(setting, dict) or set(setting) != {"owner_name", "addresses", "level", "description"}:
        raise AssistantError("invalid_data", "基础关系字段不完整")
    if (not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected)
            or len(set(selected)) != len(selected) or anchor not in selected):
        raise AssistantError("invalid_selection", "请选择主要账号，并确认适用的账号")
    with store.lock, store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not set(selected).issubset(item["key"] for item in members(store, anchor)):
            raise AssistantError("identity_changed", "所选账号当前不属于同一个人，请重新核对关联")
        for key in selected:
            actor_by_key(store, key)
        value = {**setting, "target": "person", "source": {"type": "user_instruction",
                 "reference": "manual-person-baseline", "recorded_at": utcnow()}}
        result = admin.patch_config(request, "persona", {"relationship_baseline": value}, expected)
        store.set_state(SELECTION, {"anchor": anchor, "selected": sorted(selected)}, db)
    return {"version": result["version"], "selected_count": len(selected)}


def matches(store, actor):
    """Do not load other people's dossiers or disclose any linked account IDs."""
    selection = store.state(SELECTION, {})
    if actor.key not in selection.get("selected", ()):
        return False
    anchor = selection["anchor"]
    if actor.key == anchor:
        return True
    return any(ActorRef(**ref).key == anchor for ref in IdentityService(store).person(actor)["members"])

def clear(admin, request, expected):
    admin.authority.require(request, administrator=True)
    store = admin.store
    with store.lock, store.db() as db, baseline.manual_edit(store):
        db.execute("BEGIN IMMEDIATE")
        card = store.read("persona")["data"]
        data = {k: v for k, v in card.items() if k != "relationship_baseline"}
        result = store.write("persona", data, expected, "admin:" + request.actor)
        baseline.sync_manual_card(store, card, request.actor)
        store.set_state(SELECTION, {}, db)
        admin.repository.audit(request.actor, "clear_relationship_baseline", "persona/@config",
                               expected, result["version"], db=db)
    return {"version": result["version"]}
