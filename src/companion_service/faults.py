"""Reuse the existing management-only, at-most-once notification mechanism."""
from companion_core.storage import utcnow


def record(service, code, *, incident=None, run_key="core-run"):
    from bili_assistant.health import BENIGN_STOPS, FAULT_TEXT

    if code in BENIGN_STOPS or code in {"control_inactive", "explicit_stop"}:
        return
    run = service.store.state(run_key, {})
    generation = "backup-" + incident if code == "backup_failed" and incident else run.get("generation")
    if not generation:
        return
    with service.store.lock, service.store.db() as db:
        key = "fault:" + generation
        if service.store.state(key, db=db):
            return
        service.store.set_state(key, {"id": generation, "generation": generation, "account": "",
            "code": code if code in FAULT_TEXT else "unexpected_daemon_error", "created_at": utcnow(),
            "owner": "" if code == "backup_failed" else run["owner"], "notification": "queued"}, db)


def deliver(service):
    from bili_assistant.engine import Engine
    from bili_assistant.health import deliver_faults

    return deliver_faults(Engine(service.store, service.host))
