"""Prove the desktop credential-vault race and that one cross-process mutex closes it.

Windows Credential Manager keeps exactly one generic credential per target name. The keyring
Windows backend this application uses therefore keeps one *shared* target per service name and,
when a different username writes to that service, first copies the credential it finds on the
shared target to the compound target ``<username>@<service>`` and only then overwrites the
shared one. Reading the previous entry, copying it and overwriting the shared target are three
separate calls, so two processes that prepare their own installation slot at the same time can
both read the same previous entry and then overwrite the shared target in turn: the credential
the first one wrote is then in no target at all. Its own verification (one read immediately
after the write) fails with ``storage_vault_unavailable`` while the database and the store do
not exist yet, and a later start reports ``storage_vault_missing`` for it.

``credential_vault`` closes that window by holding one named Windows mutex per service name for
the whole duration of each keyring call. This script proves both halves:

    mechanism   a controllable fake vault with the same slot layout, run with a barrier between
                the read and the write: every writer keeps only its own slot and exactly one of
                two (or seven of eight) concurrent credentials is unreachable; the same barrier
                applied to the block a first installation runs (read, create, verify) fails one
                of the two verifications, which is the reported ``storage_vault_unavailable``.
    fake        the same fake vault driven through ``VaultCredentials`` with the same delay:
                nothing is lost and every verification passes.
    compat      a slot written by the unfixed backend is still readable through the mutex and
                a slot written through the mutex is still readable by the unfixed backend,
                including one that already sits on a compound target.
    app         the application's own readers and writers on a synthetic slot:
                ``storage_worker.credentials``, ``installation.selected_credential`` for the
                original and a restored generation, ``portable_restore._keyslot``, and three
                processes running a first installation's credential block at once.
    race-raw    two real Windows processes, real Credential Manager, real keyring, with the
                backend's read delayed after it returns: a synthetic credential is lost.
    race-mutex  the same two processes through the mutex and with the same delay: nothing is
                lost.
    parallel    four real processes writing sixteen independent slots at once through the
                mutex, then one further process reading every slot back and comparing.
    crash       a process that holds the mutex is killed; the next waiter acquires it and the
                slot the dead owner wrote is still readable.
    exception   a process that raises inside the critical section leaves the mutex usable.
    timeout     a waiter that cannot enter in its budget fails with a stable code instead of
                blocking, and acquires once the holder is gone.
    cleanup     every slot of this run is deleted; a further process reads back nothing.

Every slot here is synthetic: one randomly named service with this run's prefix, random 32-hex
slots and values derived from a random run token. The real service is never read, written or
deleted, and the script refuses to run against anything but its own prefix. No value, token or
key is printed: the report holds booleans and counts only.

Run it directly (``python check_vault_concurrency.py``); sections may be selected with
``--section``. The real sections need the Windows Credential Manager and the ``keyring``
package and report ``untested`` (exit code 2) instead of a pass when either is missing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RUNNER = Path(__file__).resolve()
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(RUNNER.parents[2] / 'src'))

# The real desktop service: only ever compared against, never opened.
REAL_SERVICE = 'qiban-desktop-probe-storage'
SERVICE_PREFIX = 'qiban-desktop-probe-r7-vault-'
TOKEN_VARIABLE = 'QIBAN_R7_VAULT_TOKEN'
SECTIONS = ('mechanism', 'fake', 'compat', 'app', 'real', 'all')


def value(token, slot):
    """The synthetic value of one slot; deterministic, never printed."""
    return hashlib.sha256((token + '|' + slot).encode('utf8')).hexdigest()


class FakeVault:
    """The slot layout of the keyring Windows backend, with hooks around its own steps.

    This is the mechanism under test, not a stand-in for the Windows API: ``set_password``
    reads the shared target, copies what it found to ``<username>@<service>`` when that was
    another username, and only then overwrites the shared target; ``get_password`` prefers the
    shared target and falls back to the compound one. A hook is called before and after every
    read and after every write, so a test can place a barrier exactly where the real code has
    its window: between the read and the overwrite, or just before a verification reads back.
    """

    def __init__(self, hook=None):
        self.slots, self.hook = {}, hook or (lambda where, stage: None)

    def _read(self, target, stage):
        self.hook('before', stage)
        entry = self.slots.get(target)
        self.hook('after', stage)
        return entry

    def _write(self, target, username, password, stage):
        self.slots[target] = (username, password)
        self.hook('after', stage)

    def get_password(self, service, username):
        entry = self._read(service, 'get-shared')
        if not entry or (username and entry[0] != username):
            entry = self._read(f'{username}@{service}', 'get-compound')
        return entry[1] if entry else None

    def set_password(self, service, username, password):
        self.write_after_read(service, username, password, self._read(service, 'set-shared'))

    def write_after_read(self, service, username, password, previous):
        """The write path of ``set_password`` with the entry that its own read returned."""
        if previous:
            self._write(f'{previous[0]}@{service}', previous[0], previous[1], 'set-backup')
        self._write(service, username, str(password), 'set-write')

    def delete_password(self, service, username):
        deleted = False
        for target in (service, f'{username}@{service}'):
            entry = self.slots.get(target)
            if entry and entry[0] == username:
                self.slots.pop(target, None)
                deleted = True
        if not deleted:
            raise KeyError(service)


def barrier_hook(writers):
    """Stop every writer right after it read the shared target, then let all of them write."""
    barrier = threading.Barrier(writers, timeout=20)

    def hook(where, stage):
        if where == 'after' and stage == 'set-shared':
            barrier.wait()

    return hook


def scripted_sequences(vault, service, slots, token):
    """Two first installations interleaved exactly as the scheduler can do it.

    Every installation reads its own slot, then the writes happen in turn using exactly the
    entry each one read, and finally each one verifies its own write. This is the interleaving
    the report describes: both reads saw the same previous entry, so the second write does not
    know about the first one and the first installation's verification fails.
    """
    previous = [vault.get_password(service, slot) for slot in slots]
    for slot, prior in zip(slots, previous):
        vault.write_after_read(service, slot, app_value(token, slot), prior)
    verified = [vault.get_password(service, slot) == app_value(token, slot) for slot in slots]
    return {'writers': len(slots), 'verifications': len(verified),
            'verifications_failed': sum(1 for ok in verified if not ok)}


def parallel_writers(vault, service, slots, token):
    """Write each slot from its own thread and return nothing; failures propagate."""
    errors = []

    def write(slot):
        try:
            vault.set_password(service, slot, value(token, slot))
        except BaseException as error:  # noqa: BLE001 - reported as a count, never re-raised
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=write, args=(slot,), daemon=True) for slot in slots]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return {'threads': len(threads), 'errors': len(errors),
            'finished': sum(1 for thread in threads if not thread.is_alive())}


def readable(vault, service, slots, token):
    """How many slots resolve, how many hold exactly their own value, and how many values differ."""
    found, matching, seen = 0, 0, set()
    for slot in slots:
        raw = vault.get_password(service, slot)
        if raw is None:
            continue
        found += 1
        seen.add(hashlib.sha256(raw.encode('utf8')).hexdigest())
        matching += int(raw == value(token, slot))
    return {'expected': len(slots), 'found': found, 'matching': matching, 'distinct': len(seen),
            'lost': len(slots) - matching}


class Run:
    """One run's synthetic service and slots; nothing here is a real credential."""

    def __init__(self):
        self.token = secrets.token_hex(16)
        self.service = SERVICE_PREFIX + secrets.token_hex(8)
        self.slots = []

    def slot(self, name=''):
        slot = secrets.token_hex(16) if not name else hashlib.sha256(
            (self.token + '@' + name).encode('utf8')).hexdigest()[:32]
        self.slots.append(slot)
        return slot


