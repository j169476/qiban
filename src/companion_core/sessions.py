"""Local-owner authentication with separate admin/role sessions and durable revocation."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .contracts import ActorRef
from .errors import AssistantError


def token_hash(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 256:
        return "invalid"
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise AssistantError("password_length", "密码需要 12 至 256 个字符")
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


class SessionService:
    def __init__(self, repository, authority):
        self.repository, self.authority, self.store = repository, authority, repository.store

    def setup_ticket(self):
        """Trusted installer/control bridge only; never expose as an HTTP route."""
        with self.store.lock:
            if self.store.state("web-owner"):
                raise AssistantError("owner_exists", "本机管理员已建立")
            ticket = secrets.token_urlsafe(32)
            self.store.set_state("web-setup", {"hash": token_hash(ticket), "expires": self.repository.clock() + 600,
                                              "epoch": self.repository.epoch(), "consumed": False})
        return ticket

    def _setup_state(self, ticket, db=None):
        if ticket is None or ticket == "":
            raise AssistantError("setup_ticket_missing", "请先在管理聊天调用 service_setup，并打开返回的一次性初始化链接")
        setup = self.store.state("web-setup", {}, db)
        if not setup:
            raise AssistantError("setup_ticket_unissued", "尚未签发初始化凭据，请先在管理聊天调用 service_setup")
        if not hmac.compare_digest(setup["hash"], token_hash(ticket)):
            raise AssistantError("setup_ticket_invalid", "初始化链接无效，请在管理聊天重新调用 service_setup")
        if setup["consumed"] or self.store.state("web-owner", db=db):
            raise AssistantError("setup_ticket_used", "初始化已完成，请返回首页登录；不要重复提交初始化表单")
        if setup["expires"] <= self.repository.clock():
            raise AssistantError("setup_ticket_expired", "初始化链接已过期，请在管理聊天重新调用 service_setup")
        if setup["epoch"] != self.repository.epoch():
            raise AssistantError("setup_ticket_invalid", "初始化链接已失效，请在管理聊天重新调用 service_setup")
        return setup

    def validate_setup_ticket(self, ticket):
        with self.store.lock, self.store.db() as db:
            self._setup_state(ticket, db)
        return {"valid": True, "role_started": False}

    def configure(self, ticket, password):
        self.validate_setup_ticket(ticket)
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            setup = self._setup_state(ticket, db)
            actor = ActorRef("local", secrets.token_hex(16), self.store.read("manifest")["data"]["project_id"])
            from .seed_import import claim_local_owner

            claim_local_owner(self.repository, actor, db)
            self.store.set_state("web-owner", {"actor": actor.as_dict(), "salt": salt, "hash": hashed,
                                              "algorithm": "pbkdf2-sha256-600000"}, db)
            self.store.set_state("web-setup", {**setup, "consumed": True}, db)
            self.repository.audit(actor.key, "owner_setup", "local", db=db)
        return {"configured": True, "role_started": False}

    def login(self, password, kind):
        if kind not in ("admin", "role"):
            raise AssistantError("login_denied", "登录入口无效")
        failure, token = None, None
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.repository.clock()
            attempts = self.store.state("web-login-attempts:" + kind, {"count": 0, "until": 0}, db)
            if attempts["until"] <= now:
                attempts = {"count": 0, "until": now + 300}
            if attempts["count"] >= 5:
                failure = "尝试次数过多，请稍后重试"
            else:
                owner = self.store.state("web-owner", db=db)
                try:
                    correct = bool(owner and hmac.compare_digest(owner["hash"], password_hash(password, owner["salt"])))
                except AssistantError:
                    correct = False
                if not correct:
                    self.store.set_state("web-login-attempts:" + kind, {**attempts, "count": attempts["count"] + 1}, db)
                    failure = "密码不正确或管理员尚未建立"
                else:
                    token = secrets.token_urlsafe(32)
                    actor = ActorRef(**owner["actor"])
                    db.execute("INSERT INTO governance_sessions VALUES(?,?,?,?,?,0)",
                               (token_hash(token), actor.key, kind, now + (3600 if kind == "admin" else 43200), self.repository.epoch()))
                    self.store.set_state("web-login-attempts:" + kind, {"count": 0, "until": 0}, db)
                    db.execute("DELETE FROM governance_sessions WHERE expires_at<? OR revoked=1", (now - 86400,))
        if failure:
            raise AssistantError("login_denied", failure)
        return token

    def request(self, token, kind, *, conversation="local-owner"):
        return self._recover(token_hash(token), kind, conversation=conversation)

    def _recover(self, identifier, kind, *, conversation="local-owner"):
        """Trusted service jobs only; an HTTP caller must supply the raw token."""
        with self.store.db() as db:
            row = db.execute("SELECT * FROM governance_sessions WHERE id=?", (identifier,)).fetchone()
        owner = self.store.state("web-owner")
        actor = ActorRef(**owner["actor"]) if owner else None
        if (kind not in ("admin", "role") or not row or row["purpose"] != kind or not actor
                or row["actor"] != actor.key or not self.repository.session_active(identifier)):
            raise AssistantError("login_required", "请登录当前入口")
        return self.authority.issue(actor=actor, conversation=conversation, audience="private", recipients=(actor,),
                                    purpose="management" if kind == "admin" else "reply", administrator=kind == "admin",
                                    session_id=identifier, lifetime=min(300, row["expires_at"] - self.repository.clock()))

    def actor(self, request):
        self.authority.require(request)
        actor = ActorRef(**self.store.state("web-owner")["actor"])
        if actor.key != request.actor:
            raise AssistantError("access_denied", "当前账号不是本机登录账号")
        return actor

    def logout(self, token):
        with self.store.db() as db:
            db.execute("UPDATE governance_sessions SET revoked=1 WHERE id=?", (token_hash(token),))
        return {"logged_out": True}

    def csrf(self, token, kind):
        self.request(token, kind)
        return hmac.new(token.encode(), ("csrf:" + kind).encode(), hashlib.sha256).hexdigest()

    def require_csrf(self, token, kind, supplied):
        if not isinstance(supplied, str) or not hmac.compare_digest(self.csrf(token, kind), supplied):
            raise AssistantError("csrf_denied", "表单已失效，请重新加载页面")

    def change_password(self, request, old_password, new_password):
        self.authority.require(request, administrator=True)
        salt = secrets.token_hex(16)
        hashed = password_hash(new_password, salt)
        # Reuse the same durable password attempt budget. A failed attempt commits its count.
        proof = self.login(old_password, "admin")
        try:
            with self.store.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.authority.require(request, administrator=True)
                self.request(proof, "admin")
                self._replace_password(salt, hashed, request.actor, db)
        finally:
            self.logout(proof)
        return {"changed": True, "sessions_revoked": True, "role_started": False}

    def reset_ticket(self):
        """Called only by the authenticated, bound local management bridge."""
        with self.store.lock, self.store.db() as db:
            owner = self.store.state("web-owner", {}, db)
            if not owner:
                raise AssistantError("setup_required", "尚未建立本机管理员")
            ticket = secrets.token_urlsafe(32)
            self.store.set_state("web-reset", {"hash": token_hash(ticket), "expires": self.repository.clock() + 600,
                "epoch": self.repository.epoch(), "owner_hash": owner["hash"], "consumed": False, "attempts": 0}, db)
        return ticket

    def reset_password(self, ticket, new_password):
        salt = secrets.token_hex(16)
        hashed = password_hash(new_password, salt)
        denied = False
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            reset = self.store.state("web-reset", {}, db)
            owner = self.store.state("web-owner", {}, db)
            if (not reset or not owner or reset.get("consumed") or reset.get("attempts", 0) >= 5
                    or reset["expires"] <= self.repository.clock() or reset["epoch"] != self.repository.epoch()
                    or reset["owner_hash"] != owner["hash"] or not hmac.compare_digest(reset["hash"], token_hash(ticket))):
                if reset:
                    self.store.set_state("web-reset", {**reset, "attempts": reset.get("attempts", 0) + 1}, db)
                denied = True
            else:
                self._replace_password(salt, hashed, ActorRef(**owner["actor"]).key, db)
        if denied:
            raise AssistantError("reset_denied", "密码重设链接无效、过期或已使用，请从后台管理聊天重新获取")
        return {"changed": True, "sessions_revoked": True, "role_started": False}

    def _replace_password(self, salt, hashed, actor, db):
        owner = self.store.state("web-owner", {}, db)
        self.store.set_state("web-owner", {**owner, "salt": salt, "hash": hashed}, db)
        self.store.set_state("web-reset", {}, db)
        db.execute("UPDATE governance_sessions SET revoked=1")
        self.repository.audit(actor, "password_changed", "local", db=db)
