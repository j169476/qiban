"""One Windows owner, isolated database, local synthetic decisions only."""
import asyncio
import base64
import hashlib
import io
import uuid
from dataclasses import replace

import pytest

from companion_core.errors import AssistantError
from qiban.desktop_runtime import DesktopRuntime
from tests_postgres.test_desktop_management import call, desktop_store, service
from tests_postgres.test_desktop_runtime import Adapter
from tests_postgres.test_wechat_role import Model

__all__ = ["desktop_store","service"]


def picture_file(directory, name, color):
    from PIL import Image

    path = directory / name
    Image.new("RGB", (21, 12), color).save(path)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("in_flight", [False, True])
async def test_cancel_only_this_reply_keeps_photo_and_supplies_it_to_followup(service,tmp_path,in_flight):
    from PIL import Image

    entered, stopped = asyncio.Event(), asyncio.Event()
    inspected = []
    class CancelModel(Model):
        async def generate(self, instructions, context, schema, images=(), **kwargs):
            if context["event"]["text"] == "先看红图":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            turns = context["conversation"]["turns"]
            assert any(turn["data"]["content"] == "先看红图" for turn in turns)
            assert context["event_material"]["images_read"] == 1
            with Image.open(images[0]) as image:
                assert image.getpixel((0,0)) == (255,0,0)
            inspected.append(images[0])
            return await super().generate(instructions,context,schema,images,**kwargs)

    room = service.room
    runtime = DesktopRuntime(service,"cancel-photo",room_model=CancelModel(),adapters=[room])
    try:
        password = {"password":"temporary-strong-pass"}
        token = call(service,"lock.enable",password)["result"]["room_session"]
        runtime.start("local")
        first = {"session":token,"text":"先看红图","nonce":uuid.uuid4().hex,
                 "files":[picture_file(tmp_path,"red.png","red")]}
        event_id = call(service,"room.post_files",first)["result"]["id"]
        cancel = {"session":token,"id":event_id}
        event, = await room.runtime_poll()
        if in_flight:
            task = asyncio.create_task(room.core.handle(event))
            await asyncio.wait_for(entered.wait(),10)
        assert (await asyncio.to_thread(call,service,"room.cancel",cancel))["ok"]
        if in_flight:
            assert (await asyncio.wait_for(task,5))["cancelled"] and stopped.is_set()
        else:
            assert (await room.core.handle(event))["cancelled"] and not entered.is_set()
        assert call(service,"room.cancel",cancel)["ok"]
        assert call(service,"room.post_files",first)["result"]["duplicate"]
        assert await room.runtime_poll() == []
        assert call(service,"session.lock")["ok"]
        assert call(service,"room.cancel",cancel)["error"] == "desktop_locked"
        token = call(service,"session.unlock",password)["result"]["room_session"]
        row, = call(service,"room.history",{"session":token,"before":""})["result"]["messages"]
        assert row["text"] == first["text"] and row["attachments"]
        queued, = call(service,"room.queue",{"session":token})["result"]["messages"]
        assert queued["reason"] == "reply_cancelled" and queued["images"] == 1
        assert call(service,"room.preview",{"session":token,"turn":row["id"],"image":row["attachments"][0]["url"][12:]})["ok"]
        followup = call(service,"room.post",{"session":token,"text":"现在说说刚才的颜色","nonce":uuid.uuid4().hex})
        latest, = await room.runtime_poll()
        assert (await room.core.handle(latest))["action"]["status"] == "succeeded"
        assert call(service,"room.cancel",{"session":token,"id":followup["result"]["id"]})["error"] == "message_sent"
        assert len(inspected)==1 and not inspected[0].exists()
        rows = call(service,"room.history",{"session":token,"before":""})["result"]["messages"]
        assert [r["text"] for r in rows]==["先看红图","现在说说刚才的颜色","synthetic reply"]
    finally:
        runtime.close()


