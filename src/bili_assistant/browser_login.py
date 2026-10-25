"""Admin-owned, expiring browser login tasks. Cookies never enter MCP arguments."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from datetime import timedelta

from . import browser_login_transport as transport
from . import credentials
from .errors import AssistantError
from .storage import digest, utcnow


def _run_version(store):
    return digest(json.dumps(store.state("run"), sort_keys=True).encode("utf-8"))


def _require_paused(store):
    run = store.state("run") or {}
    if run.get("status") not in (None, "paused", "stopped"):
        raise AssistantError("login_requires_pause", "请先暂停角色再更新凭证；登录完成后不会自动恢复")


def _view(ticket):
    return {k: ticket[k] for k in (
        "ticket_id", "status", "expected_uid", "expires_at", "created_at", "result", "error",
    ) if k in ticket} | {"login_url": transport.ORIGIN + "/"}


def _end(store, ticket, status, error=None):
    transport.ticket_path(store.root, ticket["ticket_id"], ".sealed").unlink(missing_ok=True)
    ticket.update(status=status, ended_at=utcnow())
    if error:
        ticket["error"] = error
    transport.write_ticket(store.root, ticket)


def _owned(engine, ticket_id=None):
    engine.mode("admin")
    ticket_id = ticket_id or engine.store.state("browser-login:current")
    if not ticket_id:
        return None
    ticket = transport.read_ticket(engine.store.root, ticket_id)
    if ticket["owner"] != engine.identity():
        raise AssistantError("login_owner_mismatch", "此登录任务属于另一条管理聊天")
    if ticket["status"] == "saving":
        # Saving holds store.lock synchronously. Seeing this state after acquiring
        # the lock means the previous process ended without confirming completion.
        _end(engine.store, ticket, "uncertain", "credential_save_interrupted")
    if transport.expired(ticket, engine.clock()) and ticket["status"] in transport.PENDING:
        _end(engine.store, ticket, "expired", "login_ticket_expired")
    return ticket


def prepare(engine):
    engine.mode("admin")
    if not transport.supported():
        raise AssistantError("browser_login_unsupported", "浏览器凭证导入当前仅支持 Windows 本地项目")
    store = engine.store
    with store.lock:
        _require_paused(store)
        manifest = store.read("manifest")
        uid = manifest["data"]["account_uid"]
        if not uid:
            raise AssistantError("account_not_bound", "先在管理配置中指定要登录的账号 UID，或使用原有本地扫码登录")
        previous_id = store.state("browser-login:current")
        if previous_id:
            try:
                previous = transport.read_ticket(store.root, previous_id)
            except AssistantError as exc:
                if exc.code != "login_ticket_missing":
                    raise
                # Runtime cleanup may remove task metadata. Such a task cannot
                # be completed; discard only its scoped ciphertext and start anew.
                transport.ticket_path(store.root, previous_id, ".sealed").unlink(missing_ok=True)
                previous = {"status": "missing"}
            if previous["status"] in transport.PENDING:
                if (previous["owner"] == engine.identity() and not transport.expired(previous, engine.clock())
                        and previous["manifest_version"] == manifest["version"]
                        and previous["run_version"] == _run_version(store)):
                    return _view(previous)
                _end(store, previous, "superseded")
            elif previous["status"] == "saving":
                # Interrupted keyring writes need an explicit new task, not an automatic replay.
                _end(store, previous, "uncertain", "credential_save_interrupted")
        ticket = {
            "ticket_id": uuid.uuid4().hex, "status": "awaiting_browser",
            "owner": engine.identity(), "project_id": manifest["data"]["project_id"],
            "expected_uid": uid, "manifest_version": manifest["version"],
            "run_version": _run_version(store), "created_at": engine.clock().isoformat(),
            "expires_at": (engine.clock() + timedelta(minutes=15)).isoformat(),
        }
        transport.write_ticket(store.root, ticket)
        store.set_state("browser-login:current", ticket["ticket_id"])
        store.audit("credentials", {"event": "browser_login_prepared", "ticket_id": ticket["ticket_id"],
                                    "actor": engine.identity(), "uid": uid})
        return _view(ticket)


def status(engine, ticket_id=None):
    with engine.store.lock:
        ticket = _owned(engine, ticket_id)
        return _view(ticket) if ticket else {"status": "not_started"}


def cancel(engine, ticket_id):
    with engine.store.lock:
        ticket = _owned(engine, ticket_id)
        if ticket["status"] in transport.PENDING:
            _end(engine.store, ticket, "cancelled")
        return _view(ticket)


def _check_current(engine, ticket):
    engine.mode("admin")  # Recheck archive/ownership after network validation as well.
    _require_paused(engine.store)
    manifest = engine.store.read("manifest")
    if (manifest["version"] != ticket["manifest_version"]
            or manifest["data"]["account_uid"] != ticket["expected_uid"]
            or _run_version(engine.store) != ticket["run_version"]):
        raise AssistantError("login_state_changed", "登录期间账号配置或角色状态已改变；请重新发起登录")


def _replace(store, candidate):
    """Retain one previous session in keyring; compensate a failed write/readback."""
    keyring, service, key = credentials.keyring, credentials.SERVICE, credentials.key(store)
    try:
        old = keyring.get_password(service, key)
    except Exception:
        raise AssistantError("keyring_unavailable", "无法读取系统凭证库；原凭证未改动") from None
    expected = {f: getattr(candidate, f, None) for f in credentials.FIELDS}
    if old:
        try:
            same = json.loads(old) == expected
        except (ValueError, TypeError):
            same = False
        if same:
            return False
        try:
            keyring.set_password(service, key + ":previous", old)
        except Exception:
            raise AssistantError("keyring_unavailable", "无法保留原凭证备份；原凭证未改动") from None
    try:
        credentials._save(store, candidate)
        saved = credentials.load(store)
        if any(not hmac.compare_digest(str(getattr(saved, f) or ""), str(v or ""))
               for f, v in expected.items()):
            raise ValueError()
    except Exception:
        try:
            if old is None:
                if keyring.get_password(service, key) is not None:
                    keyring.delete_password(service, key)
            else:
                keyring.set_password(service, key, old)
            if keyring.get_password(service, key) != old:
                raise ValueError()
        except Exception:
            raise AssistantError("credential_save_uncertain", "凭证写入与回退未确认；保持暂停，请重新登录核查") from None
        raise AssistantError("credential_save_failed", "新凭证未能可靠保存，已恢复原凭证") from None
    return True


async def finish(engine, ticket_id):
    store = engine.store
    with store.lock:
        ticket = _owned(engine, ticket_id)
        if ticket["status"] in transport.TERMINAL or ticket["status"] == "saving":
            return _view(ticket)  # Repeated completion never repeats a keyring write.
        if ticket["status"] != "staged":
            raise AssistantError("login_not_staged", "尚未取得浏览器凭证，或另一进程正在验证；请查看登录状态")
        try:
            _check_current(engine, ticket)
        except AssistantError as exc:
            _end(store, ticket, "failed", exc.code)
            raise
        ticket.update(status="verifying")
        transport.write_ticket(store.root, ticket)
    try:
        from bilibili_api import Credential, user

        async with credentials.credential_lock(store):
            with store.lock:
                current = _owned(engine, ticket_id)
                if current["status"] != "verifying":
                    return _view(current)
                _check_current(engine, ticket)
            path = transport.ticket_path(store.root, ticket_id, ".sealed")
            if path.stat().st_size > transport.MAX_INPUT * 2:
                raise AssistantError("browser_credentials_invalid", "凭证密文大小异常")
            fields = transport.parse_secret(transport.crypt(path.read_bytes(), transport.entropy(ticket),
                                                            decrypt=True), ticket)
            candidate = Credential(**fields)
            credentials.validate_credential(candidate)
            try:
                info = await asyncio.wait_for(user.get_self_info(candidate), 20)
            except Exception:
                raise AssistantError("login_verification_failed", "网页登录账号验证失败；原凭证未变，请重新发起") from None
            if (not isinstance(info, dict) or type(info.get("mid")) is not int
                    or str(info["mid"]) != ticket["expected_uid"]
                    or not isinstance(info.get("name"), str)):
                raise AssistantError("login_account_mismatch", "平台验证账号与绑定账号不一致；原凭证未变")
            with store.lock:
                current = _owned(engine, ticket_id)
                if current["status"] != "verifying":
                    return _view(current)  # Cancel/expiry/supersession during validation revokes the write.
                _check_current(engine, ticket)
                current.update(status="saving")
                transport.write_ticket(store.root, current)
                replaced = _replace(store, candidate)
                result = {
                    "logged_in": True, "uid": str(info["mid"]), "name": info["name"][:120],
                    "replaced": replaced, "refresh_available": bool(candidate.ac_time_value),
                    "storage": "os_keyring",
                }
                store.set_state(f"credential-health:{ticket['expected_uid']}", {
                    "status": "logged_in", "at": utcnow(), "source": "browser",
                    "refresh_available": bool(candidate.ac_time_value),
                })
                current["result"] = result
                _end(store, current, "succeeded")
                store.audit("credentials", {"event": "browser_login_completed", "ticket_id": ticket_id,
                                            "actor": engine.identity(), **result})
                return _view(current)
    except BaseException as exc:
        # No raw exception (SDK/keyring/JSON) is allowed into task metadata or tool output.
        code = exc.code if isinstance(exc, AssistantError) else "browser_login_failed"
        with store.lock:
            current = transport.read_ticket(store.root, ticket_id)
            if current["status"] in ("verifying", "saving"):
                uncertain = current["status"] == "saving" and code not in (
                    "keyring_unavailable", "credential_save_failed",
                )
                _end(store, current, "uncertain" if uncertain else "failed", code)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, AssistantError):
            raise
        raise AssistantError("browser_login_failed", "网页登录导入失败；未输出敏感诊断，请查看登录状态") from None
