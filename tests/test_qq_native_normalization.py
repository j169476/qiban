from copy import deepcopy

import pytest

from companion_core.errors import AssistantError
from companion_qq.native_normalization import normalize_native

OPTIONS = {"account": "10001", "private_targets": ["10002"], "groups": ["10003"]}
IDENTITY = {"uin": "10001", "uid": "u_self", "friends": {"10002": "u_peer"}, "groups": ["10003"]}


def raw(n=1, *, group=False):
    return {"chatType": 2 if group else 1, "peerUid": "10003" if group else "u_peer",
            "senderUin": "10002", "senderUid": "u_peer", "msgTime": "1792922400",
            "msgId": str(9000 + n), "msgSeq": str(n), "clientSeq": str(5000 + n), "msgRandom": str(100 + n),
            "elements": [{"elementType": 1, "textElement": {"content": "same text"}}]}


def normalize(value, **kwargs):
    return normalize_native(value, OPTIONS, identity=kwargs.pop("identity", IDENTITY),
                            sent_reference=kwargs.pop("sent_reference", lambda *args: False), **kwargs)


def test_private_cache_rebuild_keeps_event_identity_and_equal_text_messages_stay_distinct():
    first, second = normalize(raw(1)), normalize(raw(2))
    rebound = raw(1)
    rebound.update(msgId="7000000000000000000", msgSeq="8888")
    assert normalize(rebound).as_dict() == first.as_dict()
    assert first.id != second.id
    assert first.text == second.text


def test_group_cache_rebuild_keeps_identity_and_other_sequences_stay_distinct():
    original = raw(group=True)
    rebound = {**original, "msgId": "7000000000000000000"}
    assert normalize(original).as_dict() == normalize(rebound).as_dict()
    assert normalize(original).id != normalize(raw(2, group=True)).id


@pytest.mark.parametrize("change", [{"peerUid": "u_other"}, {"senderUid": "u_wrong"}, {"senderUid": "u_self"}])
def test_private_peer_and_author_must_match_verified_catalog(change):
    with pytest.raises(AssistantError):
        normalize({**raw(), **change})


def test_account_switch_and_unverified_group_are_rejected():
    with pytest.raises(AssistantError) as mismatch:
        normalize(raw(), identity={**IDENTITY, "uin": "99999"})
    assert mismatch.value.code == "qq_account_mismatch"
    with pytest.raises(AssistantError) as unverified:
        normalize(raw(group=True), identity={**IDENTITY, "groups": []})
    assert unverified.value.code == "qq_target_unverified"


def test_group_mention_requires_both_identifiers_and_nested_preview_is_not_a_trigger():
    message = raw(group=True)
    message["elements"][0]["textElement"].update(atType=2, atUid="10001", atNtUid="u_other")
    assert normalize(message).metadata["qq"]["trigger"] == "none"
    message["elements"][0]["textElement"]["atNtUid"] = "u_self"
    assert normalize(message).metadata["qq"]["trigger"] == "mention"
    message["elements"] = [raw()["elements"][0], {"elementType": 7, "replyElement": {
        "senderUid": "10002", "senderUidStr": "u_peer", "replayMsgSeq": "80",
        "sourceMsgTextElems": deepcopy(message["elements"])}}]
    assert normalize(message).metadata["qq"]["trigger"] == "none"


def test_group_quote_requires_confirmed_self_sequence_and_wins_over_later_mention():
    message = raw(group=True)
    message["elements"].insert(0, {"elementType": 7, "replyElement": {
        "senderUid": "10001", "senderUidStr": "u_self", "replayMsgSeq": "80"}})
    message["elements"][-1]["textElement"].update(atType=2, atUid="10001", atNtUid="u_self")
    assert normalize(message).metadata["qq"]["trigger"] == "mention"
    observed = []

    def confirmed(*args):
        observed.append(args)
        return True

    assert normalize(message, sent_reference=confirmed).metadata["qq"]["trigger"] == "reply"
    assert observed == [("group", "10003", "native-seq:80")]


