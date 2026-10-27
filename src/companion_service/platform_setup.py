"""Authenticated local setup jobs. Configuration and discovery never grant replies."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from companion_core.contracts import stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import require
from companion_core.storage import atomic_write


def merge_enrollment(old, fresh):
    """Append targets while retaining old aliases, epochs and committed source positions."""
    from companion_wechat.receiver import _snapshot

    fresh = _snapshot(fresh)
    if old is None:
        return fresh
    old = _snapshot(old)
    before, after = old["enrollment"], fresh["enrollment"]
    require(all(before[k] == after[k] for k in ("account_id", "account_directory_id", "self_username")),
            "wechat_account_mismatch")
    aliases = {v["username"]: k for k, v in before["targets"].items()}
    rename = {k: aliases.get(v["username"], k) for k, v in after["targets"].items()}
    targets = {rename[k]: v for k, v in after["targets"].items()}
    require(all(targets.get(k) == v for k, v in before["targets"].items()), "wechat_binding_changed")
    sources = {}
    for value in fresh["checkpoint"]["sources"].values():
        value = {**value, "target_ref": rename[value["target_ref"]]}
        key = value["shard"] + "|" + value["target_ref"]
        prior = old["checkpoint"]["sources"].get(key)
        if prior is not None:
            require(prior["salt"] == value["salt"], "wechat_source_generation_changed")
            value = prior
        sources[key] = value
    require(set(old["checkpoint"]["sources"]) <= set(sources), "wechat_source_generation_changed")
    return _snapshot({"enrollment": {**before, "targets": targets},
                      "checkpoint": {**old["checkpoint"], "sources": sources}})


class PlatformSetup:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.jobs = {}
        self.running = set()
        self.active_tasks = set()

    @property
    def project(self):
        return Path(self.service.host.project).resolve()

    def options(self, platform):
        if platform == "qq":
            from companion_qq.config import load

            return load(self.project)
        from companion_wechat.module import load_options

        return load_options(self.project)

    def revision(self, platform):
        paths = [self.project / f".dev/{platform}/module.json"]
        if platform == "wechat":
            try:
                options = self.options(platform)
            except Exception:
                options = None
            if options:
                paths.extend(options[k] for k in ("locations", "targets"))
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path).encode())
            digest.update(path.read_bytes() if path.is_file() else b"missing")
        return digest.hexdigest()

    def status(self, platform):
        require(platform in ("qq", "wechat"))
        try:
            options = self.options(platform)
        except Exception:
            return {"platform": platform, "configured": False, "version": self.revision(platform),
                    "running_job": platform in self.running, "job": self.store.state("platform-setup:" + platform),
                    "role_started": False, "configuration_error": True,
                    "instructions": "现有配置格式无效，请修正本机配置文件后重新检查。不会启动平台或发送消息。"}
        result = {"platform": platform, "configured": options is not None, "version": self.revision(platform),
                  "running_job": platform in self.running, "job": self.store.state("platform-setup:" + platform),
                  "role_started": False}
        if platform == "qq":
            from companion_qq.config import management_url, token

            present = False
            if options:
                try:
                    token(self.project, options["account"])
                    present = True
                except Exception:
                    pass
            result.update(configuration=options, token_saved=present,
                          installation_url="https://napneko.github.io/guide/boot/Shell",
                          login_url=management_url(self.project),
                          instructions="先在 NapCat 登录机器人 QQ，启用本机 HTTP 与正向 WebSocket，设置相同 Token，然后核验。普通 QQ 登录不能代替桥接登录。")
        else:
            files = {key: bool(options and options[key].is_file()) for key in ("database_python", "native_dll", "locations", "targets")}
            result.update(files=files, instructions="在本机微信登录机器人账号。填写账号数据目录及目标内部 ID；核验前保持客户端运行。发送阶段需要最小化客户端。")
            if options and files["locations"] and files["targets"]:
                from companion_wechat.db_cli import load_config

                try:
                    result["configuration"] = load_config(options["locations"], options["targets"])
                except Exception:
                    result["configuration_error"] = True
                    result["instructions"] = "微信目标或数据目录配置无效，请修正本机配置文件后重新检查。"
        return result

    def operation(self, request, name, arguments):
        self.service.admin.authority.require(request, administrator=True)
        require(isinstance(arguments, dict) and arguments.get("platform") in ("qq", "wechat"))
        platform = arguments["platform"]
        if name == "platform_setup_status":
            require(set(arguments) == {"platform"})
            return self.status(platform)
        require(name == "platform_setup_run" and set(arguments) == {"platform", "action", "settings", "expected_version"})
        action = arguments["action"]
        require(action in ("save", "probe", "prepare") and isinstance(arguments["settings"], dict))
        require(action == "save" or not arguments["settings"])
        require(self.service.loop is not None and not self.service.shutdown.is_set(), "setup_service_not_ready")
        with self.service.state_lock:
            require(platform not in self.running, "setup_busy")
            require(self.revision(platform) == arguments["expected_version"], "edit_conflict")
            self.running.add(platform)
            self.store.set_state("platform-setup:" + platform, {"status": "running", "action": action, "started": time.time()})
            self.jobs[platform] = asyncio.run_coroutine_threadsafe(self._job(
                request, platform, action, arguments["settings"], arguments["expected_version"]), self.service.loop)
        return {"accepted": True, "platform": platform, "role_started": False}

    async def _job(self, request, platform, action, settings, expected_version):
        task = asyncio.current_task()
        self.active_tasks.add(task)
        result = None
        try:
            await self.service.platform_runs.detach(platform)
            require(self.revision(platform) == expected_version, "edit_conflict")
            if action == "save":
                self.save(platform, settings)
            elif action == "prepare":
                result = await self.prepare(platform)
            else:
                self.service.require_runtime()
                result = await self.probe(platform)
            self.service.admin.repository.audit(request.actor, "platform_setup_" + action, platform)
            self.store.set_state("platform-setup:" + platform, {"status": "completed", "action": action,
                "finished": time.time(), "result": result, "messages_sent": 0, "role_started": False})
        except asyncio.CancelledError:
            self.store.set_state("platform-setup:" + platform, {"status": "interrupted", "action": action})
            raise
        except Exception as exc:
            self.store.set_state("platform-setup:" + platform, {"status": "failed", "action": action,
                "error": getattr(exc, "code", "setup_failed"), "finished": time.time(), "role_started": False,
                "stage": self.store.state("platform-setup:" + platform, {}).get("stage")})
        finally:
            try:
                if not self.service.shutdown.is_set():
                    self.service.platform_runs.attach_configured(platform)
            except Exception as exc:
                record = self.store.state("platform-setup:" + platform, {})
                self.store.set_state("platform-setup:" + platform, {**record, "status": "failed", "action": action,
                    "error": record.get("error", getattr(exc, "code", "setup_reload_failed")),
                    "reload_error": getattr(exc, "code", "setup_reload_failed"), "role_started": False})
            finally:
                self.running.discard(platform)
                self.active_tasks.discard(task)

    def save(self, platform, settings):
        if platform == "qq":
            import keyring

            from companion_qq.config import credential_key, validate

            fields = {"account", "private_targets", "groups", "http", "ws", "token"}
            require(fields <= set(settings) and not set(settings) - fields - {"launch"})
            secret = settings["token"]
            require(isinstance(secret, str) and (not secret or 16 <= len(secret) <= 512))
            value = validate({"version": 1, **{k: v for k, v in settings.items() if k != "token"}})
            old = self.options(platform)
            if old and old["account"] == value["account"]:
                if "launch" not in settings and "launch" in old:
                    value["launch"] = old["launch"]
                for key in ("private_targets", "groups"):
                    value[key] = list(dict.fromkeys(old[key] + value[key]))
            if secret:
                keyring.set_password("local-companion-qq", credential_key(self.project, value["account"]), secret)
            atomic_write(self.project / ".dev/qq/module.json", json.dumps(value).encode())
            return
        require(set(settings) == {"account_dirs", "targets"})
        from companion_wechat.db_cli import load_config
        from companion_wechat.db_probe import validate_config

        old = self.options(platform)
        targets = settings["targets"]
        require(isinstance(targets, list))
        config = {"account_dirs": settings["account_dirs"], "target_hints": targets}
        if targets:
            validate_config(config)
        if old and old["locations"].is_file() and old["targets"].is_file():
            previous = load_config(old["locations"], old["targets"])
            inherited = previous.get("target_hints")
            if inherited is None:
                # Legacy groups were configured by display name. Migrate only the
                # already verified identity, never reinterpret that name as an ID.
                from companion_wechat.collector import collector_account

                account = collector_account(self.service.installation_id)
                snapshot = self.store.state("wechat-db-enrollment:" + stable_key("logical-account", account))
                require(snapshot is not None, "wechat_legacy_enrollment_required")
                inherited = [{"kind": v["kind"], "hint": v["username"]}
                             for v in snapshot["enrollment"]["targets"].values()]
                # Older pages echoed legacy names into append-only inputs.
                # These known entries are already represented by verified IDs.
                echoes = {("private", previous["private_hint"]), ("group", previous["group_hint"])}
                targets = [v for v in targets if (v["kind"], v["hint"]) not in echoes]
            targets = list({(v["kind"], v["hint"]): v for v in inherited + targets}.values())
        validate_config({**config, "target_hints": targets})
        base = ".dev/wechat/"
        # Commit the module pointer last so an interrupted write cannot combine
        # locations from one revision with target intent from another.
        revision = base + "setup-configs/" + uuid.uuid4().hex + "/"
        atomic_write(self.project / (revision + "locations.json"), json.dumps({"account_dirs": settings["account_dirs"]}).encode())
        atomic_write(self.project / (revision + "targets.json"), json.dumps({"automatic_sending_enabled": False, "targets": targets}).encode())
        value = {"version": 2, "locations": revision + "locations.json", "targets": revision + "targets.json",
                 "database_python": str(old["database_python"]) if old else ".dev/wechat-db-venv/Scripts/python.exe",
                 "native_dll": str(old["native_dll"]) if old else base + "native-module-build/xln_native_module.dll"}
        atomic_write(self.project / (base + "module.json"), json.dumps(value).encode())

    async def command(self, args, timeout=600):
        from companion_codex.process_scope import ProcessScope

        scope, process = ProcessScope(), None
        try:
            process = subprocess.Popen(args, cwd=self.project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | (4 if os.name == "nt" else 0),
                start_new_session=os.name != "nt")
            scope.attach(process)
            scope.resume()
            end = time.monotonic() + timeout
            while process.poll() is None:
                require(time.monotonic() < end, "setup_timeout")
                await asyncio.sleep(.2)
            require(process.returncode == 0, "setup_dependency_failed")
        finally:
            scope.close()

    def prepare_stage(self, platform, stage):
        key = "platform-setup:" + platform
        self.store.set_state(key, {**self.store.state(key, {}), "stage": stage})

    async def dependencies_ready(self, python, platform):
        checks = ("import websockets; from importlib.metadata import version; "
                  "assert 15 <= int(version('websockets').split('.')[0]) < 17" if platform == "qq" else
                  "import sqlcipher3, cryptography, zstandard; from importlib.metadata import version; "
                  "assert version('sqlcipher3') == '0.6.4'; "
                  "assert 46 <= int(version('cryptography').split('.')[0]) < 51; "
                  "assert version('zstandard') == '0.25.0'")
        try:
            await self.command([str(python), "-c", checks], timeout=30)
            return True
        except (AssistantError, OSError):
            return False

    async def prepare(self, platform):
        if platform == "qq":
            self.prepare_stage(platform, "dependencies_check")
            if await self.dependencies_ready(sys.executable, platform):
                return {"dependencies": "reused"}
            self.prepare_stage(platform, "dependencies_install")
            await self.command([sys.executable, "-m", "pip", "install", "websockets>=15,<17"])
            require(await self.dependencies_ready(sys.executable, platform), "setup_dependency_failed")
            return {"dependencies": "installed"}
        from companion_wechat.native.bridge import validate_build

        options = self.options(platform)
        python = options["database_python"] if options else self.project / ".dev/wechat-db-venv/Scripts/python.exe"
        dll = options["native_dll"] if options else self.project / ".dev/wechat/native-module-build/xln_native_module.dll"
        if not python.is_file():
            self.prepare_stage(platform, "python_create")
            await self.command([sys.executable, "-m", "venv", str(python.parent.parent)])
        self.prepare_stage(platform, "dependencies_check")
        reused = await self.dependencies_ready(python, platform)
        if not reused:
            self.prepare_stage(platform, "dependencies_install")
            await self.command([str(python), "-m", "pip", "install", "-r", str(self.project / "requirements/wechat-db.txt")])
            require(await self.dependencies_ready(python, platform), "setup_dependency_failed")
        self.prepare_stage(platform, "native_check")
        try:
            validate_build(dll)
            native = "reused"
        except Exception:
            self.prepare_stage(platform, "native_build")
            await self.command(["cmd.exe", "/c", str(self.project / "scripts/build_wechat_native.cmd")])
            self.prepare_stage(platform, "native_verify")
            try:
                validate_build(dll)
            except Exception:
                raise AssistantError("setup_native_invalid", "桥接构建结果与当前配置不匹配，请检查构建目录") from None
            native = "built"
        return {"dependencies": "reused" if reused else "installed", "native": native}

    async def probe(self, platform):
        if platform == "qq":
            from companion_qq.module import QQModule

            task = asyncio.create_task(asyncio.to_thread(QQModule(self.store, self.project).probe))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The bounded HTTP probe owns a DB write. Finish it before service
                # shutdown can close the store; cancellation cannot stop a thread.
                await task
                raise
        from companion_wechat.collector import collector_account
        from companion_wechat.db_cli import load_config
        from companion_wechat.durable_inbox import DurableInbox, _checkpoint_json
        from companion_wechat.receiver_transport import SubprocessTransport

        options = self.options(platform)
        require(options is not None, "wechat_module_configuration")
        account = collector_account(self.service.installation_id)
        pointer_key = "wechat-db-enrollment:" + stable_key("logical-account", account)
        source_key = "database-source:" + stable_key("logical-account", account)
        pointer = self.store.state(pointer_key)
        config = load_config(options["locations"], options["targets"])
        if pointer:
            from companion_wechat.module import ReconnectingTransport

            config["client_pid"], _born = await asyncio.to_thread(
                ReconnectingTransport._select, pointer["enrollment"]["self_username"])
        worker = SubprocessTransport(python=options["database_python"])
        try:
            snapshot = await worker.request("open", config=config,
                                            enrollment=None, checkpoint=None)
        finally:
            await worker.close()
        with self.store.lock:
            require(self.store.state(pointer_key) == pointer, "edit_conflict")
            inbox = DurableInbox(self.store, snapshot["enrollment"]["account_id"])
            previous = inbox.checkpoint(source_key)
            merged = merge_enrollment(previous or pointer, snapshot)
            _checkpoint_json(merged)
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                require(self.store.state(pointer_key, None, db) == pointer, "edit_conflict")
                require(self.store.state(inbox._checkpoint_key(source_key), None, db) == previous, "edit_conflict")
                self.store.set_state(pointer_key, merged, db)
                self.store.set_state(inbox._checkpoint_key(source_key), merged, db)
        return {"verified": True, "targets": len(merged["enrollment"]["targets"]), "messages_sent": 0, "role_started": False}

    async def close(self):
        tasks = list(self.active_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