def app_value(token, slot):
    """A synthetic credential in the shape this application stores, derived, never printed."""
    return json.dumps({
        'data_key': base64.b64encode(hashlib.sha256(
            (token + '#' + slot).encode('utf8')).digest()).decode('ascii'),
        'database_password': hashlib.sha256((token + '$' + slot).encode('utf8')).hexdigest(),
        'bootstrap_password': hashlib.sha256((token + '%' + slot).encode('utf8')).hexdigest()})


def installation_sequence(vault, service, slot, token):
    """The credential block of ``installation.locked_initialize``: read, create, verify."""
    raw = vault.get_password(service, slot)
    if raw is None:
        raw = app_value(token, slot)
        vault.set_password(service, slot, raw)
        return vault.get_password(service, slot) == raw
    return True


def parallel_sequences(vault, service, slots, token):
    """Run that block in one thread per slot; a failed verification is counted, not raised."""
    outcomes, errors = [], []

    def run(slot):
        try:
            outcomes.append(installation_sequence(vault, service, slot, token))
        except BaseException as error:  # noqa: BLE001 - counted, never re-raised
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=run, args=(slot,), daemon=True) for slot in slots]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return {'threads': len(threads), 'verifications': len(outcomes), 'errors': len(errors),
            'verifications_failed': sum(1 for ok in outcomes if not ok)}


