"""Desktop entry points interrupt native transport before waiting for its ledger."""
import threading
import time

import pytest

from tests_postgres.test_desktop_management import desktop_store as desktop_store
from tests_postgres.test_desktop_management import service as service
from tests_postgres.test_desktop_runtime import account_runtime


@pytest.mark.parametrize('operation', ['switch', 'stop', 'close'])
def test_desktop_interrupts_transport_before_waiting_for_dispatch_lock(service, tmp_path, operation):
    runtime, _, _ = account_runtime(service, tmp_path, object())
    run = runtime.start('qq')['run']
    entered, released = threading.Event(), threading.Event()
    interruptions = []

    def interrupt(generation):
        interruptions.append(generation)
        released.set()

    runtime.cores['qq'].platforms.all()[0].runtime_interrupt = interrupt

    def dispatch_lock():
        with service.store.lock:
            entered.set()
            released.wait(5)

    holder = threading.Thread(target=dispatch_lock)
    holder.start()
    assert entered.wait(2)
    try:
        before = time.monotonic()
        if operation == 'switch':
            runtime.change_model_account(lambda: {'committed': True}, ready=False)
        elif operation == 'stop':
            runtime.control(service.request(), 'qq', 'stop')
        else:
            runtime.close()
        assert time.monotonic() - before < 3
        assert released.is_set() and interruptions[0] == run['generation']
    finally:
        released.set()
        holder.join(2)
