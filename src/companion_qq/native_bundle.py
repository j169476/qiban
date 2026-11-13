"""Formal D28 delivery through the owned native worker, with per-part receipts."""

import base64
import hashlib
import re

from companion_core.reply_authorization import require
from companion_core.sticker_library import StickerLibrary

from .native_sender import NativeSendReceipt
from .reply_bundle import ReplyBundle


def deliver(sender, action_id, scope, arguments, material, *, sticker_revision, guard, file_selection=None):
    library = StickerLibrary(sender.store)
    from companion_core.media_originals import MediaOriginals

    from .file_replies import FileReplySettings, ReplyFiles
    files = ReplyFiles(sender.store)


    def check(part):
        require(
            scope.platform == "qq" and scope.account_identity == sender.identity["uin"], "qq_account_mismatch"
        )
        identity = sender.worker.request("identity")
        require(all(identity.get(k) == v for k, v in sender.identity.items()), "qq_account_mismatch")
        if file_selection is not None:
            require(FileReplySettings(sender.store).read() == file_selection['limits'], 'qq_file_settings_changed')
        if part["kind"] == "file":
            require(file_selection is not None and part['file_id'] in file_selection['items'], 'qq_file_not_offered')
            files.check(scope, file_selection['authorization'], file_selection['items'][part['file_id']])
        if part["kind"] == "sticker":
            item = library.get(part["sticker_id"])
            require(item["enabled"], "sticker_disabled")
            require(item["revision"] == sticker_revision, "sticker_changed")

    def prepare(index, part):
        args = {"kind": scope.kind, "target": scope.target_id}
        if part["kind"] == "file":
            data = files.read(scope, file_selection, part['file_id'], guard=guard)
            command = "prepare_file"
            args.update(file=base64.b64encode(data).decode('ascii'), name=file_selection['items'][part['file_id']]['name'])
        elif part["kind"] == "sticker":
            data = library.read_for_send(part["sticker_id"], sticker_revision, guard=guard)
            item = library.get(part["sticker_id"])
            require(item["revision"] == sticker_revision, "sticker_changed")
            command = "prepare_image"
            args.update(
                image=base64.b64encode(data).decode("ascii"),
                width=item["width"],
                height=item["height"],
                format=item["format"],
            )
        elif part["kind"] == "voice":
            require(material and material["text"] == part["text"], "qq_action_mismatch")
            if material["format"] == "voice":
                audio = material["audio"]
                require(
                    isinstance(audio, bytes)
                    and audio.startswith(b"\x02#!SILK_V3")
                    and len(audio) <= 1024 * 1024
                    and hashlib.sha256(audio).hexdigest() == material["sha256"]
                    and type(material["audio_ms"]) is int
                    and 0 < material["audio_ms"] <= 600000,
                    "qq_voice_invalid",
                )
                command = "prepare_voice"
                args.update(audio=base64.b64encode(audio).decode("ascii"), audio_ms=material["audio_ms"])
            else:
                require(material["format"] == "text" and material.get("fallback"), "qq_action_mismatch")
                command = "prepare_text"
                args["text"] = part["text"]
        else:
            command = "prepare_text"
            args["text"] = part["text"]
        guard()
        ticket = sender.worker.request(command, **args)
        if not (
            isinstance(ticket, dict)
            and set(ticket) == {"token", "message_id"}
            and isinstance(ticket["token"], str)
            and re.fullmatch(r"[a-f0-9]{64}", ticket["token"])
            and isinstance(ticket["message_id"], str)
            and re.fullmatch(r"[1-9][0-9]{0,19}", ticket["message_id"])
        ):
            sender.worker.close()
            require(False, "qq_native_protocol_invalid")
        return ticket

    def submit(ticket):
        try:
            result = sender.worker.request("submit_text", token=ticket["token"])
            require(
                isinstance(result, dict)
                and set(result) == {"message_id", "reference"}
                and result["message_id"] == ticket["message_id"],
                "qq_send_unconfirmed",
            )
            receipt = NativeSendReceipt(**result)
            require(
                (scope.kind == "private" and receipt.reference is None)
                or (
                    scope.kind == "group"
                    and isinstance(receipt.reference, str)
                    and re.fullmatch(r"native-seq:[1-9][0-9]{0,19}", receipt.reference)
                ),
                "qq_send_unconfirmed",
            )
            return receipt
        except BaseException as error:
            detail = {"error": getattr(error, "code", type(error).__name__)}
            try:
                diagnostic = sender.worker.request("send_diagnostic")
                if (
                    isinstance(diagnostic, dict)
                    and set(diagnostic) == {"phase", "native_result", "event_status"}
                    and diagnostic["phase"]
                    in {
                        "waiting",
                        "invoking",
                        "receipt_timeout",
                        "native_return_timeout",
                        "receipt_mismatch",
                        "native_rejected",
                        "confirmed",
                    }
                    and all(
                        value is None or type(value) is int
                        for value in (diagnostic["native_result"], diagnostic["event_status"])
                    )
                ):
                    detail["transport"] = diagnostic
            except Exception:
                detail["diagnostic_unavailable"] = True
            try:
                sender.worker.close()
            except Exception as cleanup_error:
                detail["cleanup_error"] = getattr(cleanup_error, "code", type(cleanup_error).__name__)
            sender.store.set_state("qq-native-failure:" + action_id, detail)
            raise

    def discard(ticket):
        try:
            result = sender.worker.request("cancel_text", token=ticket["token"])
            require(result == {"cancelled": True}, "qq_native_protocol_invalid")
        except BaseException:
            sender.worker.close()

    originals = MediaOriginals(sender.store)
    held = file_selection is not None and sender.store.state('qq-reply-bundle:' + action_id) is None
    if held:
        files.validate(scope, file_selection)
        originals.hold(file_selection['owner'], action_id, [key.split(':', 1)[1]
            for key in file_selection['items'] if key.startswith('attachment:')])
    try:
        return ReplyBundle(sender.store).deliver(
            action_id, scope, arguments, guard=guard, check_part=check,
            prepare=prepare, submit=submit, discard=discard)
    finally:
        if held:
            originals.release(file_selection['owner'], action_id)
