"""Real cross-machine portable restore across a frozen installer/worker restart (D54/D55).

One acceptance run on a brand-new isolated installation root, never the 161b profile:

1. the managed package installs itself with ``--install-host`` (fresh generation ``qiban``);
2. a synthetic *source* installation is built in its own random database (own random data
   key, real ``desktop_profile``, ``provision_owner`` with the real Windows SID) and holds the
   role name ``跨机恢复162-来源`` plus one memory and its modification history;
3. the source exports a real ``.ccbackup``/``.cckey`` pair and the *current* installation runs
   preview, begin and complete with the real ``portable_restore.stage_restore`` stager, which
   commits one new generation and a new credential slot holding the source data key;
4. the database stops, the same package re-installs (it must now read the committed pointer and
   the new key and rewrite its configuration), and the reopened store is inspected;
5. the frozen ``--stack-host`` starts the frozen worker on that generation and stops normally;
6. the same pair is restored once more as a second generation through the same real
   stop/install/reopen cycle.

``--with-preserved`` additionally exercises this machine's own retained copies after those
two cycles: it lists them through the frozen worker's real provider, picks the copy taken
before the first replacement by the preservation name the first preview reported, and runs
the unified ``preserved-preview``/``begin``/``complete`` restore into a new generation (no
password, no pointer revert). The reopened generation must show this installation's own
earlier role and the absence of a logical binding, the portable-only data gone, and the
current physical owner, application lock, autorun and every earlier generation plus every
retained copy still present. Without the flag nothing extra runs.

The Windows verification prompt is *not* exercised here: a trusted host calls begin/complete
directly, exactly as ``desktop_portable`` expects a trusted caller to. Nothing is deleted: the
generation directories, backups and credential slots stay for manual acceptance review. Only
non-secret identifiers are recorded; no password, data key or payload is ever printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import uuid
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import psycopg

# A packaged interpreter started without PYTHONPATH (for example with -E) still has to import
# the probe modules below and companion_core, so the source root comes before them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from credential_vault import VaultCredentials
from install_windows import user_sid
from installation_generation import load_selection
from portable_restore import stage_restore
from psycopg import sql
from run_postgres import Host
from run_storage import StackHost
from storage_worker import VAULT, config_spec, credentials, open_store, portable_host

from companion_core import postgres_transfer
from companion_core.contracts import ActorRef
from companion_core.errors import AssistantError
from companion_core.postgres import PostgresStore
from companion_core.process_scope import ProcessScope
from companion_core.runtime_keys import DESKTOP_PLATFORMS
from qiban.desktop_identity import OWNER as IDENTITY_OWNER
from qiban.desktop_identity import ROLE as IDENTITY_ROLE
from qiban.desktop_identity import binding as logical_binding
from qiban.desktop_identity import verify as verify_identity
from qiban.desktop_management import LOCK, DesktopManagement, provision_owner
from qiban.desktop_portable import DesktopPortable
from qiban.desktop_profile import desktop_profile
from qiban.desktop_runtime import HOLD, PREFERENCES

BASE = Path(__file__).resolve().parents[2] / ".dev/refactor-r0/desktop/portable-162"
ROLE_NAME = "跨机恢复162-来源"
MEMORY = "portable-restart-162"
MEMORY_CONTENT = "跨机恢复162-来源记忆"
PASSWORD = secrets.token_urlsafe(24)  # synthetic, never written to the report
REPORT, HOSTS = {"passed": False, "cases": []}, []
PRESERVED = {}  # label -> preservation file name reported by that restore's preview
APP, ROOT, EVIDENCE = Path(), Path(), Path()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def phase(name, **detail):
    REPORT["cases"].append(name)
    if detail:
        REPORT.setdefault("details", {})[name] = detail
    write(EVIDENCE / "result.json", REPORT)


def check(condition, name, **detail):
    if not condition:
        raise RuntimeError("case_failed: " + name)
    phase(name, **detail)


def unique(name):
    path, index = EVIDENCE / name, 1
    while path.exists():
        index += 1
        path = EVIDENCE / f"{name}-{index}"
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def owned(arguments, timeout=300):
    """One application host run inside this test's process scope; never a window."""
    process = subprocess.Popen([str(APP / "Qiban.exe"), *arguments], stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
    scope = ProcessScope()
    try:
        scope.attach(process)
        scope.resume()
        return process.wait(timeout=timeout)
    finally:
        scope.close()


def install(label):
    """Frozen installer/worker entry: fresh install first, generation re-selection afterwards."""
    evidence = unique(label)
    assert owned(["--install-host", str(ROOT), str(APP), str(evidence), ""]) == 0, "安装宿主未正常结束"
    state = read_json(ROOT / "installation.json")
    config = read_json(ROOT / "storage-config.json")
    phase(label, generation=config.get("credential_id"), database=config["database"]["dbname"])
    return state, config


def start_database(label):
    host = Host(APP / "Qiban.exe", ROOT / "postgres-spec.json", unique(label), deadline_seconds=300)
    HOSTS.append(host)
    return host


def stop_database(host):
    host.stop()
    host.close()
    HOSTS.remove(host)


@contextmanager
def cluster(label):
    """The installation's own cluster, started and stopped normally by the frozen host."""
    host = start_database(label)
    try:
        yield
    finally:
        stop_database(host)


def maintenance(state, credential):
    return {"host": "127.0.0.1", "port": state["port"], "user": "qiban_setup",
            "password": credential["bootstrap_password"], "dbname": "postgres", "connect_timeout": 5}


def dispatch(service, operation, arguments=None):
    answer = service.dispatch({"action": "management", "operation": operation,
                               "arguments": arguments or {}, "request_id": uuid.uuid4().hex})
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise AssistantError((answer or {}).get("error", "management"), "管理操作未完成")
    return answer["result"]


def write_memory(service, content, expected):
    return dispatch(service, "records.correct", {
        "kind": "memories", "identifier": MEMORY, "expected": expected,
        "value": {"schema_version": 1, "id": MEMORY, "kind": "user_preference", "content": content,
                  "confidence": "observed", "tags": [], "locked": False,
                  "source": {"type": "user_instruction", "reference": "portable-restart-162",
                             "recorded_at": "2026-11-07T00:00:00Z"}}})


def actor_key(value):
    return ActorRef(**value).key if isinstance(value, dict) else getattr(value, "key", None)


def build_source(state, credential):
    """A second, independent installation's data inside this test cluster only."""
    database, installation = "qiban_source_" + uuid.uuid4().hex, uuid.uuid4().hex
    with psycopg.connect(**maintenance(state, credential), autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier("qiban_owner")))
    key = secrets.token_bytes(32)
    store = PostgresStore(EVIDENCE / "source-store",
                          {**maintenance(state, credential), "dbname": database}, key,
                          profile=desktop_profile())
    store.migrate()
    check(store.initialize()["ok"], "source_store_initialized")
    provision_owner(store, installation, user_sid())
    service = DesktopManagement(store, installation, user_sid())
    identity = {"owner": store.state(IDENTITY_OWNER)["actor"], "role": store.state(IDENTITY_ROLE)}
    persona = dispatch(service, "config.read", {"kind": "persona"})
    dispatch(service, "config.patch", {"kind": "persona", "expected": persona["version"],
             "changes": {"name": ROLE_NAME, "core": persona["data"]["core"],
                         "interests": persona["data"]["interests"]}})
    first = write_memory(service, MEMORY_CONTENT + "（修改前）", None)
    write_memory(service, MEMORY_CONTENT, first["version"])
    with store.db() as db:
        history = db.execute("SELECT COUNT(*) FROM content_history WHERE kind=? AND id=?",
                             ("memories", MEMORY)).fetchone()[0]
    check(dispatch(service, "config.read", {"kind": "persona"})["data"]["name"] == ROLE_NAME
          and dispatch(service, "records.read", {"kind": "memories", "identifier": MEMORY})["data"]["content"]
          == MEMORY_CONTENT and history >= 1, "source_role_name_and_memory_written",
          database=database, installation=installation, history=history)
    return {"installation": installation, "database": database, "store": store, "key": key,
            "service": service, "identity": identity, "history": history}