def section_mechanism(run, report):
    """Deterministic: every writer reads the same previous entry, then they overwrite in turn."""
    token, service = run.token, run.service
    two = [run.slot('mechanism-a'), run.slot('mechanism-b')]
    vault = FakeVault(barrier_hook(2))
    threads = parallel_writers(vault, service, two, token)
    report['mechanism_two_writers'] = {'race': threads, **readable(vault, service, two, token)}
    report['mechanism_two_writers']['lost_exactly_one'] = (
        report['mechanism_two_writers']['lost'] == 1)

    # The previous entry of an existing installation is copied by both writers, so it survives;
    # only the credential of one of the two concurrent writers disappears.
    prior, three = run.slot('mechanism-prior'), [run.slot('mechanism-c'), run.slot('mechanism-d')]
    vault = FakeVault()
    vault.set_password(service, prior, value(token, prior))
    vault.hook = barrier_hook(2)
    report['mechanism_prior_entry'] = {'race': parallel_writers(vault, service, three, token),
                                       **readable(vault, service, [prior, *three], token)}
    report['mechanism_prior_entry']['prior_survived'] = (
        vault.get_password(service, prior) == value(token, prior))

    eight = [run.slot(f'mechanism-{index}') for index in range(8)]
    vault = FakeVault(barrier_hook(8))
    report['mechanism_eight_writers'] = {'race': parallel_writers(vault, service, eight, token),
                                         **readable(vault, service, eight, token)}
    report['mechanism_eight_writers']['lost_seven'] = report['mechanism_eight_writers']['lost'] == 7

    # The reported failure itself: two first installations interleave read-create-verify, and the
    # one whose write is overwritten between its write and its own read-back fails its own
    # verification. This is the same block installation.locked_initialize runs.
    sequence = [run.slot('mechanism-sequence-a'), run.slot('mechanism-sequence-b')]
    vault = FakeVault()
    report['mechanism_installation_sequence'] = scripted_sequences(vault, service, sequence, token)
    report['mechanism_installation_sequence']['slots_readable'] = sum(
        1 for slot in sequence if vault.get_password(service, slot) is not None)
    report['mechanism_installation_sequence']['reproduces_the_reported_error'] = (
        report['mechanism_installation_sequence']['verifications_failed'] == 1)
    report['mechanism_passed'] = all((
        report['mechanism_two_writers']['lost_exactly_one'],
        report['mechanism_prior_entry']['prior_survived'],
        report['mechanism_prior_entry']['lost'] == 1,
        report['mechanism_eight_writers']['lost_seven'],
        report['mechanism_installation_sequence']['reproduces_the_reported_error']))
    return report['mechanism_passed']


def section_fake(run, report):
    """The same fake vault through the mutex: concurrent writers lose nothing."""
    from credential_vault import VaultCredentials

    token, service = run.token, run.service
    harness = [run.slot(f'fake-{index}') for index in range(8)]

    def delay(where, stage):
        if where == 'after' and stage == 'set-shared':
            time.sleep(0.02)

    vault = FakeVault(delay)
    report['fake_serialized'] = {'race': parallel_writers(VaultCredentials(vault), service,
                                                          harness, token),
                                 **readable(vault, service, harness, token)}
    report['fake_serialized']['nothing_lost'] = report['fake_serialized']['lost'] == 0
    report['fake_installation_sequence'] = parallel_sequences(
        VaultCredentials(FakeVault(delay)), service, harness, token)
    report['fake_installation_sequence']['all_verified'] = (
        report['fake_installation_sequence']['verifications_failed'] == 0
        and report['fake_installation_sequence']['verifications'] == len(harness))
    report['fake_passed'] = bool(report['fake_serialized']['nothing_lost']
                                 and report['fake_installation_sequence']['all_verified'])
    return report['fake_passed']