async def test_cancel_after_dispatch_claim_is_refused(service):
    entered, release = asyncio.Event(), asyncio.Event()
    room = service.room
    execute = room.execute
    async def pause_at_commit(*args,**kwargs):
        entered.set()
        await release.wait()
        return await execute(*args,**kwargs)
    room.execute = pause_at_commit
    runtime = DesktopRuntime(service,"cancel-race",room_model=Model(),adapters=[room])
    try:
        token = call(service,"session.status")["result"]["room_session"]
        runtime.start("local")
        identifier = call(service,"room.post",{"session":token,"text":"提交竞态","nonce":uuid.uuid4().hex})["result"]["id"]
        event, = await room.runtime_poll()
        task = asyncio.create_task(room.core.handle(event))
        await asyncio.wait_for(entered.wait(),10)
        assert call(service,"room.cancel",{"session":token,"id":identifier})["error"] == "message_sent"
        release.set()
        assert (await asyncio.wait_for(task,5))["action"]["status"] == "succeeded"
        assert await room.runtime_poll() == []
    finally:
        release.set()
        runtime.close()


async def test_cancelled_photo_is_not_passed_when_text_budget_drops_source_turn(service,tmp_path):
    room = service.room
    runtime = DesktopRuntime(service,"cancel-packed-out",room_model=Model(),adapters=[room])
    try:
        token = call(service,"session.status")["result"]["room_session"]
        runtime.start("local")
        args = {"session":token,"nonce":uuid.uuid4().hex,"text":"旧图",
                "files":[picture_file(tmp_path,"旧图.png","red")]}
        event_id = call(service,"room.post_files",args)["result"]["id"]
        assert call(service,"room.cancel",{"session":token,"id":event_id})["ok"]
        assert call(service,"room.post",{"session":token,"text":"后续消息","nonce":uuid.uuid4().hex})["ok"]
        event, = await room.runtime_poll()
        row, = room.history(token)
        item = service.store.read("conversation_turns",row["id"])
        async with room.room_decision_material(event,{"turns":[item],"recalled":[]},guard=lambda:None) as material:
            assert material["context"]["history_images_after_current"]
            packed = {"conversation":{"turns":[],"recalled":[]},"event_material":dict(material["context"])}
            material["finalize_images"](packed)
            assert not material["images"] and not packed["event_material"]["history_images_after_current"]
            assert packed["event_material"]["images_read"] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("caption", ["", "请看看这几张图片🙂"])
