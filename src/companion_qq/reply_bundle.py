"""D28 ordered reply receipts. No model calls, asset lookup, or automatic resume."""

import hashlib
import json
import re
from dataclasses import asdict

from companion_core.contracts import ActionResult, Event
from companion_core.reply_authorization import ReplyAuthorizations, require

from .native_sender import NativeSendReceipt


def reply_parts(arguments):
    """A reply is a body, one registered sticker, or a body followed by a sticker."""
    require(
        isinstance(arguments, dict) and not set(arguments) - {"text", "format", "sticker_id", "file_ids"},
        "qq_reply_invalid",
    )
    text, mode, sticker = arguments.get("text"), arguments.get("format", "text"), arguments.get("sticker_id")
    require(mode in {"text", "voice"}, "qq_reply_invalid")
    parts = []
    if text is not None:
        require(isinstance(text, str) and text.strip() and len(text) <= 1000, "qq_reply_invalid")
        parts.append({"kind": mode, "text": text})
    else:
        require("format" not in arguments, "qq_reply_invalid")
    if "file_ids" in arguments:
        identifiers = arguments["file_ids"]
        require(isinstance(identifiers, list) and 1 <= len(identifiers) <= 12
                and all(isinstance(value, str) and re.fullmatch(r"(?:library|attachment):[a-zA-Z0-9_-]{1,128}", value)
                        for value in identifiers) and len(set(identifiers)) == len(identifiers), "qq_reply_invalid")
        parts.extend({"kind": "file", "file_id": value} for value in identifiers)
    if sticker is not None:
        require(
            isinstance(sticker, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", sticker), "qq_reply_invalid"
        )
        parts.append({"kind": "sticker", "sticker_id": sticker})
    require(parts, "qq_reply_invalid")
    return parts


class ReplyBundle:
    """Journal each irreversible submit; a previously claimed bundle never resumes.

    prepare must not submit. It returns a private ticket after resolving the
    allowed material. check_part rechecks material eligibility before submit.
    submit returns a verified NativeSendReceipt; discard releases an unsent
    ticket. Transport callbacks are internal and never selected by the model.
    """

    def __init__(self, store):
        self.store = store

    def deliver(self, action_id, scope, arguments, *, guard, check_part, prepare, submit, discard):
        parts = reply_parts(arguments)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"scope": asdict(scope), "arguments": arguments}, sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        key = "qq-reply-bundle:" + action_id
        with self.store.lock:
            previous = self.store.state(key)
            if previous is not None:
                require(previous["fingerprint"] == fingerprint, "qq_action_mismatch")
                return self._outcome(previous)
            guard()
            with self.store.db() as db:
                row = db.execute("SELECT * FROM core_actions WHERE id=?", (action_id,)).fetchone()
                require(
                    row is not None
                    and row["status"] == "dispatching"
                    and row["capability"] == "message.reply"
                    and row["platform"] == "qq"
                    and row["account"] == scope.account_identity
                    and scope.platform == "qq",
                    "qq_action_missing",
                )
                body = self.store.ledger_decode("core_actions.body", row["body"])
                event = Event.from_dict(body["event"])
                require(
                    body["arguments"] == arguments
                    and event.account == scope.account_identity
                    and event.platform == "qq"
                    and event.visibility == scope.kind
                    and event.metadata["qq"]["target_id"] == scope.target_id,
                    "qq_action_mismatch",
                )
                version = ReplyAuthorizations(self.store).version(scope, db=db)
                require(version is not None, "reply_not_authorized")
            record = {
                "fingerprint": fingerprint,
                "status": "running",
                "error": "",
                "parts": [
                    {"index": i, "kind": part["kind"], "status": "not_submitted"}
                    for i, part in enumerate(parts)
                ],
            }
            self.store.set_state(key, record)

        def check(part):
            guard()
            ReplyAuthorizations(self.store).check(scope, version)
            check_part(part)

        for index, part in enumerate(parts):
            ticket = None
            attempted = False
            try:
                check(part)
                ticket = prepare(index, part)
                # A queued image may have been disabled while its bytes were prepared.
                check(part)
                record["parts"][index]["status"] = "attempted"
                self.store.set_state(key, record)  # Commit before any possible submission.
                with self.store.lock:
                    check(part)
                    attempted = True
                    receipt = submit(ticket)
                require(
                    isinstance(receipt, NativeSendReceipt)
                    and re.fullmatch(r"[1-9][0-9]{0,19}", receipt.message_id)
                    and (
                        (scope.kind == "private" and receipt.reference is None)
                        or (
                            scope.kind == "group"
                            and isinstance(receipt.reference, str)
                            and re.fullmatch(r"native-seq:[1-9][0-9]{0,19}", receipt.reference)
                        )
                    ),
                    "qq_send_unconfirmed",
                )
                record["parts"][index].update(status="confirmed", receipt=asdict(receipt))
                self.store.set_state(key, record)
            except BaseException as error:
                if not attempted and ticket is not None:
                    discard(ticket)
                record["parts"][index]["status"] = "uncertain" if attempted else "not_submitted"
                record["status"] = "uncertain" if attempted else "failed"
                record["error"] = getattr(error, "code", "qq_reply_interrupted")
                # Never silently discard an already confirmed prefix from the receipt.
                self.store.set_state(key, record)
                if not isinstance(error, Exception):
                    raise
                return self._outcome(record)
        record["status"] = "succeeded"
        self.store.set_state(key, record)
        return self._outcome(record)

    @staticmethod
    def _outcome(record):
        # Running means another caller or an interrupted process owns this bundle.
        status = record["status"] if record["status"] != "running" else "uncertain"
        parts = json.loads(json.dumps(record["parts"]))
        for part in parts:
            if part["status"] == "attempted":
                part["status"] = "uncertain"
        return ActionResult(
            status,
            {"parts": parts, "confirmed_count": sum(p["status"] == "confirmed" for p in parts)},
            record["error"] or ("qq_send_unconfirmed" if status == "uncertain" else ""),
        )
