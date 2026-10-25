"""Synthetic memory maps only: never open a real process or database."""
import pickle
import struct

import pytest

from companion_wechat import db_windows as w


class FakeAPI:
    def __init__(self):
        self.identities = {77: w.ClientInfo(77, "C:/PRIVATE/Weixin.exe", w.SUPPORTED_VERSION)}
        self.modules = {77}
        self.windows = {77: {"window_count": 1, "minimized_count": 1,
                             "all_minimized": True, "foreground_is_target": False}}
        self.opened, self.closed, self.reads = [], [], []
        self.region_map = {0: (0, 0x10000, False), 0x10000: (0x10000, 4096, True)}

    def process_ids(self):
        return list(self.identities)

    def has_weixin_module(self, pid):
        return pid in self.modules

    def identity(self, pid, handle=None):
        return self.identities[pid]

    def window_state(self, pid):
        return self.windows.get(pid, {"window_count": 0})

    def open(self, pid, access):
        self.opened.append((pid, access))
        return 123

    def close(self, handle):
        self.closed.append(handle)

    def read(self, handle, address, size):
        self.reads.append((handle, address, size))
        return bytearray(size)

    def query_region(self, handle, address):
        return self.region_map.get(address)


class FakeMemory:
    version = w.SUPPORTED_VERSION

    def __init__(self, *, key=bytes(range(32)), size=8192, name_offset=0x100):
        self.base = 0x10000
        self.data = bytearray(size)
        self.buffers = []
        self.name_address = self.base + name_offset
        self.data[name_offset:name_offset + len(w._NAME)] = w._NAME
        node, config, blob = 0x300, 0x500, 0x700
        struct.pack_into("<QQ", self.data, node + 0x10, self.name_address, len(w._NAME))
        struct.pack_into("<Q", self.data, node + 0x28, self.base + config)
        literal = b"x'" + key.hex().encode("ascii") + b"'"
        encoded = bytearray(value ^ w._MASK[i % len(w._MASK)] for i, value in enumerate(literal))
        struct.pack_into("<QQ", self.data, config + 0x88 + 8, self.base + blob, len(encoded))
        self.data[blob:blob + len(encoded)] = encoded

    def regions(self):
        yield w.MemoryRegion(self.base, len(self.data))

    def read(self, address, size):
        start = address - self.base
        if start < 0 or start + size > len(self.data):
            return None
        value = bytearray(self.data[start:start + size])
        self.buffers.append(value)
        return value


def test_discovery_selects_one_main_client_without_exporting_private_path():
    api = FakeAPI()
    api.identities[88] = w.ClientInfo(88, "C:/PRIVATE/Weixin.exe", w.SUPPORTED_VERSION)
    assert w.discover_wechat(api=api).pid == 77
    assert "PRIVATE" not in repr(w.discover_wechat(api=api))
    assert api.reads == []


@pytest.mark.parametrize("mutation,code", [
    ("no_modules", "client_not_running"),
    ("no_windows", "client_not_running"),
    ("two_clients", "client_ambiguous"),
    ("wrong_version", "client_version_unsupported"),
])
def test_discovery_does_not_guess_ambiguous_or_unsupported_clients(mutation, code):
    api = FakeAPI()
    if mutation == "no_modules":
        api.modules.clear()
    elif mutation == "no_windows":
        api.windows.clear()
    elif mutation == "wrong_version":
        api.identities[77] = w.ClientInfo(77, "PRIVATE", "4.1.15.53")
    else:
        api.identities[88] = w.ClientInfo(88, "PRIVATE", w.SUPPORTED_VERSION)
        api.modules.add(88)
        api.windows[88] = api.windows[77]
    with pytest.raises(w.WindowsProbeError, match=code):
        w.discover_wechat(api=api)


