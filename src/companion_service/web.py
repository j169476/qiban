"""Server-rendered local UI. Role and management apps use distinct origins."""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, is_dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from companion_core.errors import AssistantError

from .admin import Management
from .security import LocalBoundary

ROOT = Path(__file__).parent
templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))


def serializable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError("unsupported response value")


def result(value, status=200):
    return Response(json.dumps(value, ensure_ascii=False, default=serializable), status_code=status,
                    media_type="application/json")


def create_app(service, *, kind, origin, counterpart):
    if kind not in ("admin", "role"):
        raise ValueError("Unknown surface")
    cookie = "companion_" + kind
    management = Management(service)

    def authentication(request):
        token = request.cookies.get(cookie, "")
        return token, service.sessions.request(token, kind)

    async def data(request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise AssistantError("invalid_body", "请求需要 JSON 格式")
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise AssistantError("invalid_body", "请求内容无效") from None
        if not isinstance(value, dict):
            raise AssistantError("invalid_body", "请求需要对象")
        return value

    async def home(request):
        try:
            token, user = authentication(request)
            csrf = service.sessions.csrf(token, kind)
        except AssistantError:
            user, csrf = None, ""
        html = templates.get_template("index.html").render(kind=kind, authenticated=bool(user), csrf=csrf,
                 counterpart=counterpart, configured=bool(service.store.state("web-owner")),
                 nonce=secrets.token_hex(16), kinds=list(service.store.profile.schemas))
        return HTMLResponse(html)

    async def setup(request):
        if kind != "admin":
            raise AssistantError("access_denied", "请从管理入口初始化")
        args = await data(request)
        return result(await run_in_threadpool(service.sessions.configure, args.get("ticket"), args.get("password")))

    async def manual(request):
        return HTMLResponse(templates.get_template("manual.html").render())

    async def login(request):
        args = await data(request)
        token = await run_in_threadpool(service.sessions.login, args.get("password"), kind)
        response = result({"logged_in": True, "role_started": False})
        # Cookies are host-scoped, not port-scoped. The matching purpose and exact
        # Origin check are mandatory even with SameSite=Strict.
        response.set_cookie(cookie, token, httponly=True, samesite="strict", max_age=3600 if kind == "admin" else 43200)
        return response

    async def password(request):
        args = await data(request)
        if request.url.path == "/password-reset":
            value = await run_in_threadpool(service.sessions.reset_password, args.get("ticket"), args.get("password"))
        else:
            token, user = authentication(request)
            service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
            value = await run_in_threadpool(service.sessions.change_password, user, args.get("old_password"), args.get("password"))
        await run_in_threadpool(service._pause, "password_changed")
        response = result(value)
        response.delete_cookie(cookie)
        return response

    async def logout(request):
        token, user = authentication(request)
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        response = result(service.sessions.logout(token))
        if kind == "role":
            await run_in_threadpool(service.tasks.stop, "session_ended", session=user.session_id)
        response.delete_cookie(cookie)
        return response

    async def status(request):
        authentication(request)
        value = service.status()
        from companion_core import action_budget, budget

        value["quotas"] = {
            "model": budget.status(service.store, service.store.read("policy")["data"], service.core.clock()),
            "local": {key: item for key, item in action_budget.status(service.core, service.local).items()
                      if key != "key"},
        }
        if kind == "admin":
            value["error"] = service.store.state("service-last-error", {})
            value["backup"] = service.backups.status()
            conversation = service.local.conversation()
            value["compression"] = service.compression.status(service.local.account, "local", conversation) if conversation else {}
        return result(value)

    async def messages(request):
        if kind != "role":
            raise AssistantError("access_denied", "请从角色入口对话")
        token, _ = authentication(request)
        if request.method == "GET":
            return result(await run_in_threadpool(service.local.history, token))
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        args = await data(request)
        if not {"text", "nonce"} <= set(args) or set(args) - {"text", "nonce", "images"}:
            raise AssistantError("invalid_message", "消息不能指定发送人或权限")
        if "images" in args and not isinstance(args["images"], list):
            raise AssistantError("invalid_image", "图片需要上传后的标识列表")
        reply = await run_in_threadpool(service.local.post, token, args["text"], args["nonce"], args.get("images"))
        service.wakeup.set()
        return result(reply, 202)

    async def conversation(request):
        token, _ = authentication(request)
        if request.method == "POST":
            service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
            args = await data(request)
            if set(args) - {"before", "after", "query", "limit", "known", "known_versions"}:
                raise AssistantError("invalid_history", "历史查询参数无效")
            return result(await run_in_threadpool(service.local.page, token, **args))
        return result(await run_in_threadpool(service.local.queue, token))

    async def cancel_message(request):
        token, _ = authentication(request)
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        args = await data(request)
        return result(await run_in_threadpool(service.local.cancel, token, args["id"]))

    async def input_image(request):
        token, _ = authentication(request)
        user = service.local.request(token)
        if request.method == "GET":
            blob = await run_in_threadpool(service.local.images.read, user, request.query_params.get("id", ""))
            return Response(blob, media_type="image/png")
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        if request.method == "DELETE":
            return result(await run_in_threadpool(service.local.images.discard, user, request.query_params.get("id", "")))
        blob = await request.body()
        return result(await run_in_threadpool(service.local.images.upload, user, blob), 201)

    async def records(request):
        _, user = authentication(request)
        if kind != "admin":
            raise AssistantError("access_denied", "需要管理入口")
        key = request.query_params.get("kind", "memories")
        identifier = request.query_params.get("id")
        if "id" in request.query_params:
            return result(await run_in_threadpool(management.detail, user, key, identifier or None))
        return result(await run_in_threadpool(management.listing, user, key, after=request.query_params.get("after", "")))

    async def actors(request):
        _, user = authentication(request)
        return result(await run_in_threadpool(management.actors, user))

    async def operate(request):
        token, user = authentication(request)
        if kind != "admin":
            raise AssistantError("access_denied", "需要管理入口")
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        args = await data(request)
        value = await run_in_threadpool(management.operation, user, args["operation"], args.get("arguments", {}))
        return result(value)

    async def export(request):
        token, user = authentication(request)
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        args = await data(request)
        text = await run_in_threadpool(management.export, user, args["kind"], args.get("id"))
        return Response(text, media_type="application/yaml", headers={"content-disposition": 'attachment; filename="record.yaml"'})

    async def control(request):
        # CLI bridge only. Browser cookies and login passwords are irrelevant.
        args = await data(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or request.headers.get("origin"):
            raise AssistantError("bridge_denied", "控制入口需要独立本机凭据")
        value = await service.control_async(header[7:], args["thread"], args["operation"], args.get("arguments", {}))
        return result(value)

    async def recovery(request):
        token, user = authentication(request)
        service.admin.authority.require(user, administrator=True)
        service.sessions.require_csrf(token, kind, request.headers.get("x-csrf-token"))
        args = await data(request)
        if not hasattr(service, "recovery_export"):
            raise AssistantError("recovery_unavailable", "此测试实例没有系统资料密钥")
        blob = await run_in_threadpool(service.recovery_export, args.get("password"))
        service.admin.repository.audit(user.actor, "wrapped_key_export", "database-key")
        service.store.set_state("recovery-key-exported", True)
        return Response(blob, media_type="application/octet-stream", headers={"content-disposition": 'attachment; filename="recovery-key.cckey"'})

    async def health(request):
        return result({"id": getattr(service, "installation_id", "test"), "service": "ready"})

    async def failure(request, exc):
        code = 401 if exc.code == "login_required" else 409 if exc.code in {"edit_conflict", "version_conflict"} else 403
        return result(exc.as_dict(), code)

    async def invalid(request, exc):
        return JSONResponse({"error": "invalid_request", "message": "请求字段无效"}, status_code=400)

    routes = [Route("/", home), Route("/manual", manual), Route("/setup", setup, methods=["POST"]), Route("/login", login, methods=["POST"]),
              Route("/logout", logout, methods=["POST"]), Route("/status", status),
              Route("/messages", messages, methods=["GET", "POST"])]
    if kind == "admin":
        routes += [Route("/healthz", health), Route("/records", records), Route("/actors", actors), Route("/operate", operate, methods=["POST"]),
                   Route("/password", password, methods=["POST"]), Route("/password-reset", password, methods=["POST"]),
                   Route("/recovery-key", recovery, methods=["POST"]),
                   Route("/export", export, methods=["POST"]), Route("/control", control, methods=["POST"])]
    else:
        from .web_tasks import routes as task_routes

        routes += task_routes(service, authentication, data, result)
        routes += [Route("/conversation", conversation, methods=["GET", "POST"]),
                   Route("/cancel-message", cancel_message, methods=["POST"]),
                   Route("/input-image", input_image, methods=["GET", "POST", "DELETE"])]
    app = Starlette(routes=routes, exception_handlers={AssistantError: failure, KeyError: invalid, TypeError: invalid, ValueError: invalid})
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return LocalBoundary(app, origin=origin, upload_path="/input-image" if kind == "role" else None)