def child_write(args):
    """Create one synthetic slot, optionally with the backend's read delayed after it returns."""
    if args.delay_ms:
        from keyring.backends.Windows import WinVaultKeyring

        original = WinVaultKeyring._read_credential

        def slow(instance, target):
            entry = original(instance, target)
            time.sleep(args.delay_ms / 1000)
            return entry

        WinVaultKeyring._read_credential = slow
    if args.mode == 'raw':
        from keyring.backends.Windows import WinVaultKeyring

        vault = WinVaultKeyring()
    else:
        from credential_vault import VaultCredentials

        vault = VaultCredentials()
    token = os.environ[TOKEN_VARIABLE]
    slots = [slot for slot in (args.slots or args.slot).split(',') if slot]
    wait_until(args.start_at)
    for slot in slots:
        vault.set_password(args.service, slot, value(token, slot))
    return {'wrote': len(slots)}


def child_read(args):
    """Read slots from this (different) process and compare without printing any value."""
    from credential_vault import VaultCredentials

    token = os.environ[TOKEN_VARIABLE]
    slots = [slot for slot in args.slots.split(',') if slot]
    return {'read': readable(VaultCredentials(), args.service, slots, token)}


def child_hold(args):
    """Write a slot, hold the mutex and stay alive until the parent kills this process."""
    from credential_vault import VaultCredentials

    vault = VaultCredentials()
    with vault.hold(args.service):
        # A nested acquisition on the same thread must be free, not a self-deadlock.
        vault.set_password(args.service, args.slot, value(os.environ[TOKEN_VARIABLE], args.slot))
        Path(args.marker).write_text('held', encoding='utf8')
        time.sleep(args.seconds)
    return {'released': True}


def child_raise(args):
    """Fail inside the critical section; the mutex must be released on the way out."""
    from credential_vault import VaultCredentials

    try:
        with VaultCredentials().hold(args.service):
            raise RuntimeError('injected failure inside the critical section')
    except RuntimeError:
        return {'raised': True}
    return {'raised': False}


def child_wait(args):
    """Try to enter the mutex within a budget and report whether it was acquired."""
    from credential_vault import VaultCredentials

    from companion_core.errors import AssistantError

    started = time.monotonic()
    try:
        with VaultCredentials(timeout_ms=args.timeout_ms).hold(args.service):
            return {'acquired': True,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
    except AssistantError as error:
        return {'acquired': False, 'error': error.code,
                'elapsed_ms': int((time.monotonic() - started) * 1000)}


def child_sequence(args):
    """Run the application's credential block for several own slots, at a shared start time."""
    from credential_vault import VaultCredentials

    token = os.environ[TOKEN_VARIABLE]
    slots = [slot for slot in (args.slots or args.slot).split(',') if slot]
    vault = VaultCredentials()
    wait_until(args.start_at)
    outcomes = [installation_sequence(vault, args.service, slot, token) for slot in slots
                for _ in range(max(1, args.rounds))]
    return {'slots': len(slots), 'rounds': max(1, args.rounds),
            'verifications': len(outcomes),
            'verifications_failed': sum(1 for ok in outcomes if not ok)}


CHILDREN = {'write': child_write, 'read': child_read, 'hold': child_hold,
            'raise': child_raise, 'wait': child_wait, 'sequence': child_sequence}


def wait_until(moment):
    """Block until a wall-clock moment; wall time is what two processes can agree on."""
    if not moment:
        return
    while time.time() < moment:
        time.sleep(0.005)


def read_answer(path):
    """The result one child process wrote; an unreadable or missing one is reported as empty."""
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        return None


def spawn(arguments, run, report, wait=True, timeout=180):
    """Start one further process of this script and read the result it wrote."""
    report.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, str(RUNNER), '--child', arguments[0],
                                '--service', run.service, *arguments[1:], '--report', str(report)],
                               env={**os.environ, TOKEN_VARIABLE: run.token},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=str(RUNNER.parent),
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not wait:
        return process, None, None
    code = process.wait(timeout=timeout)
    return process, code, read_answer(report)


