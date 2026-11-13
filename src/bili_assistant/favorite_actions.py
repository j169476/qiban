"""Favorite-folder mutations reuse the account's durable action ledger."""
import hashlib
import json
import re

from .errors import AssistantError
from .favorites import BV, PAGE_SIZE


def validate_payload(kind, payload):
    request = payload.get("request_id")
    if request is not None and (not isinstance(request, str) or not re.fullmatch(r"[a-f0-9]{32}", request)):
        raise AssistantError("invalid_payload", "收藏夹操作标识无效。")
    if kind == "favorite_create":
        if (set(payload) - {"request_id"} != {"title", "private"} or not isinstance(payload["title"], str)
                or not payload["title"].strip() or payload["title"] != payload["title"].strip()
                or len(payload["title"]) > 80 or type(payload["private"]) is not bool):
            raise AssistantError("invalid_payload", "新建收藏夹需要名称和明确可见性。")
        return dict(payload)
    if (not {"source", "target", "bvids"} <= set(payload)
            or set(payload) - {"source", "target", "bvids", "units", "request_id"}
            or any(type(payload[key]) is not int or payload[key] <= 0 for key in ("source", "target"))
            or payload["source"] == payload["target"] or not isinstance(payload["bvids"], list)
            or not 1 <= len(payload["bvids"]) <= PAGE_SIZE
            or any(not isinstance(b, str) or not BV.fullmatch(b) for b in payload["bvids"])
            or len(set(payload["bvids"])) != len(payload["bvids"])
            or "units" in payload and (type(payload["units"]) is not int or payload["units"] != len(payload["bvids"]))):
        raise AssistantError("invalid_payload", "移动需要不同收藏夹和 1 至 20 个独立视频，按视频个数计额。")
    return {**payload, "bvids": sorted(payload["bvids"]), "units": len(payload["bvids"])}


def action_target(kind, payload):
    # Changing visibility cannot bypass an unknown create with the same title.
    value = payload["title"] if kind == "favorite_create" else [payload["source"], payload["target"], payload["bvids"]]
    # A new observed intent can organize content again after a later manual
    # change. Unknown overlapping effects still block it in _eligibility.
    return hashlib.sha256(json.dumps([value, payload.get("request_id")], ensure_ascii=False).encode()).hexdigest()


def require_no_unknown_overlap(store, db, account, kind, payload):
    rows = db.execute("SELECT payload FROM actions WHERE account=? AND kind=? "
                      "AND status IN ('dispatching','uncertain')", (account, kind))
    for row in rows:
        old = store.ledger_decode("actions.payload", row["payload"])
        overlap = (old["title"] == payload["title"] if kind == "favorite_create"
                   else bool(set(old["bvids"]) & set(payload["bvids"])))
        if overlap:
            raise AssistantError("favorite_unconfirmed", "相关收藏操作尚未核对完成，不能换批次重复提交。")


async def recover(engine, tools=None, limit=5):
    """Read back uncertain effects; absence never proves an interrupted POST failed."""
    run = engine.role_run()
    cursor_key = "favorite-recovery-cursor:" + run["account"]
    after = engine.store.state(cursor_key, "")
    with engine.store.db() as db:
        def read(cursor):
            return [dict(row) for row in db.execute(
                "SELECT id,kind,payload,units FROM actions WHERE account=? AND status='uncertain' "
                "AND kind IN ('favorite_create','favorite_move') AND id>? ORDER BY id LIMIT ?",
                (run["account"], cursor, limit))]
        rows = read(after)
        if not rows and after:
            rows = read("")
    if not rows:
        return {"checked": 0, "verified": 0}
    if tools is None:
        tools = engine.adapter.favorite_tools(lambda: engine.run(run["generation"]))
    verified = 0
    for row in rows:
        engine.run(run["generation"])
        payload = engine.store.ledger_decode("actions.payload", row["payload"])
        result = None
        if row["kind"] == "favorite_create":
            matching = [folder for folder in await tools._catalog() if folder["title"] == payload["title"]]
            if len(matching) == 1 and matching[0]["private"] == payload["private"]:
                result = {"folder": matching[0], "verified": True, "recovered": True}
        else:
            complete = True
            for bvid in payload["bvids"]:
                _, states = await tools._catalog(bvid)
                if states.get(payload["source"]) is not False or states.get(payload["target"]) is not True:
                    complete = False
                    break
            if complete:
                result = {"moved": payload["bvids"], "verified": True, "recovered": True}
        if result is not None:
            engine.run(run["generation"])
            with engine.store.db() as db:
                changed = db.execute("UPDATE actions SET status='succeeded',result=? WHERE id=? AND status='uncertain'",
                                     (engine.store.ledger_encode("actions.result", result), row["id"])).rowcount
            if changed:
                engine.store.audit("actions", {"id": row["id"], "kind": row["kind"], "status": "succeeded", "recovered": True})
                verified += 1
    engine.store.set_state(cursor_key, rows[-1]["id"])
    return {"checked": len(rows), "verified": verified}
