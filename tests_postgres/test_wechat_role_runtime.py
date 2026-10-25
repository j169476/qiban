"""Existing encrypted core/model pipeline composed with a synthetic desktop only."""
from datetime import datetime, timezone

import pytest

from companion_core.contracts import stable_key
from companion_wechat.receiver import DatabaseGateway
from companion_wechat.role_runtime import run_role_test
from tests_postgres.test_wechat_role import Driver, Host, Model
from tests_wechat.test_receiver import Factory, Worker, batch, row, snapshot


@pytest.mark.asyncio
async def test_explicit_composition_runs_two_replies_and_closes_every_resource(pg_store):
    account = "synthetic-composed-role"
    initial = snapshot()

    class IncomingWorker(Worker):
        async def request(self, command, **arguments):
            if command == "read":
                state = pg_store.state(stable_key("wechat-role-session-v1", account))
                now = datetime.now(timezone.utc).isoformat()
                items = [row(1, text=state["nonce"] + " synthetic private"),
                         row(1, group=True, text=state["nonce"] + " synthetic group")]
                for item in items:
                    item["occurred_at"] = now
                return batch(initial, *items)[0]
            return await super().request(command, **arguments)

    worker = IncomingWorker([initial])
    receiver = DatabaseGateway(pg_store, {}, account=account, transport_factory=Factory(worker))
    model, driver, output = Model(), Driver(), []
    result = await run_role_test(receiver, model, Host(), driver, duration_seconds=60, emit=output.append)
    assert result["status"] == "complete" and result["succeeded"] == result["handled"] == 2
    assert result["role_stopped"] and receiver._closed and worker.closed and driver.closed
    assert len(model.calls) == len(driver.sent) == 2
    assert pg_store.state("core-run:wechat")["status"] == "paused"
    assert output[0]["status"] == "ready" and output[-1]["test"]["status"] == "revoked"
    with pg_store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM core_actions WHERE status='succeeded'").fetchone()[0] == 2
        assert all(row[0].startswith("enc1:") for row in db.execute("SELECT body FROM core_actions"))


@pytest.mark.asyncio
async def test_composition_failure_before_adapter_ownership_closes_driver_and_source(pg_store, monkeypatch):
    from companion_wechat import role

    worker = Worker([snapshot()])
    receiver = DatabaseGateway(pg_store, {}, account="synthetic-build-failure", transport_factory=Factory(worker))
    driver = Driver()

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic construction failure")

    monkeypatch.setattr(role, "RoleAdapter", fail)
    with pytest.raises(RuntimeError, match="synthetic construction failure"):
        await run_role_test(receiver, Model(), Host(), driver)
    assert receiver._closed and worker.closed and driver.closed
    assert pg_store.state("core-run:wechat", {}).get("status") != "active"