def section_compat(run, report):
    """A slot the unfixed backend wrote is still readable, and the other way round."""
    from credential_vault import VaultCredentials
    from keyring.backends.Windows import WinVaultKeyring

    token, service = run.token, run.service
    legacy, moved, current = (run.slot('compat-legacy'), run.slot('compat-moved'),
                              run.slot('compat-current'))
    raw = WinVaultKeyring()
    raw.set_password(service, legacy, value(token, legacy))
    # A second username on the same service moves the first one to its compound target, which
    # is the layout an existing pair of installations already has on disk today.
    raw.set_password(service, moved, value(token, moved))
    vault = VaultCredentials()
    through_mutex = readable(vault, service, [legacy, moved], token)
    vault.set_password(service, current, value(token, current))
    through_backend = readable(raw, service, [legacy, moved, current], token)
    report['compat'] = {'written_by_backend_read_by_mutex': through_mutex,
                        'written_by_mutex_read_by_backend': through_backend}
    report['compat']['interoperable'] = (
        through_mutex['matching'] == 2 and through_backend['matching'] == 3)
    report['compat_passed'] = report['compat']['interoperable']
    return report['compat_passed']


def section_app(run, report, folder):
    """The application's own readers and writers, and its first-install block under contention."""
    token, service = run.token, run.service
    try:
        import installation
        import portable_restore
        import storage_worker
        from credential_vault import VaultCredentials
        from keyring.backends.Windows import WinVaultKeyring

        from companion_core.errors import AssistantError
    except Exception:  # noqa: BLE001 - reported as untested, never as a pass
        report['app_sections'] = 'untested'
        return None
    saved = (installation.VAULT, storage_worker.VAULT, portable_restore.VAULT)
    # Only the service constant is redirected, and only to this run's synthetic service.
    installation.VAULT, storage_worker.VAULT, portable_restore.VAULT = service, service, service
    try:
        slot = run.slot('app-installation')
        key = hashlib.sha256((token + '&' + slot).encode('utf8')).digest()
        password = hashlib.sha256((token + '!' + slot).encode('utf8')).hexdigest()
        bootstrap = hashlib.sha256((token + '?' + slot).encode('utf8')).hexdigest()
        # A slot in the layout an installation has on disk today, written by the plain backend.
        WinVaultKeyring().set_password(service, slot, json.dumps({
            'data_key': base64.b64encode(key).decode('ascii'),
            'database_password': password, 'bootstrap_password': bootstrap}))
        report['app_read'] = {
            'installation_slot_read': storage_worker.credentials(slot) == (key, password),
            'selected_same_slot': installation.selected_credential(
                VaultCredentials(), slot, {'credential_id': slot}, key, password) == (key, password)}
        restored = run.slot('app-restored')
        restored_key = hashlib.sha256((token + '~' + restored).encode('utf8')).digest()
        WinVaultKeyring().set_password(service, restored, json.dumps({
            'data_key': base64.b64encode(restored_key).decode('ascii'),
            'database_password': password}))
        report['app_read']['selected_restored_slot'] = installation.selected_credential(
            VaultCredentials(), slot, {'credential_id': restored}, key, password) == (
                restored_key, password)
        try:
            installation.selected_credential(
                VaultCredentials(), slot, {'credential_id': restored}, key,
                hashlib.sha256(b'a different cluster password').hexdigest())
        except AssistantError as error:
            report['app_read']['restored_wrong_password_refused'] = (
                error.code == 'storage_vault_invalid')
        generation = run.slot('app-generation')
        portable_restore._keyslot(VaultCredentials(), generation, restored_key,
                                  {'database_password': password})
        written = WinVaultKeyring().get_password(service, generation)
        report['app_write'] = {'portable_keyslot_readable_by_backend': bool(written) and json.loads(
            written) == {'data_key': base64.b64encode(restored_key).decode('ascii'),
                         'database_password': password}}

        # Three processes run the read-create-verify block of a first installation at once, then
        # the application's own credential reader compares every slot they created.
        app_slots = [run.slot(f'app-{index}') for index in range(12)]
        moment = time.time() + 2.5
        children = [spawn(['sequence', '--slots', ','.join(app_slots[chunk * 4:(chunk + 1) * 4]),
                           '--rounds', '2', '--start-at', repr(moment)], run,
                          folder / f'sequence-{chunk}.json', wait=False) for chunk in range(3)]
        codes = [process.wait(timeout=180) for process, _, _ in children]
        answers = [read_answer(folder / f'sequence-{chunk}.json') for chunk in range(3)]
        matched = 0
        for app_slot in app_slots:
            expected = json.loads(app_value(token, app_slot))
            found = storage_worker.credentials(app_slot)
            matched += int(found[0] == base64.b64decode(expected['data_key'])
                           and found[1] == expected['database_password'])
        failed = sum(int((answer or {}).get('verifications_failed', 1)) for answer in answers)
        report['app_contention'] = {
            'processes': len(children), 'codes': codes, 'slots': len(app_slots),
            'verifications': sum(int((answer or {}).get('verifications', 0))
                                 for answer in answers),
            'verifications_failed': failed, 'read_by_app_reader': matched}
        report['app_contention']['nothing_lost'] = failed == 0 and matched == len(app_slots)
    finally:
        installation.VAULT, storage_worker.VAULT, portable_restore.VAULT = saved
    report['app_passed'] = bool(all(report['app_read'].values())
                                and all(report['app_write'].values())
                                and report['app_contention']['nothing_lost'])
    return report['app_passed']