def export_pair(source):
    target = EVIDENCE / "portable-pair"
    target.mkdir()

    def unused(**_):
        raise AssistantError("portable_input", "导出不切换资料")

    portable = DesktopPortable(source["service"], key_provider=lambda: source["key"], stager=unused)
    answer = portable.dispatch({"action": "desktop-portable", "operation": "export",
                                "arguments": {"path": str(target / "source.ccbackup"),
                                              "password": PASSWORD}})
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise AssistantError((answer or {}).get("error", "portable_failed"), "便携备份未导出")
    result = answer["result"]
    backup, key_file = target / result["backup"], target / result["key_file"]
    check(hashlib.sha256(backup.read_bytes()).hexdigest() == result["backup_sha256"]
          and hashlib.sha256(key_file.read_bytes()).hexdigest() == result["key_sha256"]
          and result["records"] > 0 and result["history"] > 0, "portable_pair_exported",
          backup=result["backup"], key_file=result["key_file"], backup_bytes=result["backup_bytes"],
          key_bytes=result["key_bytes"], backup_sha256=result["backup_sha256"],
          records=result["records"], history=result["history"])
    REPORT["backup_sha256"], REPORT["backup_records"] = result["backup_sha256"], result["records"]
    return backup, key_file


def restore(store, service, backup, key_file, label):
    """preview -> begin -> complete with the real stager; no Windows prompt is claimed."""
    selection = load_selection(ROOT, INSTALLATION)
    key = credentials(INSTALLATION, selection["credential_id"])[0]
    stager = partial(stage_restore, current_store=store, root=ROOT,
                     application_session="portable-restart-162:" + uuid.uuid4().hex)
    portable = DesktopPortable(service, key_provider=lambda: key, stager=stager)
    before = postgres_transfer.fingerprint(store)
    preview = portable.preview(str(backup), str(key_file), PASSWORD)
    # The copy this preview took is the state that existed *before* this replacement; the
    # ``--with-preserved`` section later reopens exactly that artifact.
    PRESERVED.setdefault(label, preview["preservation"]["name"])
    # `records`/`history` are the preview's own payload counters, not the export's row counts.
    check(preview["records"] > 0 and preview["history"] > 0 and bool(preview["counters"])
          and preview["source"]["role"] == ROLE_NAME
          and preview["preservation"]["name"].endswith(".ccbackup"),
          label + "_preview_authenticated", source=preview["source"], records=preview["records"],
          history=preview["history"], preservation=preview["preservation"]["name"])
    challenge = portable.begin(preview["token"])["challenge"]
    result = portable.complete(challenge)
    check(result["switched"] is True and result["restart_required"] is True, label + "_switch_committed",
          generation=result["stager"]["generation"], database=result["stager"]["database"])
    check(postgres_transfer.fingerprint(store) == before, label + "_old_store_unchanged")
    check(credentials(INSTALLATION, result["stager"]["generation"])[0] == SOURCE["key"],
          label + "_new_slot_holds_source_key")
    current = load_selection(ROOT, INSTALLATION)
    check(current["generation"] == result["stager"]["generation"]
          and current["database"] == result["stager"]["database"]
          and current["credential_id"] == result["stager"]["generation"], label + "_pointer_advanced")
    return result


