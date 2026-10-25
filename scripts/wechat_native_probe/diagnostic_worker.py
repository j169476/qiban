"""Private, bounded historical receipt diagnosis; never issues a send/commit.

Input travels only through a hidden child's stdin. Reconstructed historical
heads are NOT a live watermark and are never accepted by the send gateway.
Output contains only fixed error codes, counts, booleans, time deltas and hashes.
"""
import hashlib
import json
import re
import sys
from datetime import datetime

from companion_wechat import send_receipt as proof
from companion_wechat.db_receipt import DatabaseReceiptReader
from companion_wechat.db_source import SourceSession


def diagnose(payload):
    with SourceSession(payload["config"], payload["enrollment"], payload["checkpoint"]) as session:
        target = payload["enrollment"]["targets"]["owner_main"]["username"]
        reader = DatabaseReceiptReader(session, target)
        items = session.snapshot()["checkpoint"]["sources"]
        heads = tuple(proof.ReceiptHead(key, item["salt"],
                      proof.ReceiptAnchor(item["anchor"]["local_id"], str(item["anchor"]["server_id"]),
                                          item["anchor"]["create_time"]) if item["anchor"] else None)
                      for key, item in items.items() if item["target_ref"] == "owner_main")
        stage = "read_self_echo"
        try:
            report = reader.read_self_echo(reader.scope, heads, limit=100, scan_limit=512)
            stage = "match_metadata"
            started = datetime.fromisoformat(payload["probe"]["started_at"]).timestamp()
            finished = datetime.fromisoformat(payload["probe"]["at"]).timestamp()
            matches = [row for row in report["rows"]
                       if hashlib.sha256(row["text"].encode()).hexdigest() == payload["probe"]["probe_text_sha256"]]
            return {"historical_diagnostic_only": True, "reader_ok": True, "complete": report["complete"],
                    "scanned": report["scanned"], "exact_digest_matches": len(matches),
                    "matches": [{"sender_is_self": row["sender_ref"] == reader.scope.self_ref,
                                 "server_id_is_new": row["server_id_is_new"],
                                 "seconds_from_probe_start": round(row["create_time"] - started, 3),
                                 "seconds_from_probe_end": round(row["create_time"] - finished, 3)} for row in matches]}
        except Exception as exc:
            code = getattr(exc, "code", "")
            return {"historical_diagnostic_only": True, "reader_ok": False, "stage": stage,
                    "code": code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "reader_error"}


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.readline(262145)
        if len(raw) > 262144 or not raw.endswith(b"\n"):
            raise ValueError()
        result = diagnose(json.loads(raw))
    except Exception:
        result = {"historical_diagnostic_only": True, "reader_ok": False, "code": "diagnosis_failed"}
    print(json.dumps(result))