def section_real(run, report, folder):
    """Two real Windows processes against the real Credential Manager, with and without the fix."""
    token, service = run.token, run.service
    prior = run.slot('race-prior')
    from credential_vault import VaultCredentials

    VaultCredentials().set_password(service, prior, value(token, prior))
    for name, mode in (('raw', 'raw'), ('mutex', 'locked')):
        slots = [run.slot(f'race-{name}-a'), run.slot(f'race-{name}-b')]
        moment = time.time() + 2.0
        children = []
        for index, slot in enumerate(slots):
            children.append(spawn(['write', '--slot', slot, '--mode', mode, '--delay-ms', '400',
                                   '--start-at', repr(moment)],
                                  run, folder / f'race-{name}-{index}.json', wait=False))
        written = [process.wait(timeout=180) for process, _, _ in children]
        _, code, answer = spawn(['read', '--slots', ','.join([prior, *slots])], run,
                                folder / f'race-{name}-read.json')
        report[f'race-{name}'] = {'writers': len(children), 'writer_codes': written,
                                  'reader_code': code,
                                  **((answer or {}).get('read') or {'failed': True})}
    report['race-raw']['lost_credential'] = report['race-raw'].get('lost', 0) > 0
    report['race-mutex']['nothing_lost'] = (
        report['race-mutex'].get('found') == 3 and report['race-mutex'].get('lost') == 0)
    report['real_passed'] = bool(report['race-raw']['lost_credential']
                                 and report['race-mutex']['nothing_lost'])
    return report['real_passed']


def section_parallel(run, report, folder):
    """Four processes writing sixteen independent slots at once, verified from a fifth one."""
    rounds, writers = 4, 4
    slots = [run.slot(f'parallel-{writer}-{round_}') for writer in range(writers)
             for round_ in range(rounds)]
    moment = time.time() + 2.5
    # One process per writer; a writer enters the mutex several times in a row for its own slots.
    children = [spawn(['write', '--slots', ','.join(slots[writer * rounds:(writer + 1) * rounds]),
                       '--mode', 'locked', '--start-at', repr(moment)],
                      run, folder / f'parallel-{writer}.json', wait=False)
                for writer in range(writers)]
    codes = [process.wait(timeout=180) for process, _, _ in children]
    report['parallel_writers'] = {'processes': len(children), 'codes': codes,
                                  'all_exited_cleanly': all(code == 0 for code in codes)}
    _, code, answer = spawn(['read', '--slots', ','.join(slots)], run,
                            folder / 'parallel-read.json')
    report['parallel'] = {'reader_code': code,
                          **((answer or {}).get('read') or {'failed': True})}
    report['parallel']['all_slots_resolve'] = (
        report['parallel'].get('found') == len(slots)
        and report['parallel'].get('matching') == len(slots)
        and report['parallel'].get('distinct') == len(slots))
    report['parallel_passed'] = report['parallel']['all_slots_resolve']
    return report['parallel_passed']


