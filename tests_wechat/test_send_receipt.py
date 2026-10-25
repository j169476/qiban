"""Synthetic-only receipt proofs; no native clients, database keys, UI or files."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace

import pytest

from companion_wechat import send_receipt as sr

STAMP = 1700000000
TEXT = "synthetic exact reply"
SCOPE = sr.ReceiptScope("synthetic-account", "synthetic-owner", "synthetic-self", "source-epoch-1")


class SyntheticReader:
    """An in-memory reader whose heads derive from rows, never its stale ack."""

    def __init__(self):
        self.scope = SCOPE
        self.sources = {"shard-0": [], "shard-1": []}
        self.generations = {key: key + "-generation" for key in self.sources}
        self.calls = []
        self.complete = True
        self.mutate_report = lambda report: report
        self.ack = {key: None for key in self.sources}

    def add(self, local_id, *, source="shard-0", server_id=None, created=STAMP,
            text=TEXT, sender=None):
        row = {"local_id": local_id, "server_id": server_id or str(1000 + local_id),
               "create_time": created, "text": text, "sender_ref": sender or self.scope.self_ref}
        self.sources[source].append(row)
        return row

    @staticmethod
    def anchor(row):
        return {key: row[key] for key in ("local_id", "server_id", "create_time")}

    def heads(self):
        return [{"source_id": key, "generation": self.generations[key],
                 "anchor": self.anchor(max(rows, key=lambda row: row["local_id"])) if rows else None}
                for key, rows in self.sources.items()]

    def current_heads(self, scope):
        self.calls.append(("heads", scope))
        return self.mutate_report({"schema_version": 1, "scope": self.scope.private_dict(),
                                   "heads": self.heads()})

    def read_self_echo(self, scope, heads, *, limit, scan_limit):
        self.calls.append(("echo", scope, heads, limit, scan_limit))
        old = {head.source_id: head for head in heads}
        anchors, rows, preexisting = {}, [], set()
        for key, head in old.items():
            prior = head.anchor.local_id if head.anchor else 0
            originals = self.sources.get(key, [])
            original = next((row for row in originals if row["local_id"] == prior), None)
            anchors[key] = self.anchor(original) if original is not None else None
            preexisting.update(row["server_id"] for row in originals if row["local_id"] <= prior)
        scanned = 0
        complete = self.complete
        for key, originals in self.sources.items():
            prior = old[key].anchor.local_id if key in old and old[key].anchor else 0
            for row in sorted(originals, key=lambda row: row["local_id"]):
                if row["local_id"] <= prior:
                    continue
                if scanned >= scan_limit or len(rows) >= limit:
                    complete = False
                    break
                scanned += 1
                if row["sender_ref"] != self.scope.self_ref:
                    continue
                rows.append(dict(row, source_id=key, generation=self.generations[key],
                                 account_id=self.scope.account_id, target_ref=self.scope.target_ref,
                                 server_id_is_new=row["server_id"] not in preexisting))
        report = {"schema_version": 1, "scope": self.scope.private_dict(), "heads": self.heads(),
                  "anchors": anchors, "rows": rows, "complete": complete, "scanned": scanned}
        return self.mutate_report(report)


def begin(reader, **kwargs):
    return sr.begin_watermark(reader, SCOPE, clock=lambda: 10.0, wall_clock=lambda: STAMP, **kwargs)


def verify(reader, watermark, **kwargs):
    return sr.verify_receipt(reader, watermark, TEXT, clock=lambda: 11.0, **kwargs)


def test_unique_matching_self_echo_confirms_only_client_recorded():
    reader = SyntheticReader()
    reader.add(1, created=STAMP - 100)
    watermark = begin(reader)
    reader.add(2)
    result = verify(reader, watermark)
    assert result.confirmed
    assert result.status == "client_recorded"
    assert result.reason == "unique_self_echo"
    assert result.record.local_id == 2
    assert result.record.server_id == "1002"
    assert not hasattr(result, "delivered")
    assert not hasattr(result.record, "text")


def test_begin_uses_live_max_even_when_receive_ack_is_empty():
    reader = SyntheticReader()
    reader.add(9, text="old private body", created=STAMP - 1)
    reader.add(3, created=STAMP - 1)
    reader.add(2, source="shard-1", server_id="444", created=STAMP - 1)
    watermark = begin(reader)
    assert reader.ack == {"shard-0": None, "shard-1": None}
    assert [head.anchor.local_id for head in watermark.heads] == [9, 2]
    assert [call[0] for call in reader.calls] == ["heads"]
    assert all(set(h.private_dict()) == {"source_id", "generation", "anchor"} for h in watermark.heads)
    assert all(set(h.anchor.private_dict()) == {"local_id", "server_id", "create_time"}
               for h in watermark.heads)
    assert verify(reader, watermark).status == "pending"


def test_old_identical_text_and_other_sender_cannot_confirm():
    reader = SyntheticReader()
    reader.add(1)
    watermark = begin(reader)
    reader.add(2, sender=SCOPE.target_ref)
    result = verify(reader, watermark)
    assert not result.confirmed
    assert result.reason == "no_matching_echo"


def test_server_ids_are_not_assumed_monotone():
    reader = SyntheticReader()
    reader.add(1, server_id="800000", created=STAMP - 1)
    watermark = begin(reader)
    reader.add(2, server_id="31")
    assert verify(reader, watermark).confirmed


@pytest.mark.parametrize("source", ["shard-0", "shard-1"])
def test_preexisting_server_id_in_nonhead_history_is_rejected_across_all_shards(source):
    reader = SyntheticReader()
    reader.add(1, source=source, server_id="31", created=STAMP - 2)
    reader.add(2, source=source, server_id="32", created=STAMP - 1)
    watermark = begin(reader)
    reader.add(3, server_id="31")
    result = verify(reader, watermark)
    assert not result.confirmed
    assert result.reason == "receipt_server_id_reused"


@pytest.mark.parametrize("created", [STAMP - 1, STAMP - 1000, STAMP + 1000])
def test_history_import_or_future_timestamp_with_new_ids_cannot_confirm(created):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1, created=created)
    assert verify(reader, watermark).reason == "no_matching_echo"


@pytest.mark.parametrize("text", [TEXT.upper(), " " + TEXT, TEXT + " ", "draft " + TEXT,
                                   TEXT + "\n", "synthetic exact replу", ""])
def test_body_match_is_exact_without_trimming_casefold_or_substring(text):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1, text=text)
    assert not verify(reader, watermark).confirmed


@pytest.mark.parametrize("second_source", ["shard-0", "shard-1"])
def test_two_identical_new_self_records_are_ambiguous(second_source):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.add(2, source=second_source)
    result = verify(reader, watermark)
    assert not result.confirmed
    assert result.status == "ambiguous"
    assert result.record is None


def test_one_match_does_not_confirm_when_scan_or_row_budget_is_incomplete():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.add(2)
    result = verify(reader, watermark, limit=1, scan_limit=1)
    assert not result.confirmed
    assert result.reason == "incomplete_scan"
    assert verify(reader, watermark).status == "ambiguous"


def test_pending_metadata_or_missing_text_must_make_reader_incomplete():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.complete = False
    assert verify(reader, watermark).reason == "incomplete_scan"
    reader.complete = True
    assert verify(reader, watermark).confirmed
    assert len([call for call in reader.calls if call[0] == "heads"]) == 1
    assert watermark.heads[0].anchor is None  # Polling does not rebaseline.


@pytest.mark.parametrize("field,value", [("account_id", "another-account"),
                                         ("target_ref", "another-target"),
                                         ("self_ref", "another-self"),
                                         ("source_generation", "another-epoch")])
def test_changed_scope_never_confirms(field, value):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, scope=dict(r["scope"], **{field: value}))
    assert verify(reader, watermark).reason == "receipt_scope_changed"


@pytest.mark.parametrize("field,value", [("account_id", "another-account"),
                                         ("target_ref", "another-target"),
                                         ("sender_ref", SCOPE.target_ref),
                                         ("generation", "new-salt")])
def test_each_echo_must_match_fixed_scope_and_shard_generation(field, value):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], **{field: value})])
    assert not verify(reader, watermark).confirmed


@pytest.mark.parametrize("change", ["missing", "added", "generation"])
def test_source_catalog_and_generations_cannot_change_after_begin(change):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    if change == "missing":
        del reader.sources["shard-1"]
    elif change == "added":
        reader.sources["shard-2"] = []
        reader.generations["shard-2"] = "new-generation"
    else:
        reader.generations["shard-1"] = "new-generation"
    result = verify(reader, watermark)
    assert not result.confirmed
    assert result.reason == ("receipt_generation_changed" if change == "generation"
                             else "receipt_catalog_changed")


@pytest.mark.parametrize("change", ["missing", "server", "timestamp", "regressed", "unreported"])
def test_original_max_anchor_must_still_exist_unchanged(change):
    reader = SyntheticReader()
    original = reader.add(10, created=STAMP - 1)
    watermark = begin(reader)
    reader.add(11)
    if change == "missing":
        reader.sources["shard-0"].remove(original)
    elif change == "server":
        original["server_id"] = "555"
    elif change == "timestamp":
        original["create_time"] -= 1
    elif change == "regressed":
        reader.mutate_report = lambda r: dict(r, heads=[dict(h, anchor=None) for h in r["heads"]])
    else:
        reader.mutate_report = lambda r: dict(r, anchors={})
    assert verify(reader, watermark).reason == "receipt_anchor_changed"


def test_current_head_metadata_cannot_disagree_with_echo_at_max_id():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], server_id="9999")])
    assert verify(reader, watermark).reason == "receipt_anchor_changed"


@pytest.mark.parametrize("field,value", [("server_id", None), ("server_id", ""), ("server_id", "0"),
                                         ("server_id", "01"), ("server_id", True), ("server_id", 1001),
                                         ("server_id", str(2**63)), ("create_time", 0),
                                         ("create_time", float("nan")), ("local_id", True),
                                         ("local_id", 0), ("local_id", 999)])
def test_invalid_or_pending_echo_metadata_cannot_confirm(field, value):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], **{field: value})])
    assert not verify(reader, watermark).confirmed


def test_reader_cannot_claim_preexisting_head_server_id_is_new():
    reader = SyntheticReader()
    reader.add(1)
    watermark = begin(reader)
    reader.add(2, server_id="1001")
    reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], server_id_is_new=True)])
    assert verify(reader, watermark).reason == "receipt_server_id_reused"


@pytest.mark.parametrize("change", ["duplicate-local", "duplicate-server", "old-row"])
def test_duplicated_or_old_records_never_confirm(change):
    reader = SyntheticReader()
    old = reader.add(1, created=STAMP - 1)
    watermark = begin(reader)
    reader.add(2)
    if change == "duplicate-local":
        reader.mutate_report = lambda r: dict(r, rows=r["rows"] * 2, scanned=2)
    elif change == "duplicate-server":
        reader.add(3, source="shard-1", server_id="1002")
    else:
        reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], **reader.anchor(old))])
    assert not verify(reader, watermark).confirmed


@pytest.mark.parametrize("field,value", [("complete", 1), ("complete", "true"),
                                         ("scanned", 0), ("scanned", True), ("scanned", 513),
                                         ("scanned", float("nan")), ("schema_version", True)])
def test_report_limits_and_types_are_strict(field, value):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, **{field: value})
    assert verify(reader, watermark).reason == "receipt_invalid_report"


@pytest.mark.parametrize("location", ["root", "scope", "head", "anchor", "row"])
def test_unknown_fields_are_rejected_deeply(location):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)

    def inject(report):
        node = {"root": report, "scope": report["scope"], "head": report["heads"][0],
                "anchor": report["heads"][0]["anchor"], "row": report["rows"][0]}[location]
        node["private_extra"] = "must-not-be-returned"
        return report

    reader.mutate_report = inject
    assert verify(reader, watermark).reason == "receipt_invalid_report"


@pytest.mark.parametrize("bad_text", ["\0secret", "x" * 4097, "界" * 1366, "\ud800", b"text"])
def test_echo_body_and_candidate_are_byte_bounded_utf8(bad_text):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, rows=[dict(r["rows"][0], text=bad_text)])
    assert not verify(reader, watermark).confirmed
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_input"):
        sr.verify_receipt(reader, watermark, bad_text)


def test_watermark_and_results_do_not_expose_private_identifiers_in_repr():
    reader = SyntheticReader()
    reader.add(1)
    watermark = begin(reader)
    reader.add(2)
    result = verify(reader, watermark)
    for obj in (SCOPE, watermark, watermark.heads[0], watermark.heads[0].anchor, result, result.record):
        assert TEXT not in repr(obj)
        assert SCOPE.self_ref not in repr(obj)
        assert SCOPE.target_ref not in repr(obj)
        assert "1002" not in repr(obj)
    with pytest.raises(FrozenInstanceError):
        watermark.heads = ()
    assert not hasattr(watermark, "private_dict")


def test_reader_exceptions_are_sanitized_without_chained_provider_error():
    reader = SyntheticReader()

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE message and credential")

    reader.current_heads = fail
    with pytest.raises(sr.ReceiptError, match="receipt_reader_failed") as exc:
        begin(reader)
    assert "PRIVATE" not in str(exc.value)
    assert exc.value.__suppress_context__
    reader.current_heads = SyntheticReader.current_heads.__get__(reader)
    watermark = begin(reader)
    reader.read_self_echo = fail
    result = verify(reader, watermark)
    assert not result.confirmed
    assert result.reason == "receipt_reader_failed"
    assert "PRIVATE" not in repr(result)


def test_recreated_reader_cannot_reuse_old_watermark_even_if_scope_matches():
    reader = SyntheticReader()
    watermark = begin(reader)
    replacement = SyntheticReader()
    replacement.add(1)
    assert verify(replacement, watermark).reason == "receipt_reader_changed"
    assert replacement.calls == []


def test_serialized_or_receive_checkpoint_is_not_a_watermark_input():
    reader = SyntheticReader()
    reader.add(1)
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_input"):
        sr.verify_receipt(reader, {"schema_version": 1, "sources": reader.ack}, TEXT)
    invalid = replace(begin(reader), _origin=object())
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_input"):
        verify(reader, invalid)


@pytest.mark.parametrize("now", [9, 131, float("nan"), float("inf"), True])
def test_expired_or_invalid_clock_never_confirms_or_reads_echo(now):
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    result = sr.verify_receipt(reader, watermark, TEXT, clock=lambda: now)
    assert not result.confirmed
    assert not any(call[0] == "echo" for call in reader.calls)


def test_timeout_during_read_cannot_confirm_late_result():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    moments = iter([11, 131])
    result = sr.verify_receipt(reader, watermark, TEXT, clock=lambda: next(moments))
    assert result.reason == "receipt_watermark_expired"


def test_metadata_capture_timeout_prevents_watermark_creation():
    reader = SyntheticReader()
    moments = iter([10, 31])
    with pytest.raises(sr.ReceiptError, match="receipt_watermark_expired"):
        sr.begin_watermark(reader, SCOPE, clock=lambda: next(moments), wall_clock=lambda: STAMP)


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True},
                                     {"scan_limit": 99}, {"scan_limit": 513}, {"max_age": 301},
                                     {"max_age": float("nan")}, {"max_age": True}])
def test_invalid_budgets_never_call_reader(kwargs):
    reader = SyntheticReader()
    watermark = begin(reader)
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_input"):
        verify(reader, watermark, **kwargs)
    assert not any(call[0] == "echo" for call in reader.calls)


@pytest.mark.parametrize("bad", [None, {}, {"schema_version": 1, "epoch": "e", "sources": {}},
                                {"schema_version": 1, "scope": SCOPE.private_dict(), "heads": []}])
def test_begin_rejects_nonlive_protocol_or_missing_catalog(bad):
    reader = SyntheticReader()
    reader.current_heads = lambda scope: copy.deepcopy(bad)
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_report"):
        begin(reader)


@pytest.mark.parametrize("change", ["pending-head", "duplicate-source", "too-many", "private-body"])
def test_metadata_begin_fails_before_dispatch_on_invalid_or_unbounded_heads(change):
    reader = SyntheticReader()
    reader.add(1)

    def change_heads(report):
        if change == "pending-head":
            report["heads"][0]["anchor"]["server_id"] = "0"
        elif change == "duplicate-source":
            report["heads"] *= 2
        elif change == "too-many":
            report["heads"] = [dict(report["heads"][0], source_id=str(i)) for i in range(17)]
        else:
            report["heads"][0]["anchor"]["text"] = "body must never be included in metadata"
        return report

    reader.mutate_report = change_heads
    with pytest.raises(sr.ReceiptError, match="receipt_invalid_report"):
        begin(reader)
    assert not any(call[0] == "echo" for call in reader.calls)


def test_extra_report_rows_cannot_exceed_requested_bound():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.mutate_report = lambda r: dict(r, rows=r["rows"] * 2, scanned=2)
    assert verify(reader, watermark, limit=1).reason == "receipt_invalid_report"


def test_watermark_is_detached_from_mutable_reader_report():
    reader = SyntheticReader()
    reader.add(1)
    report = reader.current_heads(SCOPE)
    reader.current_heads = lambda scope: report
    watermark = begin(reader)
    report["heads"][0]["anchor"]["local_id"] = 999
    report["scope"]["target_ref"] = "another-target"
    assert watermark.heads[0].anchor.local_id == 1
    assert watermark.scope.target_ref == SCOPE.target_ref
    reader.add(2)
    assert verify(reader, watermark).confirmed


def test_empty_source_after_begin_can_receive_first_message():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1, source="shard-1")
    assert verify(reader, watermark).confirmed


def test_incoming_head_after_self_echo_does_not_require_body_read_for_head():
    reader = SyntheticReader()
    watermark = begin(reader)
    reader.add(1)
    reader.add(2, sender=SCOPE.target_ref, text="incoming private content")
    result = verify(reader, watermark)
    assert result.confirmed
    assert result.record.local_id == 1


def test_caller_budgets_are_passed_to_the_reader():
    reader = SyntheticReader()
    watermark = begin(reader)
    verify(reader, watermark, limit=7, scan_limit=32)
    assert reader.calls[-1][-2:] == (7, 32)