async def test_desktop_picture_submission_replay_and_preview(service, tmp_path, caption):
    from PIL import Image

    class Pictures(Model):
        async def generate(self, instructions, context, schema, images=(), **kwargs):
            pixels = []
            for path in images:
                with Image.open(path) as image:
                    pixels.append(image.getpixel((0, 0)))
            assert len(pixels) == 3
            for pixel, expected in zip(pixels, [(255, 0, 0), (0, 0, 255), (0, 128, 0)]):
                assert max(abs(a-b) for a,b in zip(pixel, expected)) < 8
            return await super().generate(instructions, context, schema, images, **kwargs)

    model = Pictures()
    runtime = DesktopRuntime(service, "room-pictures", room_model=model, adapters=[service.room])
    try:
        password = {"password": "temporary-strong-pass"}
        token = call(service, "lock.enable", password)["result"]["room_session"]
        runtime.start("local")
        files = [picture_file(tmp_path, color+"🙂."+ext, color)
                 for color,ext in [("red","png"), ("blue","jpg"), ("green","webp")]]
        args = {"session": token, "text": caption, "files": files, "nonce": uuid.uuid4().hex}
        assert call(service, "room.post_files", args)["result"]["new"]
        queued, = call(service, "room.queue", {"session":token})["result"]["messages"]
        assert queued["status"] == "queued" and queued["images"] == 3
        (tmp_path / "red🙂.png").unlink()
        assert call(service, "room.post_files", args)["result"]["duplicate"]
        assert call(service, "room.post_files", {**args, "text":"changed"})["error"] == "message_conflict"
        assert call(service, "room.post_files", {**args, "files":files[::-1]})["error"] == "message_conflict"
        assert call(service, "session.lock")["ok"]
        event, = await service.room.runtime_poll()
        assert event.text == caption and len(event.attachments) == 3
        assert (await runtime.cores["local"].handle(event))["action"]["status"] == "succeeded"
        current = call(service, "session.unlock", password)["result"]["room_session"]
        assert call(service, "room.queue", {"session":token})["error"] == "desktop_locked"
        assert call(service, "room.queue", {"session":current})["result"]["messages"][0]["status"] == "succeeded"
        query = {"session":current, "before":"", "query":"请看看"}
        matches = call(service, "room.search", query)["result"]["messages"]
        assert [item["text"] for item in matches] == ([caption] if caption else [])
        assert call(service, "room.search", {**query, "session":token})["error"] == "desktop_locked"
        history = call(service, "room.history", {"session":current, "before":""})["result"]["messages"]
        user, reply = history
        assert user["text"] == caption and reply["text"] == "synthetic reply" and len(model.calls) == 1
        preview = {"session": current, "turn": user["id"], "image": user["attachments"][1]["url"][12:]}
        output = call(service, "room.preview", preview)["result"]["data"]
        with Image.open(io.BytesIO(base64.b64decode(output))) as image:
            assert image.size == (21, 12) and image.getpixel((0, 0))[2] > 245
        assert call(service, "room.preview", {**preview, "turn":reply["id"]})["error"] == "access_denied"
        assert call(service, "room.preview", {**preview, "session":token})["error"] == "desktop_locked"
        with service.store.db() as db:
            db.execute("UPDATE core_actions SET status='failed' WHERE status='succeeded'")
        assert call(service, "room.queue", {"session":current})["result"]["messages"][0]["status"] == "failed"
    finally:
        runtime.close()


def test_invalid_selected_files_rollback_without_reply(service, tmp_path):
    runtime = DesktopRuntime(service, "room-invalid-pictures", room_model=Model(), adapters=[service.room])
    try:
        token = call(service, "session.status")["result"]["room_session"]
        runtime.start("local")
        item = picture_file(tmp_path, "ok.png", "red")
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"wrong format")
        invalid = {"path":str(bad), "sha256":hashlib.sha256(bad.read_bytes()).hexdigest()}
        args = {"session":token, "text":"", "nonce":uuid.uuid4().hex}
        for files, error in [([], "invalid_message"), ([item]*4, "invalid_message"), ([item]*2, "invalid_image"),
                             ([item, invalid], "invalid_image"), ([{**item, "sha256":"0"*64}], "image_changed")]:
            assert call(service, "room.post_files", {**args, "files":files})["error"] == error
        assert call(service, "room.post", args)["error"] == "invalid_message"
        with service.store.db() as db:
            assert db.execute("SELECT count(*) FROM input_image_blobs").fetchone()[0] == 0
        assert call(service, "room.post_files", {**args, "files":[item]})["result"]["new"]
    finally:
        runtime.close()