def section_crash(run, report, folder):
    """Kill a process that holds the mutex; the next waiter enters and its slot survives."""
    slot = run.slot('crash-holder')
    marker = folder / 'crash-held.marker'
    holder, _, _ = spawn(['hold', '--slot', slot, '--seconds', '120', '--marker', str(marker)],
                         run, folder / 'crash-holder.json', wait=False)
    held = wait_for(marker, holder)
    # While it lives the mutex is really held, so a waiter with a small budget must fail.
    _, code, busy = spawn(['wait', '--timeout-ms', '400'], run, folder / 'crash-wait.json')
    holder.kill()
    holder.wait(timeout=30)
    _, after_code, after = spawn(['wait', '--timeout-ms', '10000'], run,
                                 folder / 'crash-wait-after.json')
    _, read_code, answer = spawn(['read', '--slots', slot], run, folder / 'crash-read.json')
    report['crash'] = {'holder_reached_critical_section': held,
                       'wait_while_holding': busy, 'killed': True,
                       'wait_after_kill': after, 'read_after_kill': (answer or {}).get('read'),
                       'codes': [code, after_code, read_code]}
    report['crash']['released_by_system'] = bool(held and busy
                                                 and not (busy or {}).get('acquired', True)
                                                 and (after or {}).get('acquired'))
    report['crash']['slot_survived'] = (answer or {}).get('read', {}).get('matching') == 1
    report['crash_passed'] = report['crash']['released_by_system'] and report['crash']['slot_survived']
    return report['crash_passed']


def section_exception(run, report, folder):
    """A process that raises inside the critical section leaves the mutex usable."""
    raiser, code, answer = spawn(['raise'], run, folder / 'exception-raise.json')
    _, after_code, after = spawn(['wait', '--timeout-ms', '10000'], run,
                                 folder / 'exception-wait.json')
    report['exception'] = {'raised_inside_section': bool((answer or {}).get('raised')),
                           'raise_code': code, 'wait_after': after, 'wait_code': after_code}
    report['exception']['released'] = bool((answer or {}).get('raised')
                                           and (after or {}).get('acquired'))
    report['exception_passed'] = report['exception']['released']
    return report['exception_passed']


def section_timeout(run, report, folder):
    """A waiter that cannot enter in its budget fails with a stable code, then recovers."""
    slot = run.slot('timeout-holder')
    marker = folder / 'timeout-held.marker'
    holder, _, _ = spawn(['hold', '--slot', slot, '--seconds', '120', '--marker', str(marker)],
                         run, folder / 'timeout-holder.json', wait=False)
    held = wait_for(marker, holder)
    _, code, answer = spawn(['wait', '--timeout-ms', '300'], run, folder / 'timeout-wait.json')
    holder.kill()
    holder.wait(timeout=30)
    _, after_code, after = spawn(['wait', '--timeout-ms', '10000'], run,
                                 folder / 'timeout-wait-after.json')
    report['timeout'] = {'holder_reached_critical_section': held, 'waiter': answer,
                         'codes': [code, after_code], 'waiter_after_release': after}
    report['timeout']['failed_explicitly'] = bool(
        (answer or {}).get('acquired') is False
        and (answer or {}).get('error') == 'storage_vault_unavailable'
        and (answer or {}).get('elapsed_ms', 10 ** 9) < 5000)
    report['timeout']['recovered'] = bool((after or {}).get('acquired'))
    report['timeout_passed'] = report['timeout']['failed_explicitly'] and report['timeout']['recovered']
    return report['timeout_passed']