def test_self_echo_and_observed_group_system_tip_do_not_create_events():
    assert normalize({**raw(), "senderUin": "10001", "senderUid": "u_self"}) is None
    assert normalize({**raw(group=True), "msgType": 5, "senderUin": "0", "senderUid": "",
                      "elements": [{"elementType": 8}]}) is None


@pytest.mark.parametrize("field", ["clientSeq", "msgRandom", "msgTime"])
def test_private_source_fields_are_required(field):
    value = raw()
    del value[field]
    with pytest.raises(AssistantError):
        normalize(value)


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("number,field,kind", [(2, "picElement", "image"),
                                             (3, "fileElement", "file"), (4, "pttElement", "audio")])
def test_media_only_events_have_stable_scoped_references_without_native_paths(group, number, field, kind):
    value = raw(group=group)
    value["elements"] = [{"elementType": number, "elementId": "123", field: {
        "fileName": "test", "filePath": "C:/private/example", "fileSize": "0"}}]
    event = normalize(value)
    assert event is not None and "内容尚未读取" in event.text
    assert event.metadata["qq"]["trigger"] == ("none" if group else "direct")
    assert len(event.attachments) == 1 and event.attachments[0]["kind"] == kind
    assert set(event.attachments[0]) == {"id", "kind", "position"}
    rebound = deepcopy(value)
    rebound["msgId"] = "8888"
    rebound["elements"][0].update(elementId="999", **{field: {"filePath": "D:/different", "fileSize": "1234"}})
    assert normalize(rebound).as_dict() == event.as_dict()
    assert normalize({**value, "msgTime": "1792922401"}).attachments[0]["id"] != event.attachments[0]["id"]


def test_mixed_text_and_media_preserve_text_and_require_real_group_trigger():
    value = raw(group=True)
    value["elements"] += [{"elementType": 2, "picElement": {}}, {"elementType": 2, "picElement": {}},
                          {"elementType": 4, "pttElement": {}}, {"elementType": 3, "fileElement": {}}]
    event = normalize(value)
    assert event.text == "same text" and len(event.attachments) == 4
    assert len({item["id"] for item in event.attachments}) == 4
    assert event.metadata["qq"]["trigger"] == "none"
    value["elements"][0]["textElement"].update(atType=2, atUid="10001", atNtUid="u_self")
    assert normalize(value).metadata["qq"]["trigger"] == "mention"


def test_invalid_media_and_quoted_media_cannot_create_untrusted_attachment_sources():
    with pytest.raises(AssistantError) as error:
        normalize({**raw(), "elements": [{"elementType": 2, "picElement": "invalid"}]})
    assert error.value.code == "qq_media_invalid"
    quoted = {"elementType": 7, "replyElement": {"senderUid": "10002", "senderUidStr": "u_peer",
              "sourceMsgTextElems": [{"elementType": 2, "picElement": {}}]}}
    event = normalize({**raw(group=True), "elements": [*raw()["elements"], quoted]})
    assert event.attachments == ()
    assert normalize({**raw(), "senderUin": "10001", "senderUid": "u_self",
                      "elements": [{"elementType": 2, "picElement": {}}]}) is None


def test_attachment_limit_is_explicit_and_shared_archive_schema_accepts_only_typed_refs():
    import json
    from pathlib import Path

    from jsonschema import ValidationError, validate

    event = normalize({**raw(), "elements": [{"elementType": 2, "picElement": {}}] * 20})
    assert len(event.attachments) == 12
    assert event.metadata["qq"]["attachments_omitted"] == 8
    assert "8 个附件" in event.text
    schemas = json.loads((Path(__file__).parents[1] / "src/companion_core/schemas.json").read_text(encoding="utf8"))
    schema = schemas["conversation_turns"]["properties"]["attachments"]
    validate(list(event.attachments), schema)
    validate([{"kind": "image", "url": "input-image:" + "a" * 32, "title": "旧图片", "bvid": ""}], schema)
    for changes in ({"id": "C:/private/file"}, {"position": 200}, {"kind": "video"}, {"filePath": "C:/private/file"}):
        with pytest.raises(ValidationError):
            validate([{**event.attachments[0], **changes}], schema)