def inspect(store, service, expected, label):
    check(store.state(IDENTITY_OWNER) == expected["owner"], label + "_current_physical_owner_preserved")
    lock = store.state(LOCK)
    check(lock["enabled"] == expected["lock"]["enabled"]
          and lock["revision"] == expected["lock"]["revision"] + 1, label + "_current_app_lock_preserved")
    held = store.state(HOLD)
    check(isinstance(held, dict) and set(held.get("platforms", [])) == set(DESKTOP_PLATFORMS),
          label + "_all_platforms_held")
    check(dispatch(service, "config.read", {"kind": "persona"})["data"]["name"] == ROLE_NAME,
          label + "_source_role_name_inherited")
    memory = dispatch(service, "records.read", {"kind": "memories", "identifier": MEMORY})
    with store.db() as db:
        history = db.execute("SELECT COUNT(*) FROM content_history WHERE kind=? AND id=?",
                             ("memories", MEMORY)).fetchone()[0]
    check(memory["data"]["content"] == MEMORY_CONTENT and history >= expected["history"],
          label + "_source_memory_and_history_inherited", history=history)
    bound = verify_identity(store, installation=INSTALLATION, owner=service.actor)
    check(bound is not None and actor_key(service.room_actor()) == expected["room"]
          and actor_key(bound["logical_role"]) == expected["role"], label + "_logical_identity_inherited")


