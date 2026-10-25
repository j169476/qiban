"""Authenticated loopback OneBot v11. Reads may reconnect; sends never retry."""
import json
import re

import httpx

from companion_core.errors import AssistantError
from companion_core.reply_authorization import require

from .config import qq_id, validate


def message_id(value):
    require(type(value) in (str, int) and re.fullmatch(r"-?[0-9]{1,20}", str(value)) is not None,
            "qq_message_invalid")
    return str(int(value))


class OneBot:
    def __init__(self, options, secret):
        self.options = validate(options)
        require(isinstance(secret, str) and 16 <= len(secret) <= 512, "qq_token_required")
        self.secret = secret
        self.http = httpx.Client(timeout=httpx.Timeout(10, connect=2), trust_env=False, follow_redirects=False)

    def call(self, action, params=None):
        require(action in {"get_login_info", "get_status", "get_friend_list", "get_group_list", "get_group_member_info",
                           "get_version_info", "send_private_msg", "send_group_msg"}, "qq_action_unsupported")
        try:
            with self.http.stream("POST", self.options["http"] + "/" + action, json=params or {},
                                  headers={"Authorization": "Bearer " + self.secret}) as response:
                require(response.status_code == 200, "qq_provider_unavailable")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    require(len(body) <= 1048576, "qq_response_oversize")
            value = json.loads(body)
            require(isinstance(value, dict) and value.get("status") == "ok" and value.get("retcode") == 0,
                    "qq_provider_rejected")
            return value.get("data")
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("qq_provider_unavailable", "QQ协议端不可用，请检查本机连接与登录") from None

    def identity(self):
        identity = self.call("get_login_info")
        require(isinstance(identity, dict) and qq_id(identity.get("user_id")) == self.options["account"], "qq_account_mismatch")
        state = self.call("get_status")
        require(isinstance(state, dict) and state.get("online") is True and state.get("good") is True, "qq_login_required")
        return identity

    def probe(self):
        identity = self.identity()
        friends, groups = self.call("get_friend_list"), self.call("get_group_list")
        require(isinstance(friends, list) and isinstance(groups, list), "qq_catalog_invalid")
        friend_ids = {qq_id(x["user_id"]) for x in friends}
        group_ids = {qq_id(x["group_id"]) for x in groups}
        require(set(self.options["private_targets"]) <= friend_ids and set(self.options["groups"]) <= group_ids,
                "qq_target_unverified")
        for group in self.options["groups"]:
            member = self.call("get_group_member_info", {"group_id": int(group), "user_id": int(self.options["account"])})
            require(isinstance(member, dict) and qq_id(member.get("user_id")) == self.options["account"]
                    and qq_id(member.get("group_id")) == group, "qq_target_unverified")
        self.identity()
        return {"account": self.options["account"], "account_label": str(identity.get("nickname", ""))[:100],
                "private_targets": self.options["private_targets"], "groups": self.options["groups"]}

    async def connect(self):
        try:
            from websockets.asyncio.client import connect

            return await connect(self.options["ws"], additional_headers={"Authorization": "Bearer " + self.secret},
                                 proxy=None, open_timeout=5, close_timeout=2, ping_interval=15, ping_timeout=15,
                                 max_size=262144, max_queue=32)
        except Exception:
            raise AssistantError("qq_provider_unavailable", "QQ事件连接尚未就绪") from None

    def send(self, scope, text):
        require(scope.platform == "qq" and scope.account_identity == self.options["account"]
                and scope.target_id in self.options["private_targets" if scope.kind == "private" else "groups"],
                "qq_target_unverified")
        require(isinstance(text, str) and text.strip() and len(text) <= 1000, "qq_text_invalid")
        action = "send_private_msg" if scope.kind == "private" else "send_group_msg"
        target = "user_id" if scope.kind == "private" else "group_id"
        # Structured text cannot be reinterpreted as CQ commands or mention-all.
        result = self.call(action, {target: int(scope.target_id), "message": [{"type": "text", "data": {"text": text}}]})
        require(isinstance(result, dict), "qq_send_unconfirmed")
        return message_id(result.get("message_id"))

    def close(self):
        self.secret = ""
        self.http.close()