async def test_lock_during_generation_finishes_once_with_new_and_old_views_denied(service):
    started, resume = asyncio.Event(), asyncio.Event()
    class SlowModel(Model):
        async def generate(self, *args, **kwargs):
            started.set()
            await resume.wait()
            kwargs["guard"]()
            return await super().generate(*args, **kwargs)
    model = SlowModel()
    runtime = DesktopRuntime(service, "locked-generation", room_model=model, adapters=[service.room])
    try:
        session = call(service,"lock.enable",{"password":"temporary-strong-pass"})["result"]["room_session"]
        generation = runtime.start("local")["run"]["generation"]
        nonce = uuid.uuid4().hex
        post = {"session":session,"text":"我先锁屏，稍后来看 🙂","nonce":nonce}
        assert call(service,"room.post",post)["result"]["new"]
        event, = await service.room.runtime_poll()
        task = asyncio.create_task(runtime.cores["local"].handle(event))
        await asyncio.wait_for(started.wait(),10)
        assert call(service, "room.queue", {"session":session})["result"]["messages"][0]["status"] == "processing"
        assert call(service,"session.lock")["ok"]
        assert call(service,"room.post",{**post,"nonce":uuid.uuid4().hex})["error"]=="desktop_locked"
        assert call(service,"room.history",{"session":session,"before":""})["error"]=="desktop_locked"
        resume.set()
        assert (await asyncio.wait_for(task,15))["action"]["status"]=="succeeded"
        runtime.cores["local"].guard(generation)
        assert call(service,"room.history",{"session":session,"before":""})["error"]=="desktop_locked"
        assert await service.room.runtime_poll() == []
        current = call(service,"session.unlock",{"password":"temporary-strong-pass"})["result"]["room_session"]
        assert call(service,"room.post",post)["error"]=="desktop_locked"
        assert call(service,"room.post",{**post,"session":current})["result"]["duplicate"]
        rows = call(service,"room.history",{"session":current,"before":""})["result"]["messages"]
        assert [r["text"] for r in rows]==[post["text"],"synthetic reply"]
        assert len(model.calls)==1
    finally:
        resume.set()
        runtime.close()


async def test_accepted_input_pending_at_lock_and_only_intentional_model_handoff_can_resume(service):
    model = Model()
    runtime = DesktopRuntime(service,"locked-queue",room_model=model,adapters=[service.room])
    try:
        token = call(service,"lock.enable",{"password":"temporary-strong-pass"})["result"]["room_session"]
        first = runtime.start("local")["run"]["generation"]
        assert call(service,"room.post",{"text":"已接收，等待执行","session":token,"nonce":uuid.uuid4().hex})["ok"]
        assert call(service,"session.lock")["ok"]
        event, = await service.room.runtime_poll()
        with pytest.raises(AssistantError):
            service.room.validate_event(replace(event,text="伪造正文"))
        runtime.change_model_account(lambda:{"changed":True},ready=True)
        run = runtime.cores["local"].guard()
        assert run["generation"]!=first and run["room_intake"]==first
        assert (await runtime.cores["local"].handle(event))["action"]["status"]=="succeeded"
        runtime.cores["local"].stop("explicit_stop")
        runtime.start("local")
        with pytest.raises(AssistantError):
            service.room.validate_event(event)
    finally:
        runtime.close()


async def test_locked_worker_reads_only_the_image_in_the_accepted_message(service):
    from PIL import Image

    received = []
    class ImageModel(Model):
        async def generate(self, instructions, context, schema, images=(), **kwargs):
            received.extend(images)
            assert len(images) == 1
            with Image.open(images[0]) as image:
                assert image.getpixel((0, 0)) == (255, 0, 0)
            return await super().generate(instructions, context, schema, images, **kwargs)

    room = service.room
    runtime = DesktopRuntime(service, "locked-attachment", room_model=ImageModel(), adapters=[room])
    try:
        token = call(service, "lock.enable", {"password": "temporary-strong-pass"})["result"]["room_session"]
        runtime.start("local")
        uploads = []
        for color in ["red", "blue"]:
            encoded = io.BytesIO()
            Image.new("RGB", (2, 2), color).save(encoded, "PNG")
            uploads.append(room.images.upload(room.request(token), encoded.getvalue())["id"])
        assert room.post(token, "看第一张图", uuid.uuid4().hex, uploads[:1])["new"]
        assert call(service, "session.lock")["ok"]
        event, = await room.runtime_poll()
        assert (await runtime.cores["local"].handle(event))["action"]["status"] == "succeeded"
        assert [path.stem for path in received] == uploads[:1]
        assert all(not path.exists() for path in received)
    finally:
        runtime.close()