def frozen_stack(label="frozen_worker_started_on_restored_generation"):
    """The frozen worker must open the restored generation with no source and no platform."""
    poison = EVIDENCE / "no-source"
    poison.mkdir(exist_ok=True)
    (poison / "companion_core.py").write_text('raise RuntimeError("external source loaded")', encoding="utf8")
    isolated = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
                "PYTHONHOME": str(poison / "no-python"), "PYTHONPATH": str(poison), "PYTHONNOUSERSITE": "1"}
    host = StackHost(APP / "Qiban.exe", ROOT / "stack-spec.json", unique("frozen-stack"),
                     read_json(ROOT / "postgres-spec.json"), ROOT / "logs", env=isolated, cwd=poison,
                     deadline_seconds=300)
    HOSTS.append(host)
    stop_database(host)
    phase(label)


def current_store(config):
    return open_store(config)


# -- this machine's own retained copies (--with-preserved) -------------------------
def portable_answer(portable, operation, arguments, fallback):
    """One real private-channel request; the provider's opaque handles stay inside."""
    answer = portable.dispatch({"action": "desktop-portable", "operation": operation,
                                "arguments": arguments})
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise AssistantError((answer or {}).get("error") or "portable_failed", fallback)
    return answer["result"]


def preserved_copies(portable):
    """Every retained copy the provider can see, walked through its real pages."""
    items, after = [], ""
    for _ in range(20):
        page = portable_answer(portable, "preserved-list", {"after": after}, "无法列出本机保全资料")
        items.extend(page["items"])
        after = page.get("next") or ""
        if not after:
            return items
    raise AssistantError("preserved_unavailable", "本机保全资料列表未能结束")


def preserved_fields(copies):
    """What may be recorded: the provider's listing without its capability handles."""
    return [{key: item[key] for key in ("name", "taken_at", "bytes", "usable")} for item in copies]


def preserved_channel(service, purpose):
    """The frozen worker's own portable channel for this installation root."""
    return portable_host(service, ROOT / "storage-config.json",
                         f"portable-restart-162-{purpose}:" + uuid.uuid4().hex)


def preserved_restore():
    """Restore this machine's own first pre-replacement copy into a new generation."""
    wanted = PRESERVED["first_restore"]
    with cluster("database-host-preserved"):
        store = current_store(ROOT / "storage-config.json")
        service = DesktopManagement(store, INSTALLATION, user_sid())
        portable = preserved_channel(service, "preserved")
        copies = preserved_copies(portable)
        REPORT["preserved_listing"] = preserved_fields(copies)
        chosen = next((item for item in copies if item["name"] == wanted), None)
        check(chosen is not None and chosen["usable"] is True, "preserved_original_copy_listed",
              backup_name=wanted, listed=len(copies))
        preview = portable_answer(portable, "preserved-preview", {"handle": chosen["handle"]},
                                  "本机保全资料预览未完成")
        REPORT["preserved_preview"] = {key: preview[key] for key in
                                       ("kind", "name", "taken_at", "records", "history",
                                        "source", "preservation", "effect")}
        check(preview["kind"] == "preserved" and preview["name"] == wanted
              and preview["records"] > 0 and preview["preservation"]["name"].endswith(".ccbackup"),
              "preserved_preview_authenticated", backup_name=preview["name"], taken_at=preview["taken_at"],
              records=preview["records"], source=preview["source"])
        previous = load_selection(ROOT, INSTALLATION)
        lock = store.state(LOCK)
        challenge = portable_answer(portable, "begin", {"token": preview["token"]},
                                    "本机保全恢复未开始")["challenge"]
        result = portable_answer(portable, "complete", {"challenge": challenge},
                                 "本机保全恢复未完成")
        check(result["switched"] is True and result["restart_required"] is True,
              "preserved_switch_committed", generation=result["stager"]["generation"],
              database=result["stager"]["database"])
        current = load_selection(ROOT, INSTALLATION)
        check(current["generation"] == result["stager"]["generation"]
              and current["generation"] != previous["generation"]
              and current["generation"] not in REPORT["generations"]
              and current["credential_id"] == result["stager"]["generation"],
              "preserved_restore_committed_new_generation")
        check(result["preservation"]["name"] != wanted,
              "preserved_restore_preserved_replaced_generation")
        REPORT["generations"].append(result["stager"]["generation"])
        REPORT["key_slots"].append(result["stager"]["generation"])
        store.database.close()
    return {"lock": lock, "previous": previous["generation"],
            "names": {item["name"] for item in copies}}


