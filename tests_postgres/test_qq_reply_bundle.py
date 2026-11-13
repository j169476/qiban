import pytest

from companion_core.contracts import ActorRef, Event
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope
from companion_qq.native_sender import NativeSendReceipt
from companion_qq.reply_bundle import ReplyBundle, reply_parts
from tests_postgres.test_desktop_management import desktop_store as desktop_store


@pytest.fixture
def bundle(desktop_store):
    store = desktop_store
    scope = ReplyScope("qq", "10001", "group", "10003")
    permission = ReplyAuthorizations(store)
    permission.update(scope, enabled=True, expected_version=None, actor="test")
    arguments = {"text": "同一份回复", "sticker_id": "sticker_one"}
    event = Event(
        "qq",
        "10001",
        "synthetic",
        ActorRef("qq", "10002"),
        "group:10003",
        "2026-10-31T00:00:00Z",
        "测试",
        visibility="group",
        metadata={"qq": {"target_id": "10003"}},
    )
    with store.db() as db:
        db.execute(
            "INSERT INTO core_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "action",
                "dedupe",
                "generation",
                "qq",
                "10001",
                "message.reply",
                store.ledger_encode("core_actions.body", {"event": event.as_dict(), "arguments": arguments}),
                "dispatching",
                None,
                event.occurred_at,
            ),
        )
    return store, scope, arguments


def run(bundle, **overrides):
    store, scope, arguments = bundle
    return ReplyBundle(store).deliver(
        "action",
        scope,
        arguments,
        guard=lambda: None,
        check_part=overrides.pop("check_part", lambda part: None),
        prepare=overrides.pop("prepare", lambda index, part: index),
        submit=overrides.pop(
            "submit", lambda ticket: NativeSendReceipt(str(9001 + ticket), "native-seq:" + str(701 + ticket))
        ),
        discard=overrides.pop("discard", lambda ticket: None),
        **overrides,
    )


@pytest.mark.parametrize(
    "args,kinds",
    [
        ({"text": "你好"}, ["text"]),
        ({"text": "你好", "format": "voice", "sticker_id": "one"}, ["voice", "sticker"]),
        ({"sticker_id": "one"}, ["sticker"]),
    ],
)
def test_reply_order_and_single_sticker(args, kinds):
    assert [p["kind"] for p in reply_parts(args)] == kinds


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"text": ""},
        {"sticker_id": "../private.png"},
        {"sticker_id": ["one", "two"]},
        {"sticker_id": "one", "format": "voice"},
        {"text": "你好", "path": "private.png"},
    ],
)
def test_no_empty_reply_multiple_stickers_or_model_paths(args):
    with pytest.raises(AssistantError):
        reply_parts(args)


def test_success_is_encrypted_and_reconstruction_does_not_resend(bundle):
    store, _, _ = bundle
    submitted = []

    def submit(ticket):
        state = store.state("qq-reply-bundle:action")
        assert state["parts"][ticket]["status"] == "attempted"
        submitted.append(ticket)
        return NativeSendReceipt(str(9001 + ticket), "native-seq:" + str(701 + ticket))

    first = run(bundle, submit=submit)
    assert first.status == "succeeded" and first.receipt["confirmed_count"] == 2
    assert run(bundle, submit=submit) == first and submitted == [0, 1]
    with store.db() as db:
        row = db.execute("SELECT value FROM state WHERE key=?", ("qq-reply-bundle:action",)).fetchone()
    assert "confirmed" not in str(row["value"]) and "9001" not in str(row["value"])


@pytest.mark.parametrize("lost", [0, 1])
def test_receipt_loss_stops_remaining_parts_and_never_resumes(bundle, lost):
    submitted = []

    def submit(ticket):
        submitted.append(ticket)
        if ticket == lost:
            raise TimeoutError("synthetic lost receipt")
        return NativeSendReceipt("9001", "native-seq:701")

    result = run(bundle, submit=submit)
    assert result.status == "uncertain" and result.receipt["confirmed_count"] == lost
    assert result.receipt["parts"][lost]["status"] == "uncertain"
    assert run(bundle, submit=submit) == result and submitted == list(range(lost + 1))