async def test_account_switch_during_locked_generation_replaces_old_attempt(service):
    entered = asyncio.Event()
    attempts = []
    account = "first"

    class SwitchingModel(Model):
        async def generate(self, *args, **kwargs):
            attempts.append(("started", account))
            if account == "first":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    attempts.append(("stopped", account))
            return await super().generate(*args, **kwargs)

    def select_new_account():
        nonlocal account
        assert attempts == [("started", "first"), ("stopped", "first")]
        account = "second"
        return {"changed": True}

    runtime = DesktopRuntime(service, "locked-switch", room_model=SwitchingModel(), adapters=[service.room])
    try:
        password = {"password": "temporary-strong-pass"}
        token = call(service, "lock.enable", password)["result"]["room_session"]
        runtime.start("local")
        assert call(service, "room.post", {"session": token, "text": "换号接手", "nonce": uuid.uuid4().hex})["ok"]
        event, = await service.room.runtime_poll()
        task = asyncio.create_task(runtime.cores["local"].handle(event))
        await asyncio.wait_for(entered.wait(), 10)
        assert call(service, "session.lock")["ok"]
        await asyncio.wait_for(asyncio.to_thread(runtime.change_model_account, select_new_account, ready=True), 15)
        assert (await asyncio.wait_for(task, 15))["action"]["status"] == "succeeded"
        assert attempts == [("started", "first"), ("stopped", "first"), ("started", "second")]
        assert await service.room.runtime_poll() == []
        renewed = call(service, "session.unlock", password)["result"]["room_session"]
        history = call(service, "room.history", {"session": renewed, "before": ""})["result"]["messages"]
        assert [message["text"] for message in history] == ["换号接手", "synthetic reply"]
    finally:
        runtime.close()


async def test_owner_history_survives_lock_and_new_session(service):
    model = Model()
    runtime = DesktopRuntime(service,"room-suite",room_model=model,adapters=[service.room])
    try:
        unlocked = call(service,"lock.enable",{"password":"temporary-strong-pass"})["result"]["room_session"]
        def history(session):
            return call(service,"room.history",{"session":session,"before":""})
        def post(session, text, nonce):
            return call(service,"room.post",{"session":session,"text":text,"nonce":nonce})
        assert history(unlocked)["result"]["messages"] == []
        assert post(unlocked,"暂停",uuid.uuid4().hex)["error"] == "room_paused"
        runtime.start("local")
        nonce=uuid.uuid4().hex
        assert post(unlocked,"桌面私聊 123 🙂",nonce)["ok"]
        assert post(unlocked,"桌面私聊 123 🙂",nonce)["result"]["duplicate"]
        event,=await service.room.runtime_poll()
        assert (await runtime.cores["local"].handle(event))["action"]["status"]=="succeeded"
        original=history(unlocked)["result"]["messages"]
        assert [m["text"] for m in original]==["桌面私聊 123 🙂","synthetic reply"]
        assert not service.room.request(unlocked).administrator
        assert call(service,"session.lock")["ok"] and history(unlocked)["error"]=="desktop_locked"
        assert post(unlocked,"过期请求",uuid.uuid4().hex)["error"]=="desktop_locked"
        renewed=call(service,"session.unlock",{"password":"temporary-strong-pass"})["result"]["room_session"]
        assert renewed != unlocked
        assert history(unlocked)["error"]=="desktop_locked"
        assert post(unlocked,"延迟请求",uuid.uuid4().hex)["error"]=="desktop_locked"
        assert history(renewed)["result"]["messages"]==original
    finally:
        runtime.close()


def test_lock_does_not_change_qq_or_wechat_generation(service):
    qq, wechat = Adapter(), Adapter()
    wechat.platform = "wechat"
    runtime = DesktopRuntime(service, "external-lock", model=Model(), adapters=[qq,wechat,service.room])
    try:
        generations = {p:runtime.start(p)["run"]["generation"] for p in ("qq","wechat")}
        assert call(service,"lock.enable",{"password":"temporary-strong-pass"})["ok"]
        assert call(service,"session.lock")["ok"]
        for platform,generation in generations.items():
            runtime.cores[platform].guard(generation)
        assert qq.stopped == wechat.stopped == []
    finally:
        runtime.close()