def inspect_original(store, service, expected, label):
    """The pre-portable state came back; only this machine's own identity may remain."""
    check(store.state(IDENTITY_OWNER) == expected["owner"], label + "_current_physical_owner_preserved")
    lock = store.state(LOCK)
    check(lock["enabled"] == expected["lock"]["enabled"]
          and lock["revision"] == expected["lock"]["revision"] + 1, label + "_current_app_lock_preserved")
    check(store.state(PREFERENCES) == expected["preferences"], label + "_current_autorun_preserved")
    held = store.state(HOLD)
    check(isinstance(held, dict) and set(held.get("platforms", [])) == set(DESKTOP_PLATFORMS),
          label + "_all_platforms_held")
    check(store.state(IDENTITY_ROLE) == expected["physical_role"], label + "_original_logical_role_restored")
    check(logical_binding(store) is None, label + "_original_binding_absence_restored")
    check(dispatch(service, "config.read", {"kind": "persona"})["data"]["name"] == expected["persona_name"]
          and expected["persona_name"] != ROLE_NAME, label + "_original_role_name_restored")
    answer = service.dispatch({"action": "management", "operation": "records.read",
                               "request_id": uuid.uuid4().hex,
                               "arguments": {"kind": "memories", "identifier": MEMORY}})
    check(answer.get("ok") is not True and answer.get("error") == "not_found",
          label + "_source_only_memory_absent")


def preserved_retained(previous_names, generations):
    """Nothing earlier was replaced away: every generation and every retained copy remains."""
    missing = [name for name in gens_paths(generations) if not name[1].is_dir()]
    check(not missing, "original_and_generations_retained",
          generations=generations, missing=[str(path) for _, path in missing])
    with cluster("database-host-retained"):
        store = current_store(ROOT / "storage-config.json")
        service = DesktopManagement(store, INSTALLATION, user_sid())
        portable = preserved_channel(service, "retained")
        names = {item["name"] for item in preserved_copies(portable)}
        REPORT["preserved_listing_after_restore"] = sorted(names)
        check(previous_names <= names, "preserved_copies_retained",
              added=sorted(names - previous_names), retained=len(previous_names & names))
        store.database.close()


