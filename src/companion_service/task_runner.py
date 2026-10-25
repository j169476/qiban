"""Private task workflows. Codex owns model/tool execution; platform reads are injected."""
import hashlib
import io
from zoneinfo import ZoneInfo

from PIL import Image

from companion_codex.task_tools import PROMPT, RESULT, checked_image, checked_web
from companion_core import task_learning
from companion_core.access import RecordPolicy
from companion_core.asset_transfer import _validate
from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.storage import atomic_write


class TaskRunner:
    def __init__(self, service):
        self.service = service

    async def __call__(self, task, previous, *, request, guard):
        from companion_core.baseline import load

        bundle = load(self.service.store)

        def check():
            guard()
            if bundle:
                bundle.check()

        check()
        result = await self._run(task, previous, request=request, guard=check, baseline=bundle.context if bundle else {})
        check()
        if bundle:
            result["source_pins"] = list({**dict(result.get("source_pins", ())), **dict(bundle.pins)}.items())
        return result

    async def _run(self, task, previous, *, request, guard, baseline):
        core, store = self.service.core, self.service.store
        guard()
        if not core.remaining_model_calls():
            raise AssistantError("model_quota", "当日模型额度已用完")

        async def generate(prompt, context, schema, images=(), *, native=None, extra_guard=None):
            def check():
                guard()
                if extra_guard:
                    extra_guard()
            check()
            if native:
                settings = store.read("companion")["data"]
                if not settings.get("native_tools_enabled", True):
                    raise AssistantError("native_disabled", "原生工具已关闭")
                day = core.clock().astimezone(ZoneInfo(store.read("policy")["data"]["timezone"])).date().isoformat()
                account = store.read("manifest")["data"]["account_uid"]
                key = f"native:{account}:{day}:{native}"
                with store.lock, store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    check()
                    limit = settings.get(f"native_{native}_daily_limit", 3 if native == "web" else 1)
                    used = store.state(key, 0, db)
                    if used >= limit:
                        raise AssistantError("native_quota", "今日原生工具额度已用完")
                    store.set_state(key, used + 1, db)
            return await core.model(prompt, {**context, **baseline}, schema, images, native=native, guard=check)

        async def research(material):
            response = await generate(PROMPT, {"kind": "web", "request": material}, RESULT, native="web")
            return checked_web(response)

        kind, args = task["kind"], task["arguments"]
        if kind == "learn":
            return await task_learning.step(args, previous, generate, research)
        if kind == "web":
            sources = await research(args["prompt"])
            return {"phase": "done", "complete": True, "payload": {"sources": sources,
                "evidence_scope": "tool_open_verified_model_excerpt_not_independently_fetched"},
                "summary": "\n\n".join(item["note"] for item in sources)[:12000]}
        if kind == "video":
            runtimes = getattr(self.service, "platform_runs", None)
            cores = tuple(runtimes.cores.values()) if runtimes is not None else (core,)
            readers = [(platform_core, adapter) for platform_core in cores
                       for adapter in platform_core.platforms.all() if hasattr(adapter, "read_private_task")]
            adapters = [adapter for _, adapter in readers]
            if len(adapters) != 1:
                raise AssistantError("task_connector", "需要唯一已绑定的视频读取连接器")
            platform_core, adapter = readers[0]
            if store.state(platform_core.run_key, {}).get("status") != "active":
                raise AssistantError("platform_not_running", "请先单独启动 B站模块，再提交视频任务")
            platform_run = platform_core.guard()

            def read_guard():
                guard()
                platform_core.guard(platform_run["generation"])

            return await adapter.read_private_task(task, previous, authority=self.service.admin.authority,
                                                  request=request, guard=read_guard, generate=generate)
        response = await generate(PROMPT, {"kind": "image", "request": args["prompt"]}, RESULT, native="image")
        path = checked_image(response)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        _validate(raw, digest)
        with Image.open(io.BytesIO(raw)) as image:
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
        # The generated file is never served by path or mounted as a static folder.
        filename = hashlib.sha256((task["id"] + digest).encode()).hexdigest() + suffix
        target = store.root / "assets" / "tasks" / filename
        if any(parent.is_symlink() for parent in (target, target.parent, target.parent.parent)):
            raise AssistantError("asset_path", "任务图片目录不能为链接")
        asset_id = stable_key("task-image", task["id"], digest)
        root = "task_requests/" + task["id"]
        repository = self.service.admin.repository
        with store.lock, store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            guard()
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, raw)
            old = store.read("task_assets", asset_id) if store.exists("task_assets", asset_id) else None
            store.write("task_assets", {"schema_version": 1, "id": asset_id, "task_id": task["id"],
                "filename": filename, "sha256": digest, "locked": False}, old["version"] if old else None,
                "explicit-image", asset_id)
            current = repository.policy("task_assets/" + asset_id)
            root_version = repository.policy(root).version
            repository.set_policy(RecordPolicy(current.resource, current.version, "conversation",
                frozenset((request.actor,)), request.conversation, "private", (root,),
                source_versions=((root, root_version),)), current.version, "explicit-image")
        return {"phase": "done", "complete": True, "payload": {"asset": asset_id},
                "source_pins": [(current.resource, current.version)], "summary": "图片已生成，可以在本机会话查看。"}