def test_explicit_pid_selects_one_client_and_never_inspects_the_other():
    api = FakeAPI()
    api.identities[88] = w.ClientInfo(88, "OTHER-PRIVATE", "unsupported")
    api.modules.add(88)
    api.windows[88] = api.windows[77]
    calls = []
    for name in ("identity", "has_weixin_module", "window_state"):
        original = getattr(api, name)

        def only_selected(pid, *args, operation=name, delegate=original):
            assert pid == 77
            calls.append((operation, pid))
            return delegate(pid, *args)

        setattr(api, name, only_selected)
    assert w.discover_wechat(pid=77, api=api).pid == 77
    assert {name for name, _ in calls} == {"identity", "has_weixin_module", "window_state"}
    assert api.reads == [] and api.opened == []


@pytest.mark.parametrize("pid", [0, -1, True, "77", 2**32, 1.5, {}, []])
def test_invalid_discovery_pin_is_rejected_before_os_queries(pid):
    api = FakeAPI()
    api.process_ids = lambda: pytest.fail("invalid pin must not query processes")
    with pytest.raises(w.WindowsProbeError, match="invalid_configuration"):
        w.discover_wechat(pid=pid, api=api)


@pytest.mark.parametrize("mutation", ["missing", "no-module", "no-window", "version", "identity"])
def test_failed_explicit_pin_never_falls_back_to_another_running_client(mutation):
    api = FakeAPI()
    api.identities[88] = w.ClientInfo(88, "OTHER-PRIVATE", w.SUPPORTED_VERSION)
    api.modules.add(88)
    api.windows[88] = api.windows[77]
    if mutation == "missing":
        del api.identities[77]
    elif mutation == "no-module":
        api.modules.remove(77)
    elif mutation == "no-window":
        api.windows.pop(77)
    elif mutation == "version":
        api.identities[77] = w.ClientInfo(77, "PRIVATE", "unsupported")
    else:
        api.identities[77] = w.ClientInfo(88, "OTHER-PRIVATE", w.SUPPORTED_VERSION)
    with pytest.raises(w.WindowsProbeError):
        w.discover_wechat(pid=77, api=api)
    assert api.reads == []


def test_multiple_windows_in_same_pid_are_still_one_process_not_account_proof():
    api = FakeAPI()
    api.windows[77]["window_count"] = 2
    assert w.discover_wechat(api=api).pid == 77


def test_process_opens_only_read_query_rights_and_closes_once():
    api = FakeAPI()
    with w.NativeProcess(77, api=api) as process:
        assert process.version == w.SUPPORTED_VERSION
        assert process.read(0x10000, 32) == bytearray(32)
        assert "77" not in repr(process)
    process.close()
    assert api.opened == [(77, 0x0410)]
    assert api.closed == [123]
    with pytest.raises(w.WindowsProbeError, match="process_closed"):
        process.read(0x10000, 1)


def test_unsupported_version_closes_handle_before_any_memory_read():
    api = FakeAPI()
    api.identities[77] = w.ClientInfo(77, "PRIVATE", "4.1.15.53")
    with pytest.raises(w.WindowsProbeError, match="client_version_unsupported"):
        w.NativeProcess(77, api=api)
    assert api.closed == [123]
    assert api.reads == []


@pytest.mark.parametrize("version", sorted(w.SUPPORTED_VERSIONS))
def test_allowlisted_builds_keep_read_only_access_and_authenticated_keys(version):
    api = FakeAPI()
    api.identities[77] = w.ClientInfo(77, "PRIVATE", version)
    assert w.discover_wechat(pid=77, api=api).version == version
    with w.NativeProcess(77, api=api) as process:
        assert process.version == version
    assert api.opened == [(77, w.READ_ACCESS)]
    memory = FakeMemory()
    memory.version = version
    expected = bytes(range(32))
    page = b"x" * 4096
    with w.extract_keys(memory, {"contact": page}, lambda key, candidate: bytes(key) == expected and candidate == page) as keys:
        assert bytes(keys.key("contact")) == expected
    assert all(not any(buffer) for buffer in memory.buffers)


def test_new_build_does_not_accept_an_unauthenticated_key():
    memory = FakeMemory()
    memory.version = "4.1.15.56"
    with pytest.raises(w.WindowsProbeError, match="key_not_found"):
        w.extract_keys(memory, {"contact": b"x" * 4096}, lambda *_: False)


