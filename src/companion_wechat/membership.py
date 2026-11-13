"""Bind contact observations to the exact manual grants present before a read."""
from companion_core.reply_audience import Audience, LiveMembers, catchup_since, read, registry
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope

from .contracts import require


class FollowDirectory:
    """Publish only complete membership snapshots belonging to this receiver."""
    def __init__(self, store):
        self.store = store
        self.live = {}
        self.chats = {}

    def disconnect(self):
        with self.store.lock:
            shared = registry(self.store)
            for category, members in self.live.items():
                members.disconnect()
                if shared.get(category) is members:
                    del shared[category]
            self.live.clear()
            self.chats.clear()

    async def refresh(self, identity, worker, current, *, find_chats=False):
        snapshots = {}
        chats = {}
        try:
            for kind in ("private", "group"):
                category = Audience("wechat", identity, kind)
                policy = read(self.store, category)
                if catchup_since(policy) is None:
                    continue
                members, cursors, cursor = {}, set(), None
                while True:
                    require(current(), "wechat_stale_connection")
                    page = await worker.request("chat_member_page" if find_chats else "member_page", kind=kind, cursor=cursor)
                    require(isinstance(page, dict) and set(page) == ({"ids","next","chats"} if find_chats else {"ids","next"})
                            and isinstance(page["ids"], list) and len(page["ids"]) <= 256, "wechat_source_invalid")
                    for username in page["ids"]:
                        require(isinstance(username,str) and username and username not in members
                                and username.endswith("@chatroom") == (kind == "group"), "wechat_source_invalid")
                        members[username] = username
                    if find_chats:
                        require(isinstance(page["chats"],list) and set(page["chats"]) <= set(page["ids"]), "wechat_source_invalid")
                        for name in page["chats"]:
                            chats[name] = (category,policy["version"],policy["since"])
                    cursor = page["next"]
                    if cursor is None:
                        break
                    require(isinstance(cursor,str) and len(cursor)==32 and cursor not in cursors
                            and len(page["ids"])==256, "wechat_source_invalid")
                    cursors.add(cursor)
                snapshots[category] = (policy["version"], members)
            with self.store.lock:
                require(current(), "wechat_stale_connection")
                for category,(revision,_) in snapshots.items():
                    require(read(self.store,category)["version"] == revision, "reply_not_authorized")
                shared = registry(self.store)
                for category in self.live.keys() - snapshots.keys():
                    old = self.live.pop(category)
                    old.disconnect()
                    if shared.get(category) is old:
                        del shared[category]
                for category,(revision,ids) in snapshots.items():
                    live = self.live.get(category)
                    if live is None:
                        live = self.live[category] = LiveMembers()
                    live.replace(revision, ids)
                    shared[category] = live
                self.chats = chats
        except BaseException:
            self.disconnect()
            raise


class MembershipCheck:
    def __init__(self, store, identity, targets):
        self.store = store
        self.permissions = ReplyAuthorizations(store)
        self.targets = {}
        with store.db() as db:
            for item in targets:
                scope = ReplyScope("wechat", identity, item["kind"], item["username"])
                record = self.permissions.record(scope, db=db)
                self.targets[scope.target_id] = (scope, record["version"])

    def finish(self, result):
        require(isinstance(result, dict) and isinstance(result.get("unavailable"), list)
                and all(isinstance(i, str) for i in result["unavailable"]), "wechat_source_invalid")
        missing = set(result["unavailable"])
        require(missing <= self.targets.keys() and len(missing) == len(result["unavailable"])
                and type(result.get("verified")) is int
                and result["verified"] == len(self.targets) - len(missing), "wechat_source_invalid")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for username, (scope, version) in self.targets.items():
                if username in missing:
                    live = registry(self.store).get(Audience("wechat", scope.account_identity, scope.kind))
                    if live is not None:
                        live.remove(username)
                if version is not None:
                    self.permissions.observe_member(scope, username not in missing, version, db)