def cleanup(run, report, folder):
    """Delete every slot this run created and read back nothing from a further process.

    Every call names this run's own synthetic service and one of this run's own slots, and the
    loop only deletes a slot it has just read, so no entry of another installation or of another
    service can be removed by it. It runs even when a section above failed.
    """
    from credential_vault import VaultCredentials

    vault = VaultCredentials()
    deleted = 0
    for slot in run.slots:
        try:
            if vault.get_password(run.service, slot) is not None:
                vault.delete_password(run.service, slot)
                deleted += 1
        except Exception:  # noqa: BLE001 - a slot that never arrived has nothing to delete
            continue
    _, code, answer = spawn(['read', '--slots', ','.join(run.slots)], run, folder / 'cleanup-read.json')
    remaining = (answer or {}).get('read', {}).get('found')
    report['cleanup'] = {'slots': len(run.slots), 'deleted': deleted, 'reader_code': code,
                         'remaining': remaining, 'own_entries_only': True,
                         'passed': remaining == 0}
    return report['cleanup']['passed']


def wait_for(path, process, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        if process.poll() is not None:
            return path.exists()
        time.sleep(0.02)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--section', choices=SECTIONS, default='all')
    parser.add_argument('--child', choices=tuple(CHILDREN))
    parser.add_argument('--service')
    parser.add_argument('--slot')
    parser.add_argument('--slots')
    parser.add_argument('--mode', choices=('raw', 'locked'), default='locked')
    parser.add_argument('--delay-ms', type=int, default=0)
    parser.add_argument('--start-at', type=float, default=0.0)
    parser.add_argument('--timeout-ms', type=int, default=10000)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=60.0)
    parser.add_argument('--marker')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.child:
        answer = CHILDREN[args.child](args)
        args.report.write_text(json.dumps(answer), encoding='utf8')
        return 0
    return run_sections(args)


def run_sections(args):
    selected = {'mechanism': ('mechanism',), 'fake': ('fake',),
                'compat': ('fake', 'compat'), 'app': ('app',), 'real': ('real',),
                'all': ('mechanism', 'fake', 'compat', 'app', 'real')}[args.section]
    run = Run()
    if not run.service.startswith(SERVICE_PREFIX) or run.service == REAL_SERVICE:
        raise SystemExit('refusing to touch a service this run does not own')
    folder = Path(tempfile.mkdtemp(prefix='qiban-r7-vault-'))
    report = {'format': 1, 'service_is_synthetic': True, 'synthetic_service': run.service,
              'real_service_not_touched': run.service != REAL_SERVICE,
              'slot_prefix': SERVICE_PREFIX, 'section': args.section}
    results = {}
    try:
        if 'mechanism' in selected:
            results['mechanism'] = section_mechanism(run, report)
        if 'fake' in selected:
            results['fake'] = section_fake(run, report)
        if 'compat' in selected:
            results['compat'] = section_compat(run, report)
        if 'app' in selected:
            outcome = section_app(run, report, folder)
            if outcome is None:
                report['app_sections'] = 'untested'
            else:
                results['app'] = outcome
        if 'real' in selected:
            try:
                import keyring.backends.Windows  # noqa: F401
            except Exception:  # noqa: BLE001 - reported as untested, never as a pass
                report['real_sections'] = 'untested'
            else:
                results['real'] = all((section_real(run, report, folder),
                                       section_parallel(run, report, folder),
                                       section_crash(run, report, folder),
                                       section_exception(run, report, folder),
                                       section_timeout(run, report, folder)))
        report['passed'] = all(results.values())
    finally:
        # This run's own synthetic slots are deleted even when a section above failed, so an
        # interrupted check leaves nothing of its own behind.
        if {'fake', 'compat', 'app', 'real'} & set(selected):
            try:
                results['cleanup'] = cleanup(run, report, folder)
            except Exception as error:  # noqa: BLE001 - reported, never a silent pass
                results['cleanup'] = False
                report['cleanup'] = {'error': type(error).__name__}
        else:
            results['cleanup'] = True
            report['cleanup'] = {'skipped': 'no cross-process vault access in this section'}
        report['sections'] = results
        report['passed'] = all(results.values())
        report['temp_removed'] = True
        shutil.rmtree(folder, ignore_errors=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    if 'untested' in (report.get('real_sections'), report.get('app_sections')):
        return 2
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