@pytest.mark.parametrize("pid", [0, -1, True, "77", 2**32])
def test_invalid_pid_never_opens_a_process(pid):
    api = FakeAPI()
    with pytest.raises(w.WindowsProbeError, match="invalid_configuration"):
        w.NativeProcess(pid, api=api)
    assert api.opened == []


@pytest.mark.parametrize("address,size", [(0, 1), (0xFFFF, 1), (0x10000, 0),
                                         (0x10000, 1048577), (2**47, 1), (True, 1)])
def test_out_of_bounds_memory_read_never_reaches_os(address, size):
    api = FakeAPI()
    with w.NativeProcess(77, api=api) as process:
        with pytest.raises(w.WindowsProbeError, match="invalid_memory_address"):
            process.read(address, size)
    assert api.reads == []


def test_cumulative_budget_and_deadline_bound_all_passes():
    api, now = FakeAPI(), [0.0]
    with w.NativeProcess(77, api=api, max_total_bytes=4096, timeout=2, clock=lambda: now[0]) as process:
        process.read(0x10000, 4096)
        with pytest.raises(w.WindowsProbeError, match="read_budget_exceeded"):
            process.read(0x10000, 1)
        now[0] = 2
        with pytest.raises(w.WindowsProbeError, match="scan_timeout"):
            list(process.regions())
    assert len(api.reads) == 1


def test_region_iteration_skips_unreadable_and_stops_at_os_end():
    api = FakeAPI()
    with w.NativeProcess(77, api=api) as process:
        assert list(process.regions()) == [w.MemoryRegion(0x10000, 4096)]
    assert api.reads == []


def test_malformed_region_cannot_loop_forever():
    api = FakeAPI()
    api.region_map[0] = (0, 0, False)
    with w.NativeProcess(77, api=api) as process:
        with pytest.raises(w.WindowsProbeError, match="process_query_failed"):
            list(process.regions())


def test_window_observation_does_not_require_process_memory_or_activation():
    api = FakeAPI()
    assert w.window_state(77, api=api) == {"window_count": 1, "minimized_count": 1,
                                         "all_minimized": True, "foreground_is_target": False}
    assert api.opened == api.reads == []


def test_key_matches_only_explicitly_supplied_encrypted_pages_and_buffers_are_cleared():
    key = bytes(range(32))
    process, calls = FakeMemory(key=key), []
    page = b"A" * 4096

    def verify(candidate, candidate_page):
        calls.append(candidate_page)
        return candidate == key and candidate_page == page

    with w.extract_keys(process, {"contact": page}, verify) as keys:
        view = keys.key("contact")
        assert view == key and view.readonly
        assert keys.aliases() == ("contact",)
        assert key.hex() not in repr(keys)
        with pytest.raises(TypeError, match="memory_keys_not_serializable"):
            pickle.dumps(keys)
        with pytest.raises(w.WindowsProbeError, match="unknown_key_alias"):
            keys.key("elsewhere")
    assert view == bytes(32)
    assert calls == [page]
    assert all(not any(buffer) for buffer in process.buffers)
    with pytest.raises(w.WindowsProbeError, match="key_closed"):
        keys.aliases()


def test_cross_chunk_match_and_overlap_are_found_exactly_once():
    process = FakeMemory(size=2 * 1024 * 1024, name_offset=1024 * 1024 - 7)
    hits = w._scan(process, [w._NAME])
    assert hits == [(0, process.name_address)]
    assert all(not any(buffer) for buffer in process.buffers)


def test_partial_account_key_matching_does_not_assume_every_candidate_is_logged_in():
    process = FakeMemory()
    keys = w.extract_keys(process, {"candidate_a": b"A" * 4096, "candidate_b": b"B" * 4096},
                          lambda key, page: page[0] == ord("B"), require_all=False)
    with keys:
        assert keys.aliases() == ("candidate_b",)