def gens_paths(generations):
    """The original generation store plus one address per restored generation."""
    paths = [("original", ROOT / "store")]
    paths.extend((name, ROOT / "generations" / name / "store") for name in generations)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="managed EXE publish directory")
    parser.add_argument("--evidence", required=True, type=Path, help="new directory inside the 162 evidence root")
    parser.add_argument("--with-preserved", action="store_true",
                        help="also restore this machine's own first pre-replacement copy")
    args = parser.parse_args()
    global APP, EVIDENCE, ROOT, INSTALLATION, SOURCE
    APP = args.app.resolve(strict=True)
    EVIDENCE = args.evidence.resolve()
    assert (APP / "Qiban.exe").is_file(), "缺少 Qiban.exe"
    assert (APP / "resources/storage").is_dir() and (APP / "resources/postgres").is_dir(), "应用资源不完整"
    assert EVIDENCE.is_relative_to(BASE.resolve()) and BASE.resolve().is_dir(), "evidence 必须在 portable-162 之下"
    EVIDENCE.mkdir(parents=True, exist_ok=False)
    ROOT = EVIDENCE / "installation"
    REPORT["test_root"] = str(ROOT)
    REPORT["windows_verification"] = "not_performed_trusted_host"
    write(EVIDENCE / "result.json", REPORT)
    try:
        install("fresh_install_stopped_normally")
        state = read_json(ROOT / "installation.json")
        INSTALLATION = state["installation"]
        credential = json.loads(VaultCredentials().get_password(VAULT, INSTALLATION))
        with cluster("database-host-first"):
            SOURCE = build_source(state, credential)
            REPORT["source"] = {"installation": SOURCE["installation"], "database": SOURCE["database"]}
            config, spec = ROOT / "storage-config.json", config_spec(ROOT / "storage-config.json")
            current = current_store(config)
            service = DesktopManagement(current, INSTALLATION, user_sid())
            original = {"owner": current.state(IDENTITY_OWNER), "lock": current.state(LOCK),
                        "history": SOURCE["history"], "room": actor_key(SOURCE["identity"]["owner"]),
                        "role": actor_key(SOURCE["identity"]["role"]),
                        # What this installation itself was before any replacement.
                        "physical_role": current.state(IDENTITY_ROLE),
                        "persona_name": dispatch(service, "config.read", {"kind": "persona"})["data"]["name"],
                        "preferences": current.state(PREFERENCES)}
            check(current.state(IDENTITY_OWNER)["installation"] == INSTALLATION
                  and spec["selection"]["generation"] is None, "current_installation_opened")
            backup, key_file = export_pair(SOURCE)
            first = restore(current, service, backup, key_file, "first_restore")
            REPORT["generations"] = [first["stager"]["generation"]]
            REPORT["key_slots"] = [INSTALLATION, first["stager"]["generation"]]
            current.database.close()
            SOURCE["store"].database.close()
        state, config = install("restart_reads_committed_generation")
        check(config["credential_id"] == REPORT["generations"][0]
              and config["database"]["dbname"] == "qiban_restore_" + REPORT["generations"][0]
              and state["installation"] == INSTALLATION, "restart_config_names_new_generation")
        check(load_selection(ROOT, INSTALLATION)["generation"] == REPORT["generations"][0],
              "restart_keeps_committed_pointer")
        with cluster("database-host-reopened"):
            store = current_store(ROOT / "storage-config.json")
            service = DesktopManagement(store, INSTALLATION, user_sid())
            inspect(store, service, original, "reopened")
            reopened_lock = store.state(LOCK)
            store.database.close()
        frozen_stack()
        with cluster("database-host-second"):
            store = current_store(ROOT / "storage-config.json")
            service = DesktopManagement(store, INSTALLATION, user_sid())
            second = restore(store, service, backup, key_file, "second_restore")
            REPORT["generations"].append(second["stager"]["generation"])
            REPORT["key_slots"].append(second["stager"]["generation"])
            store.database.close()
        state, config = install("restart_reads_second_generation")
        check(config["credential_id"] == REPORT["generations"][1]
              and state["installation"] == INSTALLATION, "second_restart_config_names_new_generation")
        with cluster("database-host-final"):
            store = current_store(ROOT / "storage-config.json")
            service = DesktopManagement(store, INSTALLATION, user_sid())
            inspect(store, service, {**original, "lock": reopened_lock}, "second_generation")
            store.database.close()
        if args.with_preserved:
            preserved = preserved_restore()
            state, config = install("restart_reads_preserved_generation")
            check(config["credential_id"] == REPORT["generations"][2]
                  and config["database"]["dbname"] == "qiban_restore_" + REPORT["generations"][2]
                  and state["installation"] == INSTALLATION,
                  "preserved_restart_config_names_new_generation")
            check(load_selection(ROOT, INSTALLATION)["generation"] == REPORT["generations"][2],
                  "preserved_restart_keeps_pointer")
            with cluster("database-host-preserved-reopened"):
                store = current_store(ROOT / "storage-config.json")
                service = DesktopManagement(store, INSTALLATION, user_sid())
                inspect_original(store, service, {**original, "lock": preserved["lock"]},
                                 "preserved_generation")
                store.database.close()
            preserved_retained(preserved["names"], REPORT["generations"])
            frozen_stack("frozen_worker_started_on_preserved_generation")
        REPORT["pointer"] = load_selection(ROOT, INSTALLATION)["generation"]
        REPORT["databases"] = [SOURCE["database"]] + ["qiban_restore_" + name for name in REPORT["generations"]]
        REPORT["passed"] = True
    except Exception as error:
        # Fixed fields only: a driver error text can carry connection details.
        REPORT["error"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
        write(EVIDENCE / "result.json", REPORT)
        raise
    finally:
        for host in list(HOSTS):
            try:
                if getattr(host, "process", None) is not None and host.process.poll() is None:
                    host.stop()
            except Exception:
                pass
            try:
                host.close()
            except Exception:
                pass
        HOSTS.clear()
        write(EVIDENCE / "result.json", REPORT)
    print(json.dumps({key: value for key, value in REPORT.items() if key != "details"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