def test_disabled_sticker_after_prepare_keeps_confirmed_text_and_discards_ticket(bundle):
    submitted, discarded = [], []
    disabled = False

    def prepare(index, part):
        nonlocal disabled
        disabled = index == 1
        return index

    def check(part):
        if part["kind"] == "sticker" and disabled:
            raise AssistantError("sticker_disabled", "disabled")

    def submit(ticket):
        submitted.append(ticket)
        return NativeSendReceipt("9001", "native-seq:701")

    result = run(bundle, check_part=check, prepare=prepare, submit=submit, discard=discarded.append)
    assert result.status == "failed" and result.error == "sticker_disabled"
    assert result.receipt["confirmed_count"] == 1 and submitted == [0] and discarded == [1]
    assert result.receipt["parts"][1]["status"] == "not_submitted"
    assert run(bundle, submit=submit) == result and submitted == [0]


def test_revocation_between_parts_preserves_prefix_and_stops(bundle):
    store, scope, _ = bundle
    submitted = []

    def submit(ticket):
        submitted.append(ticket)
        permission = ReplyAuthorizations(store)
        permission.update(scope, enabled=False, expected_version=permission.version(scope), actor="test")
        return NativeSendReceipt("9001", "native-seq:701")

    result = run(bundle, submit=submit)
    assert result.status == "failed" and result.receipt["confirmed_count"] == 1 and submitted == [0]
    assert result.receipt["parts"][1]["status"] == "not_submitted"


def test_duplicate_caller_during_send_does_not_create_another_submit(bundle):
    submitted = []

    def submit(ticket):
        duplicate = run(bundle)
        assert duplicate.status == "uncertain"
        assert duplicate.receipt["parts"][ticket]["status"] == "uncertain"
        submitted.append(ticket)
        return NativeSendReceipt(str(9001 + ticket), "native-seq:" + str(701 + ticket))

    assert run(bundle, submit=submit).status == "succeeded" and submitted == [0, 1]


def test_crash_between_parts_never_restarts_from_first_or_second_part(bundle):
    store, _, _ = bundle
    assert run(bundle).status == "succeeded"
    state = store.state("qq-reply-bundle:action")
    state["status"] = "running"
    state["parts"][1] = {"index": 1, "kind": "sticker", "status": "not_submitted"}
    store.set_state("qq-reply-bundle:action", state)
    calls = []
    result = run(bundle, prepare=lambda *args: calls.append(args))
    assert result.status == "uncertain" and result.receipt["confirmed_count"] == 1 and calls == []


def test_pre_submit_journal_failure_does_not_send(bundle, monkeypatch):
    store, _, _ = bundle
    original = store.set_state
    sent, discarded = [], []

    def save(key, value, *args, **kwargs):
        if key == "qq-reply-bundle:action" and value["parts"][0]["status"] == "attempted":
            raise OSError("injected write failure")
        return original(key, value, *args, **kwargs)

    monkeypatch.setattr(store, "set_state", save)
    result = run(bundle, submit=sent.append, discard=discarded.append)
    assert result.status == "failed" and sent == [] and discarded == [0]


def test_confirmed_receipt_write_failure_stays_uncertain_and_does_not_send_sticker(bundle, monkeypatch):
    store, _, _ = bundle
    original = store.set_state
    sent = []

    def save(key, value, *args, **kwargs):
        if key == "qq-reply-bundle:action" and value["parts"][0]["status"] == "confirmed":
            raise OSError("injected write failure")
        return original(key, value, *args, **kwargs)

    def submit(ticket):
        sent.append(ticket)
        return NativeSendReceipt("9001", "native-seq:701")

    monkeypatch.setattr(store, "set_state", save)
    result = run(bundle, submit=submit)
    assert result.status == "uncertain" and sent == [0]
    assert run(bundle, submit=submit) == result and sent == [0]


def test_modified_arguments_cannot_reuse_receipts(bundle):
    store, scope, arguments = bundle
    run(bundle)
    with pytest.raises(AssistantError) as error:
        run((store, scope, {**arguments, "sticker_id": "different"}))
    assert error.value.code == "qq_action_mismatch"


def test_missing_parent_action_cannot_send(bundle):
    store, _, _ = bundle
    with store.db() as db:
        db.execute("DELETE FROM core_actions WHERE id=?", ("action",))
    with pytest.raises(AssistantError) as error:
        run(bundle)
    assert error.value.code == "qq_action_missing"
