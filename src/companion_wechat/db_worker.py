"""Private JSON-lines database worker. Stdout must never be shown as user logs.

Launch only as an isolated child process with stdin/stdout pipes and discarded
stderr. It never writes files, sends messages, or imports a writable database
connection. Parent owns startup/request deadlines and acknowledged checkpoints.
"""
from __future__ import annotations

import json
import sys

from .db_receipt import _CODES as _RECEIPT_CODES
from .db_receipt import ReceiptHandles
from .db_source import _CODES, SourceError, SourceSession
from .send_receipt import _CODES as _PROOF_CODES

_ERRORS = _CODES | _RECEIPT_CODES | _PROOF_CODES | {
    "worker_protocol_invalid", "worker_not_open", "worker_already_open",
    "configuration_invalid", "database_path_invalid", "database_format_unsupported",
    "contact_schema_unsupported", "target_missing_or_ambiguous", "target_kind_mismatch",
    "message_schema_unsupported", "sender_schema_unsupported", "database_count_unsupported",
    "unsupported_platform", "invalid_configuration", "client_not_running", "client_ambiguous",
    "client_version_unsupported", "client_identity_changed", "process_access_denied",
    "process_closed", "read_budget_exceeded", "scan_timeout", "scan_limit_exceeded",
    "process_query_failed", "invalid_memory_address", "key_not_found", "key_scan_failed",
    "invalid_database_page", "window_query_failed", "dependency_missing", "dependency_version_unsupported",
    "path_invalid", "source_unavailable", "sidecars_unavailable", "key_invalid", "dependency_unavailable",
    "database_unreadable", "database_busy", "query_denied", "query_timeout", "query_failed", "connection_closed",
}
_RETRY = {"source_catalog_changed", "source_pending_at_enrollment", "database_busy", "query_timeout",
          "sidecars_unavailable", "client_not_running", "process_access_denied", "process_closed",
          "process_query_failed", "scan_timeout", "key_not_found", "source_unavailable", "database_unreadable"}
_MAX_LINE = 262144


def logged_accounts():
    from .db_probe import ProbeError
    from .native.protocol import ProbeFailure
    from .native.windows import Windows

    try:
        accounts = sorted(row[0] for row in Windows().logged_in_sessions())
    except ProbeFailure as exc:
        code = "client_version_unsupported" if str(exc).startswith("unsupported_") else "client_identity_changed"
        raise ProbeError(code) from None
    if len(set(accounts)) != len(accounts):
        raise SourceError("source_identity_changed")
    return {"accounts": accounts}


