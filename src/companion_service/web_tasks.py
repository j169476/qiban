"""Role-only explicit task endpoints; there is no generic file or tool endpoint."""
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from companion_core.asset_transfer import _path, _validate
from companion_core.errors import AssistantError
from companion_core.governance import verify_record_versions


def image_bytes(service, request, identifier):
    tasks, store = service.tasks, service.store
    detail = tasks.detail(request, identifier)
    if detail["kind"] != "image" or detail["status"] != "completed" or not detail["result"]:
        raise AssistantError("not_found", "图片尚未完成或不可访问")
    asset = detail["result"]["payload"].get("asset")
    roots = ["task_requests/" + identifier, "task_results/" + detail["result"]["id"], "task_assets/" + str(asset)]
    lease = tasks.access.lease(request, roots)
    records = [store.read(*root.split("/", 1)) for root in roots]
    versions = {tuple(root.split("/", 1)): record["version"] for root, record in zip(roots, records, strict=True)}
    result, record = records[1]["data"], records[2]["data"]
    if record["task_id"] != identifier or result["task_id"] != identifier or result["payload"].get("asset") != asset:
        raise AssistantError("access_denied", "图片与任务的来源不匹配")
    path = _path(store, "assets/tasks/" + record["filename"])
    try:
        with path.open("rb") as source:
            raw = source.read(10_000_001)
    except OSError:
        raise AssistantError("asset_missing", "任务图片暂不可读取") from None
    _validate(raw, record["sha256"])
    tasks.access.revalidate(lease)
    verify_record_versions(store, versions)
    return raw, {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]


def routes(service, authentication, data, result):
    async def tasks(request):
        token, _ = authentication(request)
        context, actor = service.task_context(token)
        if request.method == "GET":
            identifier = request.query_params.get("id")
            value = (await run_in_threadpool(service.tasks.detail, context, identifier) if identifier else
                     await run_in_threadpool(service.tasks.listing, context))
            return result(value)
        service.sessions.require_csrf(token, "role", request.headers.get("x-csrf-token"))
        args = await data(request)
        if set(args) != {"kind", "arguments", "nonce"}:
            raise AssistantError("task_request", "任务不能指定身份、权限或执行工具")
        value = await run_in_threadpool(service.tasks.submit, context, actor, args["kind"], args["arguments"], args["nonce"])
        service.notify()
        return result(value, 202)

    async def control(request):
        token, _ = authentication(request)
        service.sessions.require_csrf(token, "role", request.headers.get("x-csrf-token"))
        context, _ = service.task_context(token)
        args = await data(request)
        if set(args) != {"id", "action", "revision"} or not all(isinstance(value, str) for value in args.values()):
            raise AssistantError("task_request", "任务控制字段无效")
        value = await run_in_threadpool(service.tasks.control, context, args["id"], args["action"], args["revision"])
        service.notify()
        return result({"status": value["status"], "revision": value["revision"]})

    async def image(request):
        token, _ = authentication(request)
        context, _ = service.task_context(token)
        raw, media = await run_in_threadpool(image_bytes, service, context, request.path_params["identifier"])
        return Response(raw, media_type=media)  # LocalBoundary supplies no-store and nosniff.

    return [Route("/tasks", tasks, methods=["GET", "POST"]), Route("/task-control", control, methods=["POST"]),
            Route("/task-image/{identifier}", image)]
