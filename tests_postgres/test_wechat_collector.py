"""Synthetic collector tests; PostgreSQL cases use the dedicated test schema only."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_wechat import collector, receiver_cli
from companion_wechat.bindings import BindingRegistry, ConversationBinding
from companion_wechat.collector import Collector, public_report, verify_store

ACCOUNT = "synthetic-receive-only-account"
IDENTITY = "synthetic-self-identity"
BODY = "SYNTHETIC PRIVATE BODY NEVER IN OUTPUT"


class Gateway:
    def __init__(self, store):
        self.started = self.closed = self.pumps = 0
        bindings = BindingRegistry(store, ACCOUNT, IDENTITY)
        bindings.bind(ConversationBinding("private", "synthetic-private-target", "private", "synthetic-peer"))
        bindings.bind(ConversationBinding("family", "synthetic-family-target", "group"))
        self.connector = SimpleNamespace(platform="wechat", account=ACCOUNT, bindings=bindings,
                                         read_only_collection=True, capabilities=lambda: [], receive=self.receive)
        self.incoming = [self.event()]
        self.cursors = []

    @staticmethod
    def event(number="1"):
        return Event("wechat", ACCOUNT, number, ActorRef("wechat", "synthetic-peer", ACCOUNT), "private",
                     "2026-10-24T04:00:00+00:00", BODY, metadata={"wechat": {
                         "target_ref": "synthetic-private-target", "binding_revision": 1}})

    async def start(self):
        self.started += 1

    async def pump_once(self):
        self.pumps += 1
        return {"state": "connected", "queued": 1, "PRIVATE": BODY}

    async def receive(self, cursor):
        self.cursors.append(cursor)
        return self.incoming, {"wechat_stream": "synthetic-stream", "offset": 1}

    async def close(self):
        self.closed += 1

    def status(self):
        return {"state": "closed" if self.closed else "connected", "worker_connected": not self.closed}


@pytest.fixture
def env(pg_store):
    gateway = Gateway(pg_store)
    return pg_store, gateway, Collector(pg_store, gateway, ACCOUNT)


@pytest.mark.asyncio
async def test_once_commits_encrypted_events_and_cursor_without_reading_personal_records(env, monkeypatch):
    store, gateway, task = env
    verify_store(store)

    def forbidden(*_args, **_kwargs):
        pytest.fail("collector must not read content records, memories, or start a role")

    monkeypatch.setattr(store, "read", forbidden)
    monkeypatch.setattr(store, "records", forbidden)
    outputs = []
    result = await task.run(once=True, emit=outputs.append)
    assert result["status"] == "complete" and result["events_committed"] == 1
    assert gateway.started == gateway.closed == gateway.pumps == 1
    assert result["source"]["state"] == "closed" and result["source"]["worker_connected"] is False
    assert store.state(task.cursor_key) == {"wechat_stream": "synthetic-stream", "offset": 1}
    assert BODY not in json.dumps(outputs) and IDENTITY not in json.dumps(outputs)
    with store.db() as db:
        stored = db.execute("SELECT body FROM core_events").fetchone()[0]
        saved_cursor = db.execute("SELECT value FROM state WHERE key=?", (task.cursor_key,)).fetchone()[0]
        assert stored.startswith("enc1:") and BODY not in stored
        assert saved_cursor.startswith("enc1:") and "synthetic-stream" not in saved_cursor
        assert db.execute("SELECT COUNT(*) FROM core_actions").fetchone()[0] == 0
    assert store.state("core-run:wechat") is None


@pytest.mark.asyncio
async def test_recreated_collector_uses_committed_cursor_and_does_not_duplicate_events(env):
    store, gateway, task = env
    await task.run(once=True)
    again = Collector(store, gateway, ACCOUNT)
    result = await again.run(once=True)
    assert gateway.cursors[1] == store.state(task.cursor_key)
    assert result["events_committed"] == 0 and result["events_replayed"] == 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cursor_write_failure_rolls_back_events_and_ingress_metadata_together(env, monkeypatch):
    store, gateway, task = env
    original = store.set_state

    def fail_cursor(key, value, db=None):
        if key == task.cursor_key:
            raise AssistantError("database_error", "synthetic failure")
        return original(key, value, db)

    monkeypatch.setattr(store, "set_state", fail_cursor)
    with pytest.raises(AssistantError):
        await task.run(once=True)
    assert gateway.closed == 1 and not task._lock.is_locked
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM actor_nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM governance_policies WHERE resource LIKE 'event/%'").fetchone()[0] == 0
    assert store.state(task.cursor_key) is None


@pytest.mark.asyncio
async def test_invalid_second_event_rolls_back_the_first_event_and_cursor(env):
    store, gateway, task = env
    gateway.incoming.append(replace(gateway.event("2"), conversation="unbound"))
    with pytest.raises(AssistantError) as exc:
        await task.run(once=True)
    assert exc.value.code == "collector_scope_invalid"
    assert store.state(task.cursor_key) is None
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_changed_event_identity_cannot_overwrite_an_existing_body(env):
    store, gateway, task = env
    await task.run(once=True)
    gateway.incoming = [replace(gateway.event(), text="synthetic changed body")]
    with pytest.raises(AssistantError) as exc:
        await Collector(store, gateway, ACCOUNT).run(once=True)
    assert exc.value.code == "collector_event_conflict"
    with store.db() as db:
        body = db.execute("SELECT body FROM core_events").fetchone()[0]
    assert store.ledger_decode("core_events.body", body)["text"] == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("core-run:wechat", {"status": "active"}),
    ("core-run", {"status": "active", "accounts": [["wechat", ACCOUNT]]}),
])
async def test_active_wechat_role_is_rejected_before_worker_start(env, key, value):
    store, gateway, task = env
    store.set_state(key, value)
    with pytest.raises(AssistantError) as exc:
        await task.run(once=True)
    assert exc.value.code == "collector_role_conflict"
    assert gateway.started == 0 and gateway.closed == 1
    assert store.state(key) == value


@pytest.mark.asyncio
async def test_role_started_during_receive_prevents_event_commit(env):
    store, gateway, task = env
    original = gateway.connector.receive

    async def changed(cursor):
        result = await original(cursor)
        store.set_state("core-run:wechat", {"status": "active"})
        return result

    gateway.connector.receive = changed
    with pytest.raises(AssistantError) as exc:
        await task.run(once=True)
    assert exc.value.code == "collector_role_conflict"
    assert store.state(task.cursor_key) is None


@pytest.mark.asyncio
async def test_existing_other_platform_role_does_not_start_or_block_wechat_collector(env):
    store, _gateway, task = env
    value = {"status": "active", "accounts": [["bilibili", "synthetic-bili"]]}
    store.set_state("core-run:bilibili", value)
    assert (await task.run(once=True))["status"] == "complete"
    assert store.state("core-run:bilibili") == value


@pytest.mark.asyncio
async def test_second_collector_cannot_start_under_same_account_lock(env):
    store, gateway, first = env
    second = Collector(store, gateway, ACCOUNT)
    with first._lock:
        with pytest.raises(AssistantError) as exc:
            await second.run(once=True)
    assert exc.value.code == "collector_already_running" and gateway.started == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["capabilities", "flag", "third_target", "disabled"])
async def test_only_two_bound_targets_and_receive_only_connector_are_accepted(env, change):
    store, gateway, task = env
    if change == "capabilities":
        gateway.connector.capabilities = lambda: ["message.reply"]
    elif change == "flag":
        gateway.connector.read_only_collection = False
    else:
        key = gateway.connector.bindings.key
        state = store.state(key)
        if change == "third_target":
            state["conversations"]["third"] = dict(state["conversations"]["family"])
        else:
            state["conversations"]["family"]["enabled"] = False
        store.set_state(key, state)
    with pytest.raises(AssistantError):
        await task.run(once=True)
    assert gateway.closed == 1 and gateway.pumps == 0


@pytest.mark.asyncio
async def test_cancellation_closes_worker_and_releases_singleton_lock(env):
    _store, gateway, task = env

    async def cancel():
        raise asyncio.CancelledError()

    gateway.pump_once = cancel
    with pytest.raises(asyncio.CancelledError):
        await task.run(once=True)
    assert gateway.closed == 1 and not task._lock.is_locked


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_report_success(env):
    _store, gateway, task = env

    async def fail():
        raise RuntimeError("PRIVATE provider detail")

    gateway.close = fail
    outputs = []
    with pytest.raises(AssistantError) as exc:
        await task.run(once=True, emit=outputs.append)
    assert exc.value.code == "collector_cleanup_failed"
    assert not task._lock.is_locked
    assert "complete" not in [value["status"] for value in outputs]


@pytest.mark.asyncio
async def test_offline_gateway_can_drain_authenticated_queue_while_retrying(env):
    _store, gateway, task = env

    async def offline():
        return {"state": "retrying", "code": "worker_disconnected", "retry_in_seconds": 2,
                "worker_connected": False, "PRIVATE": BODY}

    gateway.pump_once = offline
    outputs = []
    result = await task.run(once=True, emit=outputs.append)
    assert result["events_committed"] == 1 and result["source"]["state"] == "closed"
    assert outputs[-2]["source"]["code"] == "worker_disconnected" and BODY not in json.dumps(outputs)


@pytest.mark.asyncio
async def test_fatal_source_status_drains_existing_queue_then_exits_with_fixed_code(env):
    store, gateway, task = env

    async def blocked():
        return {"state": "blocked", "code": "source_identity_changed"}

    gateway.pump_once = blocked
    outputs = []
    with pytest.raises(AssistantError) as exc:
        await task.run(once=True, emit=outputs.append)
    assert exc.value.code == "source_identity_changed"
    assert store.state(task.cursor_key)["offset"] == 1
    assert outputs[-1]["events_committed"] == 1


@pytest.mark.asyncio
async def test_idle_output_is_throttled_without_waiting_in_real_time(env):
    _store, gateway, task = env
    clock = [0.0]
    task.clock = lambda: clock[0]

    async def sleep(seconds):
        clock[0] += seconds

    async def idle():
        return {"state": "retrying", "code": "worker_disconnected", "retry_in_seconds": 60 - clock[0] % 60}

    task.sleep = sleep
    gateway.pump_once = idle
    gateway.incoming = []
    outputs = []
    result = await task.run(duration_seconds=125, poll_seconds=1, emit=outputs.append)
    assert result["polls"] == 125
    assert [item["elapsed_seconds"] for item in outputs] == [0.0, 0.0, 60.0, 120.0, 125.0]
    assert outputs[-1]["source"]["state"] == "closed"


def test_nonpostgres_store_has_no_fallback():
    with pytest.raises(AssistantError) as exc:
        Collector(SimpleNamespace(backend="sqlite"), object(), ACCOUNT)
    assert exc.value.code == "collector_store_required"


def test_public_reports_drop_unknown_fields_sensitive_strings_and_nonfinite_counts():
    value = public_report({"status": BODY, "events_committed": "PRIVATE", "polls": True,
                           "elapsed_seconds": float("nan"), "message": BODY, "path": IDENTITY,
                           "source": {"state": BODY, "code": BODY, "queued": BODY, "private": BODY}})
    assert value["status"] == "collector_failed" and value["polls"] == 0
    assert value["elapsed_seconds"] == 0 and BODY not in json.dumps(value)
    assert collector.safe_code(AssistantError(BODY, BODY)) == "collector_failed"


def test_store_factory_does_not_use_role_or_content_validation(tmp_path, monkeypatch):
    import bili_assistant.profiles
    import companion_core.postgres
    import companion_service.config
    import companion_service.managed_postgres

    calls = []
    config = {"id": "synthetic-installation", "data": tmp_path,
              "database": {"host": "127.0.0.1", "dbname": "synthetic"}}
    secret = {"database_password": "SYNTHETIC SECRET", "key": bytes(32), "bridge_secret": "unused"}
    store = SimpleNamespace(database=SimpleNamespace(close=lambda: calls.append("close")))
    monkeypatch.setattr(companion_service.config, "metadata", lambda _p: config)
    monkeypatch.setattr(companion_service.config, "secrets_for", lambda _c: secret)
    monkeypatch.setattr(companion_service.managed_postgres, "ensure", lambda _c: calls.append("ensure"))
    monkeypatch.setattr(bili_assistant.profiles, "profile", lambda: "checked-in-profile")

    def build(root, options, key, profile):
        assert root == tmp_path and key == bytes(32) and profile == "checked-in-profile"
        assert options["password"] == "SYNTHETIC SECRET"
        calls.append("construct")
        return store

    monkeypatch.setattr(companion_core.postgres, "PostgresStore", build)
    monkeypatch.setattr(receiver_cli, "verify_store", lambda value: calls.append("verify"))
    with receiver_cli.open_collector_store(tmp_path) as (opened, account):
        assert opened is store and account == collector.collector_account(config["id"])
        assert secret == {}
    assert calls == ["ensure", "construct", "verify", "close"]


def test_standalone_wheel_reports_missing_service_dependency_without_raw_import_error(monkeypatch, tmp_path):
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "companion_service.config":
            raise ModuleNotFoundError("SYNTHETIC PRIVATE INSTALL PATH")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(AssistantError) as exc, receiver_cli.open_collector_store(tmp_path):
        pytest.fail("missing installation package must fail closed")
    assert exc.value.code == "collector_service_dependency_missing"
    assert "PRIVATE" not in str(exc.value)


def test_cli_unknown_exception_and_parser_errors_emit_only_fixed_json(monkeypatch, capsys):
    async def fail(_args, _emit):
        raise RuntimeError(BODY + IDENTITY)

    monkeypatch.setattr(receiver_cli, "_run", fail)
    assert receiver_cli.main(["--locations", "synthetic", "--targets", "synthetic", "--once"]) == 2
    assert receiver_cli.main(["--unknown-argument", BODY]) == 2
    output = capsys.readouterr()
    values = [json.loads(line) for line in output.out.splitlines()]
    assert [value["status"] for value in values] == ["collector_failed", "collector_configuration_invalid"]
    assert BODY not in output.out and not output.err


@pytest.mark.parametrize("options", [["--poll-seconds", "nan"], ["--poll-seconds", "0"],
                                     ["--duration-seconds", "0"], ["--duration-seconds", "-1"]])
def test_invalid_options_do_not_open_store_or_source(monkeypatch, capsys, options):
    async def forbidden(*_args):
        pytest.fail("invalid options must fail before opening the store or source")

    monkeypatch.setattr(receiver_cli, "_run", forbidden)
    assert receiver_cli.main(["--locations", "synthetic", "--targets", "synthetic", *options]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "collector_configuration_invalid"


def test_cli_worker_python_and_factory_are_passed_without_credentials(tmp_path, monkeypatch):
    import sys

    python = tmp_path / "python.exe"
    python.write_bytes(b"synthetic executable placeholder; never launched")
    seen = {}

    @contextmanager
    def opened(project):
        seen["project"] = project
        yield "synthetic-store", ACCOUNT

    class FakeCollector:
        def __init__(self, store, gateway, account):
            assert store == "synthetic-store" and account == ACCOUNT
            seen["gateway"] = gateway

        async def run(self, **kwargs):
            seen["run"] = kwargs
            return {"status": "complete"}

    def gateway(store, config, **kwargs):
        seen["transport"] = kwargs["transport_factory"]()
        assert kwargs["account"] == ACCOUNT and config == {"synthetic": True}
        return "synthetic-gateway"

    monkeypatch.setattr(receiver_cli, "open_collector_store", opened)
    monkeypatch.setattr(receiver_cli, "load_config", lambda *_args: {"synthetic": True})
    monkeypatch.setattr(receiver_cli, "Collector", FakeCollector)
    monkeypatch.setitem(sys.modules, "companion_wechat.receiver", SimpleNamespace(DatabaseGateway=gateway))
    monkeypatch.setitem(sys.modules, "companion_wechat.receiver_transport", SimpleNamespace(
        SubprocessTransport=lambda **kwargs: kwargs))
    assert receiver_cli.main(["--project", str(tmp_path), "--locations", "synthetic", "--targets", "synthetic",
                              "--worker-python", str(python), "--once"]) == 0
    assert seen["transport"] == {"python": str(python.resolve())}
    assert seen["run"]["once"] is True and seen["run"]["duration_seconds"] is None


def test_package_entrypoint_routes_collect_without_starting_probe(monkeypatch):
    from companion_wechat import __main__

    monkeypatch.setattr(receiver_cli, "main", lambda argv: 77 if argv == ["--help"] else 2)
    assert __main__.main(["collect", "--help"]) == 77
