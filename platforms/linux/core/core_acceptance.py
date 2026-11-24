"""Real Ubuntu worker, PostgreSQL, core ledger, independent fixture receiver and SSH status."""
import hashlib
import json
import time
from datetime import datetime, timezone

from acceptance import os_evidence, require
from lab import run

EXPECTED_REPLY = hashlib.sha256("已收到合成会话里的登记请求。".encode()).hexdigest()


def observe(lab, message, predicate):
    deadline = time.monotonic() + 45
    while True:
        row = lab.control("inspect", message)
        if predicate(row):
            return row
        if time.monotonic() > deadline:
            raise RuntimeError(f"Timed out waiting for {message}: {row['inputs']}")
        time.sleep(0.1)


def terminal(lab, request, expected):
    def finished(row):
        return any(v["id"] == request["input"] and v["outcome"] not in {"queued", "processing"} for v in row["inputs"])
    row = observe(lab, request["message"], finished)
    result = next(v for v in row["inputs"] if v["id"] == request["input"])
    require(result["outcome"] == expected, f"Expected {expected}, received {result}")
    return row


def accepted_once(row, status):
    require(len(row["receiver"]) == 1, "Unexpected independent receiver submission count")
    receipt, = row["receiver"]
    require(receipt["reply_sha256"] == EXPECTED_REPLY and receipt["target"] == "synthetic-peer",
            "Wrong recipient or payload")
    require(row["action"] and row["action"]["status"] == status, "Core ledger outcome does not match")
    require(row["event_version"] and row["processed_version"] == row["event_version"], "Event receipt mismatch")
    if status == "succeeded":
        require(row["action"]["receipt"]["id"] == receipt["receipt"], "Independent receiver and core receipts differ")
    else:
        require(not row["action"]["receipt"], "Unconfirmed core action has a success receipt")


def restart(lab):
    run("docker", "restart", lab.name)
    return lab.wait_ready()


def accept(lab):
    before = lab.status()
    initial = before["synthetic_dialogue"]
    require(not any(p["available"] for p in before["platforms"].values()), "Unexpected personal platform capability")
    require(before["platforms"]["local"]["reason"] == "会客室尚未接入。", "Unexpected local room capability description")
    lab.control("grant")

    success = lab.control("send", "confirm")
    confirmed = terminal(lab, success, "succeeded")
    accepted_once(confirmed, "succeeded")
    duplicate = terminal(lab, lab.control("replay", success["message"]), "duplicate")
    accepted_once(duplicate, "succeeded")
    require(duplicate["model_calls"] == confirmed["model_calls"] == 1 and duplicate["action"] == confirmed["action"],
            "Old event triggered a new decision or action")

    held = lab.control("send", "hold")
    observe(lab, held["message"], lambda row: row["in_model"])
    lab.control("revoke")
    require(lab.control("release", held["message"])["released"], "Model did not reach revocation checkpoint")
    revoked = terminal(lab, held, "denied")
    require(revoked["model_calls"] == 1 and not revoked["receiver"] and not revoked["action"],
            "Revocation during decision did not block delivery")
    inactive = terminal(lab, lab.control("send", "confirm"), "denied")
    require(inactive["model_calls"] == 0 and inactive["event_version"] is None and not inactive["receiver"],
            "Unregistered incoming message entered the core")
    lab.control("grant")
    stale = terminal(lab, lab.control("replay", held["message"]), "denied")
    require(stale["model_calls"] == 1 and not stale["receiver"], "Re-grant revived an old permission")

    lost_request = lab.control("send", "lost")
    lost = terminal(lab, lost_request, "uncertain")
    accepted_once(lost, "uncertain")

    interrupted = lab.control("send", "interrupt")
    waiting = observe(lab, interrupted["message"],
                      lambda row: len(row["receiver"]) == 1 and row["action"]["status"] == "dispatching")
    os_before = os_evidence(lab)
    restarted = restart(lab)
    require(before["runtime_instance"] != restarted["runtime_instance"] and
            before["installation_id"] == restarted["installation_id"], "Runtime restart did not preserve installation")
    recovered = terminal(lab, interrupted, "uncertain")
    accepted_once(recovered, "uncertain")
    require(waiting["receiver"] == recovered["receiver"] and waiting["action"]["id"] == recovered["action"]["id"]
            and recovered["model_calls"] == waiting["model_calls"] == 1,
            "Interrupted submission was repeated")
    replay_lost = terminal(lab, lab.control("replay", lost_request["message"]), "duplicate")
    accepted_once(replay_lost, "uncertain")
    require(replay_lost["model_calls"] == lost["model_calls"] and replay_lost["receiver"] == lost["receiver"],
            "Unconfirmed delivery was retried")
    lab.control("revoke")
    final = lab.status()
    latest = final["synthetic_dialogue"]
    expected = {"confirmed": 1, "uncertain": 2, "submissions": 3, "denied": 3, "replays": 2}
    require({k: latest[k] - initial[k] for k in expected} == expected, "SSH counters disagree with fixture cases")
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "lab": lab.name,
              "database": lab.control("database"), "os_before_restart": os_before, "os_after_restart": os_evidence(lab),
              "previous_instance": before["runtime_instance"], "new_instance": final["runtime_instance"],
              "installation_id": final["installation_id"], "counter_changes": expected, "status": latest,
              "confirmed": confirmed, "replay": duplicate, "inflight_revocation": revoked, "unauthorized": inactive,
              "new_grant_old_message": stale, "lost_receipt": lost, "lost_after_restart": replay_lost,
              "submitted_before_restart": waiting, "recovered_without_resubmission": recovered}
    (lab.folder / "core-acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
