"""Account-specific grants for current platform members. Manual choices are stored separately."""

import math
import secrets
import time
from dataclasses import dataclass

from .contracts import stable_key
from .errors import AssistantError

PREFIX = "reply-audience-v1:"


@dataclass(frozen=True)
class Audience:
    platform: str
    account_identity: str
    kind: str

    def __post_init__(self):
        from .reply_authorization import ReplyScope

        ReplyScope(self.platform, self.account_identity, self.kind, "validation")

    @property
    def key(self):
        return PREFIX + stable_key("audience", self.platform, self.account_identity, self.kind)

    def describe(self):
        return {"platform": self.platform, "account_identity": self.account_identity, "kind": self.kind}


def read(store, category, db=None):
    value = store.state(category.key, None, db)
    if value is None:
        return {"scope": category.describe(), "enabled": False, "version": None}
    if (
        value.get("scope") != category.describe()
        or type(value.get("enabled")) is not bool
        or not isinstance(value.get("version"), str)
    ):
        raise AssistantError("reply_authorization_corrupt", "回复范围记录异常，请检查本机资料")
    return value


def preview(store, category, enabled):
    previous = read(store, category)
    platform = {"qq": "QQ", "wechat": "微信"}[category.platform]
    label = "好友" if category.kind == "private" else "群"
    if enabled:
        effect = (
            "当前及以后新添加的好友都可能收到自动回复。"
            if category.kind == "private"
            else "当前及以后新加入的群都可按群聊唤起规则自动回复，群成员可以看到回复。"
        )
        message = f"高风险：{platform} 账号 {category.account_identity}。{effect}恢复后也会补处理本次开启以来可见的未处理消息。生成回复可能消耗模型额度。"
        button = f"确认允许所有{label}"
    else:
        message = f"{platform} 账号 {category.account_identity} 将使用手动保存的{label}范围。"
        button = "恢复手动范围"
    return {
        "scope": category.describe(),
        "expected": previous["version"],
        "enabled": enabled,
        "message": message,
        "button": button,
    }


def change(store, category, enabled, expected, *, actor, confirmed, db):
    if type(enabled) is not bool or not actor or enabled and confirmed != category.describe():
        raise AssistantError("reply_scope_confirmation", "请核对平台、登录账号和回复范围，再确认启用")
    if not store.ledger_encode("reply-authorization-preflight", {}).startswith("enc1:"):
        raise AssistantError("reply_authorization_store_unencrypted", "授权设置需要加密资料库")
    previous = read(store, category, db)
    if previous["version"] != expected:
        raise AssistantError("edit_conflict", "授权范围已改变，请重新核对")
    if previous["enabled"] == enabled:
        return previous
    value = {"scope": category.describe(), "enabled": enabled, "version": secrets.token_hex(16)}
    if enabled:
        # Both transports report whole Unix seconds. Never include a second before consent.
        value["since"] = math.ceil(time.time())
    store.set_state(category.key, value, db)
    store.set_state(
        "reply-audience-audit:" + value["version"],
        {"actor": actor, "previous": previous["version"], **value},
        db,
    )
    return value


class LiveMembers:
    """Membership tokens last only for one connected session and uninterrupted membership."""

    def __init__(self):
        self.revision = None
        self.tokens = {}

    def replace(self, revision, identities):
        # The connector supplies a complete, verified {target: platform identity} snapshot.
        old = self.tokens if revision == self.revision else {}
        updated = {}
        for target, identity in identities.items():
            if not isinstance(target, str) or not target or not isinstance(identity, str) or not identity:
                self.disconnect()
                raise AssistantError("reply_directory_invalid", "平台会话名单核验失败")
            existing = old.get(target)
            updated[target] = (
                existing if existing and existing[0] == identity else (identity, secrets.token_hex(16))
            )
        self.tokens, self.revision = updated, revision

    def disconnect(self):
        self.tokens, self.revision = {}, None

    def remove(self, target):
        self.tokens.pop(target, None)

    def token(self, revision, target):
        entry = self.tokens.get(target)
        return entry[1] if entry and self.revision == revision else None


def catchup_since(policy):
    since = policy.get("since")
    if policy["enabled"] and (type(since) is not int or since <= 0):
        raise AssistantError("reply_audience_since_missing","缺少本次开启时间，请关闭后重新确认范围")
    return since if policy["enabled"] else None


def registry(store):
    with store.lock:
        if not hasattr(store, "_live_reply_members"):
            store._live_reply_members = {}
        return store._live_reply_members


def effective_version(store, scope, manual, db=None, message_time=None):
    from .reply_exclusion import gate

    category = Audience(scope.platform, scope.account_identity, scope.kind)
    policy = read(store, category, db)
    if policy["enabled"]:
        if message_time is not None and message_time < catchup_since(policy):
            return None
        live = registry(store).get(category)
        token = live.token(policy["version"], scope.target_id) if live else None
    elif manual and policy["version"]:
        token = stable_key("manual-reply", policy["version"], manual)
    else:
        token = manual
    # A single-conversation exclusion overrides both the manual pick and allow-all
    # for that one scope. Without an exclusion record the token is unchanged.
    return gate(store, scope, token, db=db, message_time=message_time)


def membership_tenure(store, scope, db=None):
    """The raw verified-membership token for one target, without the exclusion wrap.

    Follow positions are persisted in this space (``follow_inbox`` writes the same
    ``LiveMembers.token``). A single-conversation exclusion wraps the reply permit,
    which must never look like a membership change to those positions.
    """
    category = Audience(scope.platform, scope.account_identity, scope.kind)
    policy = read(store, category, db)
    live = registry(store).get(category)
    return live.token(policy["version"], scope.target_id) if policy["enabled"] and live else None


def reset(store, db):
    rows = db.execute("SELECT key FROM state WHERE key LIKE ?", (PREFIX + "%",)).fetchall()
    for row in rows:
        item = store.state(row["key"], db=db)
        category = Audience(**item["scope"])
        if item["enabled"]:
            change(store, category, False, item["version"], actor="restore", confirmed=None, db=db)
    registry(store).clear()