def serve(stdin, stdout, *, session_factory=SourceSession, receipt_factory=ReceiptHandles):
    session = None
    receipts = None
    audience = None
    try:
        while True:
            raw = stdin.readline(_MAX_LINE + 1)
            if not raw:
                return 0
            request_id = 0
            try:
                if len(raw) > _MAX_LINE or not raw.endswith("\n"):
                    raise SourceError("source_configuration_invalid")
                value = json.loads(raw)
                if (not isinstance(value, dict) or type(value.get("id")) is not int
                        or not 0 <= value["id"] <= 2**31 - 1):
                    raise SourceError("source_configuration_invalid")
                request_id = value["id"]
                command = value.get("command")
                if audience and session and command not in ("close","open") and session.member_kind(audience["username"]) != audience["kind"]:
                    raise SourceError("source_identity_changed")
                if command == "logged_accounts" and set(value) == {"id", "command"} and session is None:
                    result = logged_accounts()
                elif command == "setup_directory" and set(value) == {"id","command","login","kind","after"} and session is None:
                    from .db_setup import browse
                    result = browse(value["login"], value["kind"], value["after"])
                elif command in ("setup_verify","setup_save_check") and set(value) == {"id","command","login","targets"} and session is None:
                    from .db_setup import verify
                    result = verify(value["login"], value["targets"], include_directory=command=="setup_save_check")
                elif command == "open":
                    required = {"id", "command", "config", "enrollment", "checkpoint"}
                    if not required <= set(value) or set(value) - required - {"recover_missing_history","audience"}:
                        raise SourceError("source_configuration_invalid")
                    if session is not None:
                        raise SourceError("source_configuration_invalid")
                    options = {"recover_missing_history": value["recover_missing_history"]} if "recover_missing_history" in value else {}
                    audience = value.get("audience")
                    if audience is not None:
                        if (not isinstance(audience,dict) or set(audience)!={"account_id","kind","username","since"}
                                or audience["kind"] not in ("private","group") or type(audience["since"]) is not int
                                or audience["since"]<=0):
                            raise SourceError("source_configuration_invalid")
                        # A first sighting starts at consent, whereas reconnect uses the committed cursor.
                        if value["enrollment"] is None:
                            options["authorized_since"] = audience["since"]
                    session = session_factory(value["config"], value["enrollment"], value["checkpoint"], **options)
                    session.__enter__()
                    result = session.snapshot()
                    if audience is not None:
                        enrollment=result["enrollment"]
                        if (enrollment["account_id"]!=audience["account_id"] or enrollment["since"]!=audience["since"]
                                or list(enrollment["targets"].values()) != [{"kind":audience["kind"],"username":audience["username"]}]):
                            raise SourceError("source_identity_changed")
                elif command == "enrolled_members" and set(value) == {"id","command"} and session is not None:
                    result = session.enrolled_members()
                elif command == "member_page" and set(value) == {"id","command","kind","cursor"} and session is not None and audience is None:
                    result = session.member_page(value["kind"], value["cursor"])
                elif command == "chat_member_page" and set(value) == {"id","command","kind","cursor"} and session is not None and audience is None:
                    result = session.member_page(value["kind"], value["cursor"], find_chats=True)
                elif command == "member_window" and set(value) == {"id","command","username","kind","since","checkpoint","window","limit"} and session is not None and audience is None:
                    result = session.member_window(**{k:v for k,v in value.items() if k not in ("id","command")})
                elif command == "member_kind" and set(value) == {"id", "command", "username"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = {"kind":session.member_kind(value["username"])}
                elif command == "directory_page" and set(value) == {"id", "command", "kind", "after", "limit"}:
                    if session is None or audience is not None:
                        raise SourceError("source_configuration_invalid")
                    result = session.directory_page(value["kind"], value["after"], value["limit"])
                elif command == "resolve_alias" and set(value) == {"id", "command", "hint"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = session.resolve_alias(value["hint"])
                elif command == "snapshot" and set(value) == {"id", "command"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = session.snapshot()
                elif command == "read" and set(value) == {"id", "command", "checkpoint", "limit"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = session.read(value["checkpoint"], limit=value["limit"])
                elif command == "freeze_window" and set(value) == {"id", "command", "checkpoint"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = session.freeze_window(value["checkpoint"])
                elif command == "read_window" and set(value) == {"id", "command", "checkpoint", "window", "limit"}:
                    if session is None:
                        raise SourceError("source_closed")
                    result = session.read_window(value["checkpoint"], value["window"], limit=value["limit"])
                elif command == "receipt_begin" and (set(value) == {"id", "command", "action_id", "target_ref"}
                        or audience is None and set(value) == {"id","command","action_id","target_ref","kind","since"}):
                    if session is None:
                        raise SourceError("source_closed")
                    if receipts is None:
                        receipts = receipt_factory(session)
                    options = {k:value[k] for k in ("kind","since") if k in value}
                    result = receipts.begin(value["action_id"], value["target_ref"], **options)
                elif command == "receipt_check" and set(value) == {"id", "command", "action_id", "handle", "text"}:
                    if session is None or receipts is None:
                        raise SourceError("source_closed")
                    result = receipts.check(value["action_id"], value["handle"], value["text"])
                elif command == "receipt_discard" and set(value) == {"id", "command", "action_id", "handle"}:
                    if session is None or receipts is None:
                        raise SourceError("source_closed")
                    result = receipts.discard(value["action_id"], value["handle"])
                elif command == "close" and set(value) == {"id", "command"}:
                    if receipts is not None:
                        receipts.close()
                        receipts = None
                    if session is not None:
                        session.close()
                        session = None
                    result = {"closed": True}
                else:
                    raise SourceError("source_configuration_invalid")
                if audience and session and command != "close" and session.member_kind(audience["username"]) != audience["kind"]:
                    raise SourceError("source_identity_changed")
                output = {"id": request_id, "ok": True, "result": result}
                rendered = json.dumps(output, ensure_ascii=False, allow_nan=False)
                if len(rendered.encode("utf-8")) > 2 * 1024 * 1024:
                    raise SourceError("source_read_failed")
                stdout.write(rendered + "\n")
                stdout.flush()
            except Exception as exc:
                if receipts is not None:
                    try:
                        receipts.close()
                    except Exception:
                        pass
                    receipts = None
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                    session = None
                code = getattr(exc, "code", None)
                if type(code) is not str or code not in _ERRORS:
                    code = "source_read_failed"
                stdout.write(json.dumps({"id": request_id, "ok": False, "error": code,
                                         "recoverable": code in _RETRY}) + "\n")
                stdout.flush()
                # Stop after invalid/failing input; no trailing attacker data
                # is interpreted as a second command following an oversized line.
                return 2
    finally:
        if receipts is not None:
            try:
                receipts.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def main():
    try:
        if sys.stdin.isatty() or sys.stdout.isatty():
            return 2
        return serve(sys.stdin, sys.stdout)
    except (Exception, KeyboardInterrupt):
        # No traceback or native error details on the private channel or stderr.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
