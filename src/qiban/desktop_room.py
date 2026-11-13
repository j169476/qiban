"""Private local chat authenticated by the current unlocked desktop session."""
import asyncio
import base64
import hashlib
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from companion_core.contracts import Event, stable_key
from companion_core.errors import AssistantError
from companion_core.input_images import MAX_BYTES
from companion_service.local_adapter import LocalAdapter


class RoomSessions:
    def __init__(self, manager):
        self.manager = manager
        self.authority = manager.authority
        self.repository = manager.repository

    def request(self, token, kind, *, conversation="local-owner"):
        manager = self.manager
        if kind != "role" or not manager._active(token):
            raise AssistantError("desktop_locked", "请先解锁会客室")
        actor = manager.room_actor()
        return self.authority.issue(actor=actor, conversation=conversation, audience="private",
                                    recipients=(actor,), purpose="reply", session_id=token)

    def actor(self, request):
        self.authority.require(request)
        return self.manager.room_actor()

class DesktopRoom(LocalAdapter):
    poll_interval_seconds = 1
    allow_image_only = True

    def __init__(self, manager):
        super().__init__(manager.store, RoomSessions(manager))
        self._cleaned_run = None
        self._processing = {}

    def owner_actor(self):
        return self.sessions.manager.room_actor()

    def session_active(self, identifier):
        return self.sessions.manager._active(identifier)

    def post_files(self, token, text, nonce, files):
        request = self.request(token)
        if (not isinstance(files, list) or not 1 <= len(files) <= 3 or not isinstance(text, str)
                or len(text) > 12000 or not isinstance(nonce, str) or not re.fullmatch("[a-f0-9]{32}", nonce)):
            raise AssistantError("invalid_message", "每条消息最多附三张图片，文字最多 12000 字")
        for item in files:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or not isinstance(item["path"], str) or not isinstance(item["sha256"], str)
                    or not re.fullmatch("[a-f0-9]{64}", item["sha256"])):
                raise AssistantError("invalid_image", "请重新选择图片")
        hashes = [item["sha256"] for item in files]
        if len(set(hashes)) != len(hashes):
            raise AssistantError("invalid_image", "同一张图片只需添加一次")
        event_id = stable_key("event", self.platform, self.account, nonce)
        key = "desktop-room-input:" + event_id
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.request(token)
            prior = self.store.state(key, {}, db)
            if prior:
                if prior.get("file_hashes") != hashes:
                    raise AssistantError("message_conflict", "本次提交的图片已改变")
                # Replay checks the original message and captions, even if the source file has moved.
                return self.post(token, text, nonce, prior["image_ids"])
            images = []
            for item in files:
                path = Path(item["path"])
                if not path.is_absolute() or path.drive.startswith("\\\\") or path.is_reserved():
                    raise AssistantError("invalid_image", "请选择本地图片")
                try:
                    if not path.is_file():
                        raise OSError()
                    with path.open("rb") as stream:
                        blob = stream.read(MAX_BYTES + 1)
                except (OSError, ValueError):
                    raise AssistantError("invalid_image", "无法读取图片，请重新选择") from None
                if len(blob) > MAX_BYTES:
                    raise AssistantError("image_limit", "每张图片不能超过 8 MiB")
                if hashlib.sha256(blob).hexdigest() != item["sha256"]:
                    raise AssistantError("image_changed", "所选图片已经改变，请重新选择")
                images.append(self.images.upload(request, blob)["id"])
            result = self.post(token, text, nonce, images)
            admission = self.store.state(key, {}, db)
            self.store.set_state(key, {**admission, "file_hashes": hashes, "image_ids": images}, db)
            return result

    def preview(self, token, turn, image):
        from PIL import Image

        request = self.request(token)
        if not isinstance(turn, str) or len(turn) > 100 or not isinstance(image, str) or not re.fullmatch("[a-f0-9]{32}", image):
            raise AssistantError("invalid_image", "图片标识无效")
        lease = self.images.access.lease(request, ["conversation_turns/" + turn])
        data = self.store.read("conversation_turns", turn)["data"]
        if (data["thread_key"] != request.conversation or data["account_uid"] != self.account or
                not any(a.get("url") == "input-image:" + image for a in data.get("attachments", []))):
            raise AssistantError("access_denied", "该图片不属于本条消息")
        with Image.open(io.BytesIO(self.images.read(request, image))) as source:
            source.thumbnail((640, 640))
            while True:
                output = io.BytesIO()
                source.save(output, "JPEG", quality=78)
                if output.tell() <= 96000:
                    break
                source.thumbnail((source.width // 2 or 1, source.height // 2 or 1))
        self.images.access.revalidate(lease)
        return {"data": base64.b64encode(output.getvalue()).decode("ascii")}

    @staticmethod
    def _scope(run):
        # Only an explicit model-account handover carries accepted inputs forward.
        return run.get("room_intake", run["generation"])

    def _retire_old_inputs(self, run, db):
        scope = self._scope(run)
        if self._cleaned_run == scope:
            return
        rows = db.execute("SELECT e.id,e.version FROM core_events e LEFT JOIN core_event_receipts r ON r.event_id=e.id "
                          "WHERE e.platform='local' AND e.account=? AND e.actor=? AND e.conversation=? "
                          "AND e.deleted=0 AND r.event_id IS NULL",
                          (self.account,self.owner_actor().key,self.conversation())).fetchall()
        for row in rows:
            admission = self.store.state("desktop-room-input:"+row["id"],{},db)
            if admission.get("scope") != scope:
                self.store.set_state("core-processed:"+row["id"],row["version"],db)
                self.store.set_state("web-message:"+row["id"],{"status":"cancelled","reason":"run_ended"},db)

    def post(self, token, text, nonce, images=None):
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.request(token)
            run = self.core.guard()
            self._retire_old_inputs(run,db)
            result = super().post(token,text,nonce,images)
            if result.get("new"):
                self.store.set_state("desktop-room-input:"+result["id"],
                                     {"scope":self._scope(run),"version":result["version"]},db)
        self._cleaned_run = self._scope(run)
        return result

    def accepted_for_run(self, event, run):
        if event.platform != self.platform or event.account != self.account:
            return False
        admission = self.store.state("desktop-room-input:"+event.id,{})
        if admission.get("scope") != self._scope(run):
            return False
        digest = hashlib.sha256(json.dumps(event.as_dict(),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with self.store.db() as db:
            row = db.execute("SELECT version FROM core_events WHERE id=? AND deleted=0",(event.id,)).fetchone()
        return bool(row and row[0] == admission["version"] == digest)

    def material_request(self, event):
        # Internal access to this accepted input only. View/read/post routes still need the unlocked session.
        self.validate_event(event)
        return self.sessions.authority.for_event(event)

    def reply_cancelled(self, identifier):
        return self.store.state("web-message:"+identifier, {}).get("reason") == "reply_cancelled"

    def guard_reply_commit(self, event):
        if self.reply_cancelled(event.id):
            raise AssistantError("room_reply_cancelled", "本次回复已取消")

    def validate_event(self, event):
        super().validate_event(event)
        self.guard_reply_commit(event)

    async def run_cancellable_reply(self, event, work):
        # The core already serializes this conversation. Only cancel its current
        # attempt, never the scheduler, the account-switch barrier or another input.
        with self.store.lock:
            if self.reply_cancelled(event.id):
                return {"cancelled": True}
            task = asyncio.create_task(work(event))
            self._processing[event.id] = task
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not self.reply_cancelled(event.id):
                raise
            return {"cancelled": True}
        except AssistantError as error:
            if error.code != "room_reply_cancelled":
                raise
            return {"cancelled": True}
        finally:
            with self.store.lock:
                self._processing.pop(event.id, None)

    def cancel(self, token, event_id):
        if not isinstance(event_id, str) or not re.fullmatch("event-[a-f0-9]{40}", event_id):
            raise AssistantError("invalid_message", "消息标识无效")
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self.request(token)
            row = db.execute("SELECT body,version FROM core_events WHERE id=? AND actor=? AND conversation=? "
                             "AND platform='local' AND account=? AND deleted=0",
                             (event_id,request.actor,request.conversation,self.account)).fetchone()
            admission = self.store.state("desktop-room-input:"+event_id,{},db)
            if not row or row["version"] != admission.get("version"):
                raise AssistantError("access_denied", "当前会客室没有这条可取消的消息")
            self.images.access.lease(request, ["event/" + event_id])
            if self.reply_cancelled(event_id):
                return {"cancelled": True}
            key = stable_key("dispatch", self.platform, self.account, event_id, "message.reply")
            action = db.execute("SELECT status FROM core_actions WHERE dedupe=?", (key,)).fetchone()
            if action and action[0] in {"dispatching", "succeeded", "uncertain"}:
                raise AssistantError("message_sent", "回复已提交或正在确认，无法取消")
            if self.store.state("core-processed:"+event_id, None, db) == row["version"]:
                raise AssistantError("message_sent", "消息已经处理完成")
            event = Event.from_dict(self.store.ledger_decode("core_events.body",row["body"]))
            self.core.record_user_turn(event, self.core.governed_context(event))
            self.store.set_state("core-processed:"+event_id,row["version"],db)
            self.store.set_state("web-message:"+event_id,{"status":"cancelled","reason":"reply_cancelled"},db)
            task = self._processing.get(event_id)
        if task and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)
        return {"cancelled": True}

    @asynccontextmanager
    async def room_decision_material(self, event, conversation, *, guard):
        async with super().decision_material(event, guard=guard) as material:
            files = material["images"]
            candidates = []
            seen = {path.stem for path in files}
            for turn in [*reversed(conversation["turns"]), *conversation["recalled"]]:
                if len(files) + len(candidates) >= 3:
                    break
                data = turn["data"]
                if data["speaker"] != "user" or not data.get("attachments") or not self.reply_cancelled(data["message_id"]):
                    continue
                for attachment in data["attachments"]:
                    match = re.fullmatch(r"input-image:([a-f0-9]{32})",attachment.get("url",""))
                    if match and match[1] not in seen:
                        with self.store.db() as db:
                            bound = db.execute("SELECT 1 FROM input_image_blobs WHERE id=? AND event_id=?",
                                               (match[1],data["message_id"])).fetchone()
                        if not bound:
                            continue
                        seen.add(match[1])
                        candidates.append({"turn":data["id"], "image":match[1]})
                        if len(files) + len(candidates) == 3:
                            break
            if candidates:
                # Reserve the manifest in the ordinary text budget. Only images
                # whose source turn survives final packing reach the model.
                material["context"]["history_images_after_current"] = candidates
                def finalize(context):
                    kept = {item["data"]["id"] for section in ("turns","recalled")
                            for item in context["conversation"][section]}
                    selected = [item for item in candidates if item["turn"] in kept]
                    request = self.material_request(event)
                    for item in selected:
                        guard()
                        path = material["directory"] / (item["image"]+".png")
                        path.write_bytes(self.images.read(request,item["image"]))
                        files.append(path)
                    context["event_material"].update(history_images_after_current=selected, images_read=len(files))
                material["finalize_images"] = finalize
            yield material

    def queue(self, token):
        with self.store.lock, self.store.db() as db:
            rows = super().queue(token)
            for row in rows:
                if row["status"] == "cancelled":
                    continue
                key = stable_key("dispatch", self.platform, self.account, row["id"], "message.reply")
                action = db.execute("SELECT status FROM core_actions WHERE dedupe=?", (key,)).fetchone()
                delivery = self.store.state("core-delivery:" + row["id"], {}, db)
                if action and action[0] in {"succeeded", "uncertain", "dispatching"}:
                    row["status"] = action[0]
                elif row["id"] in self._processing:
                    row["status"] = "processing"
                elif delivery.get("status") == "deferred":
                    row.update(status=delivery["status"], reason=delivery.get("reason", ""), due_at=delivery.get("due_at"))
                elif action and action[0] in {"failed", "cancelled"}:
                    row["status"] = action[0]
                elif delivery.get("status") == "completed":
                    row.update(status="completed", reason=delivery.get("reason", ""))
        return rows

    def maintenance_request(self):
        manager = self.sessions.manager
        return self.request(manager.session_id) if self.session_active(manager.session_id) else None

    async def runtime_poll(self):
        run = self.core.guard()
        return [event for event in await self.core.poll() if self.accepted_for_run(event,run)]