def test_no_matching_key_can_return_empty_candidates_or_fail_closed():
    for require_all in (False, True):
        process = FakeMemory()
        if require_all:
            with pytest.raises(w.WindowsProbeError, match="key_not_found"):
                w.extract_keys(process, {"contact": b"A" * 4096}, lambda *_: False)
        else:
            with w.extract_keys(process, {"contact": b"A" * 4096}, lambda *_: False,
                                require_all=False) as keys:
                assert keys.aliases() == ()
        assert all(not any(buffer) for buffer in process.buffers)


def test_explicit_salt_literal_uses_only_key_for_encrypted_page_authentication():
    key = bytes(range(32))
    process = FakeMemory(key=key + b"S" * 16)
    with w.extract_keys(process, {"contact": b"A" * 4096}, lambda value, _: value == key) as keys:
        assert keys.key("contact") == key


def test_require_all_failure_clears_keys_that_already_matched(monkeypatch):
    process, owned = FakeMemory(), []
    original = w.MemoryKeys.close

    def inspect_and_close(keys):
        owned.extend(keys._keys.values())
        original(keys)

    monkeypatch.setattr(w.MemoryKeys, "close", inspect_and_close)
    with pytest.raises(w.WindowsProbeError, match="key_not_found"):
        w.extract_keys(process, {"candidate_a": b"A" * 4096, "candidate_b": b"B" * 4096},
                       lambda _, page: page[0] == ord("A"))
    assert len(owned) == 1 and owned[0] == bytearray(32)


def test_empty_memory_has_no_keys_and_partial_mode_returns_empty():
    process = FakeMemory()
    process.data[:] = bytes(len(process.data))
    with w.extract_keys(process, {"contact": b"A" * 4096}, lambda *_: True, require_all=False) as keys:
        assert keys.aliases() == ()


@pytest.mark.parametrize("pages", [{}, {"PRIVATE NAME": b"A" * 4096}, {"contact": b"x"},
                                   {"contact": b"SQLite format 3\0" + bytes(4080)},
                                   {"contact": "A" * 4096}])
def test_invalid_or_plaintext_database_pages_fail_before_scanning(pages):
    process = FakeMemory()
    with pytest.raises(w.WindowsProbeError):
        w.extract_keys(process, pages, lambda *_: True)
    assert process.buffers == []


@pytest.mark.parametrize("fault", ["bad_config_ptr", "oversize_blob", "bad_blob_ptr"])
def test_corrupt_object_pointers_and_lengths_fail_without_arbitrary_reads(fault):
    process = FakeMemory()
    if fault == "bad_config_ptr":
        struct.pack_into("<Q", process.data, 0x300 + 0x28, 2**64 - 1)
    elif fault == "oversize_blob":
        struct.pack_into("<Q", process.data, 0x500 + 0x88 + 0x10, 10**9)
    else:
        struct.pack_into("<Q", process.data, 0x500 + 0x88 + 8, 1)
    with pytest.raises(w.WindowsProbeError, match="key_not_found"):
        w.extract_keys(process, {"contact": b"A" * 4096}, lambda *_: True)
    assert all(not any(buffer) for buffer in process.buffers)


def test_callback_failure_is_redacted_and_mutable_scan_buffers_are_cleared():
    process = FakeMemory()

    def fail(*_):
        raise ValueError("SENSITIVE KEY AND PRIVATE PATH")

    with pytest.raises(w.WindowsProbeError, match="^key_scan_failed$") as error:
        w.extract_keys(process, {"contact": b"A" * 4096}, fail)
    assert "SENSITIVE" not in str(error.value)
    assert all(not any(buffer) for buffer in process.buffers)


def test_match_limit_stops_before_key_object_reads():
    process = FakeMemory()
    for i in range(65):
        offset = 0x1000 + i * len(w._NAME)
        process.data[offset:offset + len(w._NAME)] = w._NAME
    with pytest.raises(w.WindowsProbeError, match="scan_limit_exceeded"):
        w.extract_keys(process, {"contact": b"A" * 4096}, lambda *_: True)
    assert len(process.buffers) == 1
    assert all(not any(buffer) for buffer in process.buffers)


def test_unknown_errors_cannot_be_used_as_diagnostic_text():
    assert str(w.WindowsProbeError("secret-value")) == "process_query_failed"
