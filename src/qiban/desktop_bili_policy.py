"""Bilibili uses the shared action ledger and a separate set of limits per bound UID."""
import copy
import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml

from bili_assistant import schema
from bili_assistant.action_quota import daily_usage
from companion_core.errors import AssistantError


@lru_cache(maxsize=1)
def _defaults():
    value = yaml.safe_load((Path(schema.__file__).parent / "defaults" / "policy.yaml").read_text(encoding="utf8"))
    value["actions"].update(favorite_create={"enabled":True,"daily_limit":5},
                            favorite_move={"enabled":True,"daily_limit":50})
    return value


def _settings(store, uid, db=None):
    existing = store.state("desktop-bili-policy:"+uid,db=db)
    if existing is not None:
        return {**existing,"actions":{**copy.deepcopy(_defaults()["actions"]),**existing["actions"]}}
    return {"revision":"default", "actions":copy.deepcopy(_defaults()["actions"])}


class BiliRuntimeStore:
    """Project Bilibili's account and policy into its existing controller only."""
    def __init__(self, store, uid):
        self.store, self.uid = store, uid

    def __getattr__(self, name):
        return getattr(self.store,name)

    def read(self, kind, item_id=None):
        document = self.store.read(kind,item_id)
        if item_id is None and kind=="companion" and document["data"].get("native_tools_enabled"):
            # The chosen desktop model route supports structured decisions and image inputs.
            return {**document,"data":{**document["data"],"native_tools_enabled":False}}
        if item_id is not None or kind not in ("manifest","policy"):
            return document
        if kind=="manifest":
            data = {**document["data"], "account_uid":self.uid}
            scope = self.uid
        else:
            settings = _settings(self.store,self.uid)
            data = {**_defaults(), **document["data"], "actions":copy.deepcopy(settings["actions"])}
            scope = self.uid+":"+settings["revision"]
        revision = hashlib.sha256((document["version"]+":"+scope).encode()).hexdigest()
        return {**document,"data":data,"version":revision}

    def version(self, kind, item_id=None):
        if item_id is None and kind in ("manifest","policy"):
            return self.read(kind)["version"]
        return self.store.version(kind,item_id)


def manage(manager, session, uid, *, actions=None, expected=None, save=False):
    broker = manager.bili
    # A transport can hold the role's file lock during I/O. Edits use their own
    # transaction so a pending send can observe a newly saved policy in its guard.
    with broker.lock, manager.store.db() as db:
        if not manager._active(session):
            raise AssistantError("desktop_locked","请先解锁本机管理。")
        account = broker._account()
        if not account or account["uid"]!=uid:
            raise AssistantError("account_changed","B 站账号已改变，请重新读取互动设置。")
        if save:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(?))",("desktop-bili-policy:"+uid,))
        current = _settings(manager.store,uid,db)
        if save:
            if (not isinstance(actions,dict) or set(actions)!=set((*schema.ACTIONS,*schema.FOLDER_ACTIONS))
                    or any(not isinstance(rule,dict) or set(rule)!={"enabled","daily_limit"}
                           or type(rule["enabled"]) is not bool or type(rule["daily_limit"]) is not int
                           or not 0<=rule["daily_limit"]<=1000 for rule in actions.values())):
                raise AssistantError("invalid_policy","每项互动和收藏夹操作都需要开关和 0 至 1000 的整数日限额。")
            if expected!=current["revision"] and actions!=current["actions"]:
                raise AssistantError("edit_conflict","互动设置已改变，请重新读取后再保存。")
            if actions!=current["actions"]:
                updated = {"revision":uuid4().hex,"actions":actions}
                manager.store.set_state("desktop-bili-policy:"+uid,updated,db)
                manager.repository.audit(manager.actor.key,"desktop_bili_policy",uid,current["revision"],updated["revision"],db=db)
                current = updated
        zone = manager.store.read("policy")["data"]["timezone"]
        day = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date().isoformat()
        used = daily_usage(db,uid,day)
        return {"uid":uid,**current,"defaults":copy.deepcopy(_defaults()["actions"]),"day":day,"timezone":zone,
                "used":{kind:used.get(kind,0) for kind in (*schema.ACTIONS,*schema.FOLDER_ACTIONS)}}
