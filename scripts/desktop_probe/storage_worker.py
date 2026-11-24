"""Encrypted store, local administration and account broker; no model turns or platform traffic.

The worker opens exactly the generation its installation root selects. A root that has an
``installation.json`` is authoritative: the configured database name, store directory and
credential slot must be the ones ``installation_generation.load_selection`` derives from
that journal, so a startup can never read the original (empty) database after a restore
selected another generation. A hand-written development configuration without a root
journal stays readable, which is what the isolated probe runs use.

Only the runtime login and the selected generation's data key reach the normal store; the
cluster's bootstrap credential stays in this module's maintenance paths.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from credential_vault import VAULT, VaultCredentials

from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore

IDENTIFIER = re.compile(r"[a-f0-9]{32}")


def credentials(installation, keyref=None):
    """Data key and runtime password of one credential slot, keyed by an exact identifier."""
    keyref = installation if keyref is None else keyref
    if not IDENTIFIER.fullmatch(installation) or not IDENTIFIER.fullmatch(keyref):
        raise AssistantError("storage_config", "Invalid installation identity")
    try:
        raw = VaultCredentials().get_password(VAULT, keyref)
    except Exception:
        raise AssistantError("storage_vault_unavailable", "Credential vault unavailable") from None
    if raw is None:
        raise AssistantError("storage_vault_missing", "Installation credentials missing")
    try:
        value = json.loads(raw)
        key = base64.b64decode(value["data_key"], validate=True)
        if len(key) != 32 or not isinstance(value["database_password"], str) or len(value["database_password"]) < 32:
            raise ValueError()
        return key, value["database_password"]
    except (KeyError, ValueError, TypeError):
        raise AssistantError("storage_vault_invalid", "Installation credentials invalid") from None


def config_spec(path):
    """The validated configuration and the trusted selection of its installation root.

    A configuration in a directory without ``installation.json`` is a hand-written
    development/test configuration: it names its own store and slot and is opened as given.
    A configuration next to a journal must name exactly the selected generation, so the
    database name, the store directory and the credential slot are compared with the root's
    own record instead of being trusted from the file.
    """
    file = Path(path)
    try:
        config = json.loads(file.read_text(encoding="utf8"))
    except (OSError, ValueError):
        raise AssistantError("storage_config", "Invalid explicit storage configuration") from None
    connection = config.get("database") if isinstance(config, dict) else None
    if (not isinstance(connection, dict) or set(connection) != {"host", "port", "user", "dbname"}
            or connection["host"] != "127.0.0.1"
            or type(connection["port"]) is not int or not 1024 <= connection["port"] <= 65535
            or not isinstance(config.get("data"), str) or not Path(config["data"]).is_absolute()
            or not isinstance(config.get("installation"), str)
            or not IDENTIFIER.fullmatch(config["installation"])):
        raise AssistantError("storage_config", "Invalid explicit storage configuration")
    root = file.resolve().parent
    journal = root/"installation.json"
    if not journal.exists():
        if config.get("credential_id") is not None:
            raise AssistantError("storage_config", "Invalid explicit storage configuration")
        return {"root": None, "selection": None, "config": config,
                "installation": config["installation"], "keyref": config["installation"],
                "connection": connection, "data": Path(config["data"])}
    from installation_generation import load_selection

    try:
        state = json.loads(journal.read_text(encoding="utf8"))
    except (OSError, ValueError):
        raise AssistantError("storage_config", "Invalid installation record") from None
    if (not isinstance(state, dict) or state.get("format") != 1
            or state.get("installation") != config["installation"]):
        raise AssistantError("storage_config", "Invalid installation record")
    if state.get("phase") != "ready" or state.get("port") != connection["port"]:
        raise AssistantError("storage_config", "Storage configuration does not match the installation")
    if connection["user"] != "qiban_runtime":
        raise AssistantError("storage_config", "Storage configuration must use the runtime login")
    selection = load_selection(root, state["installation"])
    if (connection["dbname"] != selection["database"]
            or Path(config["data"]).resolve() != Path(selection["store"]).resolve()
            or config.get("credential_id", config["installation"]) != selection["credential_id"]):
        raise AssistantError("storage_config", "Storage configuration does not match the selected generation")
    return {"root": root, "selection": selection, "config": config,
            "installation": state["installation"], "keyref": selection["credential_id"],
            "connection": connection, "data": Path(config["data"])}


def portable_switch_applied(path):
    """Whether the selection no longer matches this worker's opened configuration.

    This also works when restoring a second time from an already restored generation.
    An unreadable selection cannot authorize continued writes to the open database.
    """
    try:
        config_spec(path)
    except (AssistantError, OSError, ValueError):
        return True
    return False


def portable_host(management, path, application_session):
    """The private portable-restore channel for one installed desktop root.

    The service's key provider reads the data key of the generation this configuration
    selects, and the stager closes over this process's store, installation root and
    application session, so `portable_restore.stage_restore` runs with exactly the pair the
    service authenticated. The retained-copy provider derives every candidate and every key
    slot from this installation root, so a page handle can only name a copy this installation
    enumerated. Nothing here is a management operation or a model tool.
    """
    from portable_preservation import preserved_provider
    from portable_restore import stage_restore

    from qiban.desktop_portable import DesktopPortable

    spec = config_spec(path)
    root = spec["root"]
    if root is None or not isinstance(application_session, str) or not application_session:
        raise AssistantError("portable_unavailable", "便携恢复需要已就绪的桌面安装")

    def key_provider():
        current = config_spec(path)
        return credentials(current["installation"], current["keyref"])[0]

    def slot_key(keyref):
        # One key slot per generation: the installation slot for the original generation,
        # the generation's own slot for a restored one. Never a page-supplied identifier.
        current = config_spec(path)
        return credentials(current["installation"], keyref)[0]

    def stager(**pair):
        config_spec(path)  # The selection must still be the one this configuration names.
        runtime = getattr(management, 'runtime', None)
        if runtime is not None:
            # Once maintenance begins no background task may resume on the old store.
            # Any uncertain outcome requires a restart, rather than releasing admission.
            runtime.tasks.drain(runtime.tasks.block())
        return stage_restore(current_store=management.store, root=root,
                             application_session=application_session, **pair)

    preserved = preserved_provider(root, spec["installation"], slot_key)
    return DesktopPortable(management, key_provider, stager, preserved=preserved)


def open_store(path, credentials_reader=None):
    spec = config_spec(path)
    if spec["root"] is not None:
        # UPGRADE-177: while an upgrade transaction is open, ordinary business opening is
        # decided by that record and this program's published identity - a previous program may
        # not write a data generation that has been handed to its successor.
        from upgrade import gate

        gate.require(spec["root"], gate.identity_for_root(spec["root"], sys.executable), gate.BUSINESS)
    read = credentials if credentials_reader is None else credentials_reader
    key, password = read(spec["installation"], spec["keyref"])
    # No migration, initialization, key generation, or old-directory fallback on open.
    # Only the runtime login and this generation's data key are handed to the store.
    from qiban.desktop_profile import desktop_profile
    store = PostgresStore(spec["data"], {**spec["connection"], "password": password}, key, profile=desktop_profile())
    try:
        from companion_core.postgres import verify_schema

        verify_schema(store.database)
        store.read("manifest")  # Verify decryption before advertising readiness.
        if spec["config"].get('desktop_management') is True:
            from qiban.desktop_backups import finish_pending

            finish_pending(store)
        if not store.validate_critical()["ok"]:
            raise AssistantError("storage_integrity", "Critical encrypted records failed validation")
        return store
    except BaseException:
        store.database.close()
        raise


def require_own_program(service, app):
    """A frozen maintenance worker may only act on the program whose code it is running.

    ``prepare`` and ``confirm`` write a transaction about the installed program that the host
    names with ``--app-dir``. In a frozen build the directory of the running program is known from
    the process itself, so the two must be the same directory: the new build cannot prepare
    against the previous program, and the previous program cannot confirm the new one by handing
    over its directory. A source-mode run (the isolated tests) has no such directory to derive and
    keeps using the caller's explicit path.
    """
    if not getattr(sys, "frozen", False):
        return None
    own = Path(service.executing_app()).resolve()
    given = Path(app).resolve() if app else None
    if given is None or given != own:
        raise AssistantError("upgrade_executing_program",
                             "维护入口只能操作正在运行的那个程序目录，未继续。")
    return own


def maintenance(service, args):
    """Dispatch one trusted upgrade-maintenance verb; the host supplies every argument."""
    root, app = args.data_dir, args.app_dir
    verb = args.upgrade_maintenance
    if verb in ("prepare", "confirm"):
        require_own_program(service, app)
    if verb == "startup-confirm":
        # The host opens this root for business right after this answer, so it is the one verb that
        # takes no program directory from its caller: the backend derives the directory of the
        # program whose code is running from its own executable.
        from upgrade import startup

        return startup.confirm(root)
    if verb == "prepare":
        if not args.target_identity:
            raise AssistantError("upgrade_state", "upgrade target identity is required")
        return service.prepare(root, app, target_identity=args.target_identity)
    if verb == "confirm":
        return service.confirm(root, app)
    if verb in ("rollback-prepare", "rollback"):
        raise AssistantError("upgrade_host_required", "程序回退须由应用内 Windows 身份确认流程启动。")
    if verb == "cleanup":
        return service.core_cleanup(root)
    if verb == "abort":
        return service.abort(root)
    return service.status(root, app)


def emit(value):
    print(json.dumps(value), flush=True)


def desktop_models(broker, route):
    """The model slots one assembly hands every platform: both name one instance or none.

    The released desktop route is SIWC validation, where one subscription broker and one
    locally installed Codex executable serve local, Bilibili, QQ and WeChat. ``DesktopRuntime`` reads the
    generic slot for QQ/WeChat and the room slot for local/Bilibili, so both slots must hold
    the same owned instance: one executor, one admission slot, one account snapshot per turn.
    The plain Codex route keeps injecting no model, and a missing executable only leaves the
    platforms unready -- the store and local management must still start.
    """
    model = None
    if route == 'siwc-validation':
        from qiban.desktop_codex import executable
        from qiban.desktop_model import DesktopModel
        try:
            model = DesktopModel(broker, executable=executable())
        except AssistantError as error:
            if error.code != 'model_unavailable':
                raise
    return {'model': model, 'room_model': model}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade-host", nargs=3, metavar=("ROOT", "APP", "VERB"))
    parser.add_argument("--config")
    parser.add_argument("--developer-reply-timing", choices=("arm", "report", "cancel"), help=argparse.SUPPRESS)
    parser.add_argument("--timing-platform", choices=("qq", "wechat", "bilibili", "local"), default="qq", help=argparse.SUPPRESS)
    parser.add_argument("--application-session")
    parser.add_argument("--authentication-route", choices=('codex', 'siwc-validation'), default='codex')
    # UNINSTALL: the preparation page's own backend. It opens the store and local management (so the
    # existing backup business works) but builds no model, no platform connector and no scheduler
    # and never reconciles, so an uninstall preparation cannot run a platform or a model turn.
    parser.add_argument("--backup-only", action="store_true")
    parser.add_argument("--check-resources", action="store_true")
    parser.add_argument("--verify-bili-binding", action="store_true")
    parser.add_argument("--verify-bili-favorites", action="store_true")
    parser.add_argument("--file-reader", action="store_true")
    parser.add_argument("--initialize")
    parser.add_argument("--postgres")
    parser.add_argument("--test-pause")
    # UPGRADE-177: the trusted maintenance entry of one local upgrade transaction. It is driven
    # only by a trusted host process of this application; no page route, management operation or
    # model tool maps to it, and the rollback verbs additionally require the host's own Windows
    # verification receipt. ``startup-confirm`` is the startup decision before the backend opens.
    parser.add_argument("--upgrade-maintenance",
                        choices=('prepare', 'confirm', 'rollback-prepare', 'rollback', 'cleanup',
                                 'status', 'abort', 'startup-confirm'))
    parser.add_argument("--data-dir")
    parser.add_argument("--app-dir")
    parser.add_argument("--target-identity")
    args = parser.parse_args()
    if args.upgrade_host:
        import upgrade_host

        return upgrade_host.main(*args.upgrade_host)
    if args.file_reader:
        from companion_core.file_worker import main as read_file

        read_file()
        return
    if args.check_resources:
        from companion_core.hybrid_history import MODEL_DIR, shared_encoder

        vectors = shared_encoder(MODEL_DIR).encode(["检查本地聊天召回"])
        assert vectors.shape == (1, 512) and abs(float(vectors[0] @ vectors[0]) - 1) < .001
        import psycopg
        from bilibili_api import BILIBILI_API_VERSION, get_selected_client, video
        from bilibili_api.utils.utils import get_api
        assert BILIBILI_API_VERSION=="17.4.2" and get_selected_client()[0]=="httpx"
        assert callable(video.Video) and get_api("video")["operate"]["favorite"]["url"]
        from bili_assistant.favorite_workflow import TOOLS
        from qiban.desktop_bili_policy import _defaults
        assert set(TOOLS)=={"folders","contents","create","move","done"}
        assert _defaults()["actions"]["favorite_create"]["daily_limit"]==5
        assert _defaults()["actions"]["favorite_move"]["daily_limit"]==50

        import companion_core
        import companion_qq
        from companion_core.cipher import ContentCipher
        from companion_core.file_reader import extract_bytes
        from companion_core.image_reader import extract_image
        from companion_core.media_budget import MediaBudgetSettings, TurnMediaBudget
        from companion_core.media_originals import MediaOriginals
        from companion_qq.native_login import NativeLogin
        from companion_qq.native_media import NativeMedia
        from companion_qq.native_resources import runtime_lock
        from qiban.desktop_profile import desktop_profile
        from qiban.desktop_qq import DesktopQQ
        from qiban.desktop_qq_targets import adapter

        root = Path(companion_core.__file__).parent
        profile = desktop_profile()
        assert "video_progress" in profile.schemas and "videos" in profile.extra_tables
        scripts = sorted((root / "sql").glob("[0-9]*.sql"))
        assert scripts and all(path.read_text(encoding="utf8").strip() for path in scripts)
        from companion_core.matter_runner import tick as run_role_turn
        from companion_core.privacy_review import tick as review_fragment
        from companion_core.role_actions import commit as commit_role_turn
        from companion_core.role_runtime import RoleRuntime
        from qiban.desktop_matters import operate as manage_matters
        from qiban.desktop_public_tools import wake as public_wake
        assert all(callable(entry) for entry in (RoleRuntime.react, RoleRuntime.wake, review_fragment,
                                                  manage_matters, public_wake, commit_role_turn, run_role_turn))
        assert {"020_role_matters_privacy.sql","021_disclosure_proofs.sql","022_privacy_review_sources.sql",
                "023_privacy_corrections.sql", "024_role_activity.sql",
                "025_role_backup_revision.sql"} <= {path.name for path in scripts}
        assert (Path(companion_qq.__file__).parent / "native" / NativeLogin.WORKER).is_file()
        assert runtime_lock()["files"]
        assert callable(DesktopQQ) and callable(adapter)
        assert callable(MediaOriginals)
        assert callable(NativeMedia)
        assert callable(extract_image)
        assert extract_bytes('probe.txt', b'file resource check')['units'][0]['text'] == 'file resource check'
        from defusedxml import ElementTree
        from docx import Document
        from openpyxl import load_workbook
        from pptx import Presentation
        from pypdf import PdfReader

        assert all(callable(value) for value in (PdfReader, Document, load_workbook, Presentation, ElementTree.fromstring))
        assert callable(MediaBudgetSettings) and callable(TurnMediaBudget)
        assert (root / 'sql' / '012_media_originals.sql').is_file()
        assert (Path(companion_qq.__file__).parent / 'native' / 'media_download.cjs').is_file()
        cipher = ContentCipher(bytes(range(32)))
        assert cipher.decrypt(cipher.encrypt(b"probe", "probe", "1", "1"), "probe", "1", "1") == b"probe"
        emit({"ready": True, "frozen": bool(getattr(sys, "frozen", False)),
              "root": str(root.parent), "search_paths": sys.path, "sql_files": len(scripts),
              "schemas": len(profile.schemas), "postgres_driver": psycopg.pq.__impl__})
        return 0
    if args.initialize:
        from installation import initialize
        from upgrade import gate

        try:
            if not args.postgres:
                parser.error("--postgres required for initialization")
            # A data generation handed to its successor may be migrated by that successor only.
            gate.require(args.initialize, gate.identity_for_root(args.initialize, sys.executable),
                         gate.INITIALIZE)
            emit(initialize(args.initialize, args.postgres, sys.executable, args.test_pause))
            return 0
        except AssistantError as error:
            emit({"ready": False, "error": error.code})
            return 1
        except Exception:
            emit({"ready": False, "error": "installation_failed"})
            return 1
    if args.upgrade_maintenance:
        from upgrade import service

        # `startup-confirm` deliberately takes no --app-dir: it confirms with the directory of the
        # program whose code is running, which no argument may supply.
        if not args.data_dir or (not args.app_dir
                                 and args.upgrade_maintenance != "startup-confirm"):
            parser.error("--data-dir and --app-dir are required for --upgrade-maintenance")
        try:
            emit(maintenance(service, args))
            return 0
        except AssistantError as error:
            emit({"ok": False, "error": error.code})
            return 1
        except Exception:
            emit({"ok": False, "error": "upgrade_failed"})
            return 1
    if not args.config:
        parser.error("--config is required unless checking resources")
    if args.developer_reply_timing:
        # Local owner/credential-vault access, no management listener or UI route.
        # Connect to the running selected store without migration or another runtime.
        diagnostic_store = None
        try:
            from companion_core.reply_timing import control

            spec = config_spec(args.config)
            data_key, password = credentials(spec["installation"], spec["keyref"])
            diagnostic_store = PostgresStore(spec["data"], {**spec["connection"], "password": password}, data_key)
            emit(control(diagnostic_store, args.developer_reply_timing, args.timing_platform))
            return 0
        except AssistantError as error:
            emit({"ok": False, "error": error.code})
            return 1
        except Exception:
            emit({"ok": False, "error": "timing_unavailable"})
            return 1
        finally:
            if diagnostic_store is not None:
                diagnostic_store.database.close()
    store = None
    codex = None
    runtime = None
    management = None
    installation_guard = None
    try:
        from filelock import FileLock, Timeout

        selected_config = config_spec(args.config)
        if selected_config["root"] is not None:
            installation_guard = FileLock(str(selected_config["root"] / "installation.lock"), timeout=0)
            try:
                installation_guard.acquire()
            except Timeout:
                raise AssistantError("installation_busy", "本机资料正在维护，暂不能启动后台。") from None
        store = open_store(args.config)
        config = json.loads(Path(args.config).read_text(encoding="utf8"))
        management = None
        if config.get("desktop_management") is True:
            from install_windows import user_sid

            from qiban.desktop_management import DesktopManagement
            from qiban.desktop_runtime import DesktopRuntime

            management = DesktopManagement(store,config["installation"],user_sid())
            if args.backup_only:
                # UNINSTALL: the preparation backend opens the store, local management and the
                # existing backup business only. No platform connector and no model is built, and
                # the runtime is minimal: it is never reconciled and its scheduler is never opened.
                runtime = DesktopRuntime(management,args.application_session)
            else:
                from qiban.desktop_bili import DesktopBili
                from qiban.desktop_codex import DesktopCodex
                from qiban.desktop_qq import DesktopQQ

                management.bili = DesktopBili(management,Path(args.config).resolve().parent)
                if args.verify_bili_binding or args.verify_bili_favorites:
                    import asyncio

                    from bilibili_api import BILIBILI_API_VERSION

                    from qiban.desktop_bili_connector import adapter

                    management.authority.require(management.request(), administrator=True)
                    binding = adapter(management.bili)
                    if not binding or store.state("core-run:bilibili",{}).get("status")=="active":
                        raise AssistantError("bili_check_unavailable","需已绑定且角色已停止。")
                    async def verify_bound():
                        try:
                            actual = await binding.engine.adapter.me()
                            matches = actual["uid"] == binding.account
                            result = {"verified":matches,"sdk":BILIBILI_API_VERSION}
                            if args.verify_bili_favorites and matches:
                                tools = binding.engine.adapter.favorite_tools(
                                    lambda: management.authority.require(management.request(), administrator=True))
                                listing = await tools.list()
                                default = await tools.default()
                                result.update(folder_count=listing["total"],default_identified=bool(default),read_only=True)
                            return result
                        finally:
                            await binding.close()
                    result = asyncio.run(verify_bound())
                    matches = result["verified"]
                    emit(result)
                    return 0 if matches else 1
                if args.authentication_route == 'siwc-validation':
                    from qiban.desktop_siwc_accounts import DesktopSiwcAccounts

                    codex = DesktopSiwcAccounts(Path(args.config).resolve().parent/'siwc-account', store)
                else:
                    codex = DesktopCodex(Path(args.config).resolve().parent/'codex-account')
                management.codex = codex
                management.qq = DesktopQQ(store, config.get('qq_runtime', str(Path(sys.executable).parent.parent/'qq')),
                                          Path(args.config).resolve().parent/'qq-account')
                from qiban.desktop_bili_connector import adapter as bili_adapter
                from qiban.desktop_qq_targets import adapter as qq_adapter

                qq = qq_adapter(management.qq)
                from qiban.desktop_wechat import adapter as wechat_adapter

                management.wechat_resources = Path(sys.executable).parent.parent / "wechat"
                wechat = wechat_adapter(store, config["installation"], management.wechat_resources)
                runtime = DesktopRuntime(management,args.application_session,
                                         **desktop_models(codex,args.authentication_route),
                                         adapters=tuple(item for item in (bili_adapter(management.bili),qq,wechat,management.room) if item is not None))
                runtime.reconcile()
                from qiban.desktop_wechat_window import WeChatTaskbar, WindowsDesktop

                runtime.open(wechat_taskbar=WeChatTaskbar(WindowsDesktop()) if sys.platform == "win32" else None)
        if args.verify_bili_binding or args.verify_bili_favorites:
            raise AssistantError("bili_check_unavailable","需桌面开发安装。")
        portable = None
        if management is not None:
            try:
                portable = portable_host(management, args.config, args.application_session)
            except AssistantError as error:
                # Only a development configuration without an installation root lacks it.
                if error.code != "portable_unavailable":
                    raise
        emit({"ready": True})
        switched = False
        for line in sys.stdin:
            message = json.loads(line)
            if message == {"action": "stop"}:
                if management is not None:
                    management.runtime.close()
                    if management.qq is not None:
                        management.qq.close()
                    runtime = None
                if codex is not None:
                    codex.close()
                store.database.close()
                store = None
                emit({"stopped": True})
                return 0
            if switched:
                # After a portable switch this process may only be observed and stopped; the
                # restarted one reads the generation its installation root now selects.
                if message == {"action": "status"}:
                    emit({"ready": False, "error": "portable_switched"})
                    continue
                emit({"ok": False, "error": "portable_switched",
                      "message": "资料已切换到新代次，本后台不再写入原资料，请重启应用后台"})
                continue
            if isinstance(message,dict) and message.get("action") == "desktop-portable":
                answer = portable.dispatch(message) if portable is not None else {
                    "ok": False, "error": "portable_unavailable", "message": "便携恢复宿主尚未就绪"}
                emit(answer)
                if (message.get("operation") == "complete"
                        and portable_switch_applied(args.config)):
                    switched = True
                continue
            if isinstance(message,dict) and message.get("action") == "management":
                emit(management.dispatch(message) if management else {"ok":False,"error":"management_unavailable"})
                continue
            if isinstance(message,dict) and message.get("action") == "desktop-recovery":
                emit(management.recovery_dispatch(message) if management else {"ok":False,"error":"management_unavailable"})
                continue
            if isinstance(message,dict) and message.get("action") == "desktop-restore":
                emit(management.backups.dispatch(message) if management else {"ok":False,"error":"management_unavailable"})
                continue
            if message != {"action": "status"}:
                raise AssistantError("storage_command", "Unsupported worker command")
            with store.db() as database:
                database.execute("SELECT 1").fetchone()
            store.read("manifest")
            emit({"ready": True})
        return 1
    except AssistantError as error:
        emit({"ready": False, "error": error.code})
        return 1
    except Exception:
        # Exception messages can contain connection details; protocol carries only a fixed code.
        emit({"ready": False, "error": "storage_unavailable"})
        return 1
    finally:
        try:
            if management is not None and management.bili is not None:
                management.bili.close()
            if management is not None and management.qq is not None:
                management.qq.close()
            if management is not None:
                management.learning_index.invalidate(closing=True)
            if runtime is not None:
                runtime.close()
        finally:
            try:
                if codex is not None:
                    codex.close()
                if store is not None:
                    store.database.close()
            finally:
                if installation_guard is not None:
                    installation_guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
