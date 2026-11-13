"""Lazy QQ adapter: bounded encrypted queue, explicit run ownership, no UI control."""
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from companion_core import delivery, events
from companion_core.contracts import ActionResult, Capability, Event, stable_key
from companion_core.errors import AssistantError
from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTarget, require
from companion_core.reply_exclusion import accepts, revision, seconds
from companion_core.reply_policy import Participation
from companion_core.schema import obj

from . import config
from .normalization import normalize


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def enrollment_intent(options):
    if options.get("transport") == "native":
        options = {key: value for key, value in options.items() if key not in {"runtime", "data"}}
    return digest(options)


class QQModule:
    platform = "qq"
    interactive = True
    poll_interval_seconds = 1
    manages_action_quota = True

    def __init__(self, store, project, *, options=None, factory=None):
        self.store, self.project = store, Path(project)
        if options is not None:
            self.options = config.validate(options)
        else:
            try:
                self.options = config.load(project)
            except Exception:
                self.options = None  # Bad optional QQ configuration cannot stop other modules.
        self.account = self.options["account"] if self.options else "unconfigured"
        native = self.options is not None and self.options.get("transport") == "native"
        if factory is not None:
            self.factory = factory
        elif native:
            from .native_provider import NativeProvider

            self.factory = lambda: NativeProvider(self.store, config.native_parameters(self.options), follow_settings=True)
        else:
            from .transport import OneBot

            self.factory = lambda: OneBot(self.options, config.token(project, self.account))
        self.core = self.provider = self.socket = self.reader = None
        self.native_generation = None
        self.ready = self.tainted = False
        self.code, self.failures, self.retry_at = "", 0, 0
        self.queue_key = "qq-inbox:" + self.account
        self.enrollment_key = "qq-enrollment:" + self.account
        self.participation = Participation(store)
        from companion_core.merged_inbox import MergedInbox

        self.merged = MergedInbox(store)
        from .native_inbox import NativeInbox

        self.native_inbox = NativeInbox(self)
        self.send_lock = asyncio.Lock()
        from .lifecycle import ManagedBridge

        self.bridge = ManagedBridge(store, self.options) if self.options and not native else None

    def bind_runtime(self, core):
        require(core.store is self.store, "qq_store_mismatch")
        self.core = core
        self.participation = Participation(self.store, clock=core.clock)

    def capabilities(self):
        from .voice_settings import VoiceSettings

        settings=VoiceSettings(self.store).read()
        properties={'text': {'type':'string','minLength':1,'maxLength':1000}}
        description='向当前已授权QQ会话发送纯文本'
        if settings['enabled'] and (self.options and self.options.get('transport')=='native'
                                   or callable(getattr(self.provider,'send_reply_action',None))):
            properties['format']={'enum':['text','voice']}
            description=(f"按语境选择文字text或语音voice，语音最长{settings['seconds']}秒。"
                         '文字应保持完整；代码、链接和不适合朗读的内容选择文字。合成失败或超限会使用同一份文字。')
        parameters = obj(properties, required=['text'])
        if (self.options and self.options.get('transport') == 'native'
                or callable(getattr(self.provider, 'send_bundle_action', None))):
            sticker = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
            parameters = {'anyOf': [parameters,
                obj({**properties, 'sticker_id': sticker}, required=['text', 'sticker_id']),
                obj({'sticker_id': sticker})]}
            from .file_replies import FileReplySettings
            file_limits = FileReplySettings(self.store).read()
            file_ids = {'type': 'array', 'minItems': 1, 'maxItems': 12,
                        'uniqueItems': True, 'items': {'type': 'string',
                        'pattern': '^(library|attachment):[a-zA-Z0-9_-]{1,128}$'}}
            parameters['anyOf'].extend([
                obj({'file_ids': file_ids}),
                obj({**properties, 'file_ids': file_ids}, required=['text', 'file_ids']),
                obj({'file_ids': file_ids, 'sticker_id': sticker}),
                obj({**properties, 'file_ids': file_ids, 'sticker_id': sticker}, required=['text','file_ids','sticker_id'])])
            description += (f" 可从files.items选择file_ids，最多{file_limits['count']}份、合计{file_limits['total_mib']} MiB，"
                            '单份10 MiB；可单发或在正文后按列表顺序发送，再发送表情。')
            description += (' 可由本次上下文stickers.items选择一张表情，填写sticker_id；'
                '可以单独发送，也可附在文字或语音后。是否使用由你按语境决定，无频率或同图冷却限制。'
                '仅可选择本次实际提供的ID，不得编造路径或素材。')
        return [Capability('message.reply',description,parameters),
            Capability('conversation.end','仅群聊：话题结束或转向无关讨论时停止参与，等待再次@或引用。不会发消息。',obj({}))]

    def reply_targets(self):
        if self.options is None:
            return ()
        saved = self.store.state(self.enrollment_key)
        if not saved or saved.get("intent") not in {enrollment_intent(self.options), digest(self.options)}:
            return ()
        return tuple(ReplyTarget(ReplyScope("qq", self.account, kind, target),
            saved["identity"].get("names", {}).get(kind, {}).get(target, ""),
            saved["identity"]["account_label"] or self.account,
            "私聊直接回应；群聊须@或引用唤起，随后限额参与")
            for kind, field in (("private", "private_targets"), ("group", "groups")) for target in self.options[field])

    def probe(self):
        """Explicit read-only setup. Saves only the configured, verified targets."""
        require(self.options is not None, "qq_configuration")
        provider = self.factory()
        try:
            identity = provider.probe()
            require(identity["account"] == self.account, "qq_account_mismatch")
            value = {"intent": enrollment_intent(self.options), "identity": identity, "at": datetime.now(timezone.utc).isoformat()}
            with self.store.lock:
                require(self.store.ledger_encode("qq-preflight", {}).startswith("enc1:"), "qq_store_unencrypted")
                self.store.set_state(self.enrollment_key, value)
            return {"verified": True, "private_count": len(self.options["private_targets"]),
                    "group_count": len(self.options["groups"]), "messages_sent": 0, "role_started": False}
        finally:
            provider.close()

    @property
    def intake_options(self):
        current = getattr(self.provider, "intake_options", None)
        if current is None:
            return self.options
        return {**self.options, "private_targets":current["private_targets"], "groups":current["groups"]}

    def validate_start(self):
        require(self.options is not None, "qq_configuration")
        require(self.provider is None and self.reader is None, "qq_stopping")
        from companion_core.reply_audience import Audience, read
        dynamic = self.options.get("transport") == "native" and any(
            read(self.store,Audience("qq",self.account,kind))["enabled"] for kind in ("private","group"))
        require(bool(self.reply_targets()) or dynamic and self.store.state(self.enrollment_key), "qq_enrollment_required")
        self.provider = self.factory()
        self.tainted = False
        self.code, self.failures, self.retry_at = "", 0, 0

    def validate_run(self, run):
        require(self.provider is not None, "qq_not_started")
        require(not self.tainted, "qq_send_unconfirmed")

    def scope(self, event):
        return ReplyScope("qq", self.account, event.visibility, event.metadata["qq"]["target_id"])

    def sent_key(self, kind, target, identifier):
        return stable_key("qq-sent", self.account, kind, target, identifier)

    def accept(self, raw):
        """Called only by the authenticated transport, never a model tool."""
        event = normalize(raw, self.options, sent_reference=lambda kind, target, identifier:
                          self.store.state(self.sent_key(kind, target, identifier)) is not None)
        self._accept_event(event)

    def accept_native(self, raw, *, identity, history_status=None):
        """Owned native receiver entry; identity/status are trusted protocol fields."""
        from companion_core.history_status import validate_history_status

        from .native_normalization import normalize_native

        report = validate_history_status(history_status)
        event = normalize_native(raw, self.options, identity=identity,
                                 sent_reference=lambda kind, target, identifier:
                                 self.store.state(self.sent_key(kind, target, identifier)) is not None)
        self._accept_event(event, history_status=report)

    def accept_native_page(self, packet, *, identity, catchup=False):
        """Commit an owned native receiver page before acknowledging its position."""
        return self.native_inbox.publish(packet, identity=identity, catchup=catchup)

    def _accept_event(self, event, *, history_status=None):
        if event is None:
            return
        stamp = datetime.fromisoformat(event.occurred_at).timestamp()
        now = self.participation.now()
        if stamp > now + 30:
            return
        with self.store.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            key, fingerprint = "qq-seen:" + event.id, digest(event.as_dict())
            previous = self.store.state(key, None, db)
            if previous is not None:
                require(previous == fingerprint, "qq_event_conflict")
                if history_status is not None and db.execute(
                        "SELECT id FROM core_events WHERE id=? AND deleted=0", (event.id,)).fetchone():
                    self.merged.record_history(event, history_status)
                return
            self.store.set_state(key, fingerprint, db)
            if ReplyAuthorizations(self.store).version(self.scope(event), db=db) is None:
                return
            # A conversation the administrator excluded, or one whose exclusion was
            # lifted after this message was sent, is dropped here and not ingested.
            if not accepts(self.store, self.scope(event), stamp, db=db):
                return
            receipt = events.ingest(self.store, event)
            require(not receipt["changed"], "qq_event_conflict")
            if history_status is not None:
                self.merged.record_history(event, history_status)
            queue = self.store.state(self.queue_key, [], db)
            require(len(queue) < 512, "qq_inbox_backpressure")
            self.store.set_state(self.queue_key, queue + [event.id], db)

    async def _read(self, socket):
        try:
            async for raw in socket:
                self.accept(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except AssistantError as exc:
            self.code = exc.code
        except Exception:
            self.code = "qq_provider_unavailable"
        finally:
            self.ready = False
            self.failures += 1
            self.retry_at = time.monotonic() + min(60, 2 ** min(self.failures, 6))

    async def _connect(self):
        if self.ready or time.monotonic() < self.retry_at:
            return
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.socket is not None:
            await self.socket.close()
            self.socket = None
        try:
            identity = (await self.bridge.ensure(self.provider, self.core.guard) if self.options.get("launch") else
                        await asyncio.to_thread(self.provider.probe))
            require(identity["account"] == self.account, "qq_account_mismatch")
            self.store.set_state(self.enrollment_key, {"intent": enrollment_intent(self.options), "identity": identity,
                                                       "at": datetime.now(timezone.utc).isoformat()})
            native = hasattr(self.provider, "poll_native")
            self.socket = None if native else await self.provider.connect()
            # Recheck both sides of the connection before accepting any event.
            await asyncio.to_thread(self.provider.identity)
            await asyncio.to_thread(self.core.guard)
            self.ready, self.code, self.failures = True, "", 0
            if self.bridge:
                self.bridge.state = "connected"
            if not native:
                self.reader = asyncio.create_task(self._read(self.socket))
        except AssistantError as exc:
            self.code = exc.code
            self.failures += 1
            self.retry_at = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            if self.options.get("launch") or exc.code in {"qq_account_mismatch", "qq_target_unverified"}:
                if self.bridge:
                    await asyncio.to_thread(self.bridge.stop)
                raise

    async def runtime_poll(self):
        run = await asyncio.to_thread(self.core.guard)
        if self.code not in {"", "qq_provider_unavailable", "qq_login_required"}:
            raise AssistantError(self.code, "QQ连接器需要检查，其他平台保持原状态")
        if hasattr(self.provider, "poll_native"):
            self.native_generation = run["generation"]
        await self._connect()
        if not self.ready or run["read_only"]:
            return []
        if hasattr(self.provider, "poll_native"):
            provider = self.provider
            try:
                await asyncio.to_thread(provider.poll_native, self,
                                        lambda: self.core.guard(run["generation"]))
                await asyncio.to_thread(self.core.guard, run["generation"])
            except BaseException as exc:
                self.ready = False
                self.code = getattr(exc, "code", "qq_native_read_failed")
                provider.interrupt()
                raise
        result, groups = [], {}
        batch_size = self.store.read("companion")["data"].get("inbox_batch_size", 3)
        with self.store.lock, self.store.db() as db:
            legacy = self.store.state(self.queue_key, [], db)
            queue = list(dict.fromkeys(legacy + self.native_inbox.pending(db)))
            retained = []
            for event_id in queue:
                try:
                    saved = events.get(self.store, event_id)
                except AssistantError as exc:
                    if exc.code == "unknown_evidence":
                        self.native_inbox.drop(event_id, db)
                        continue
                    raise
                event = Event.from_dict(saved["event"])
                if self.merged.retired(event, db):
                    self.native_inbox.drop(event_id, db)
                    continue
                if ReplyAuthorizations(self.store).version(self.scope(event), db=db) is None:
                    self.native_inbox.drop(event_id, db)
                    continue
                # Queued input from before a lifted exclusion is retired, not replayed.
                if not accepts(self.store, self.scope(event), seconds(event.occurred_at), db=db):
                    self.native_inbox.drop(event_id, db)
                    continue
                if event_id in legacy:
                    retained.append(event_id)
                if self.native_inbox.incomplete(event, db):
                    continue
                groups.setdefault(event.thread, []).append(event)
            for incoming in list(groups.values())[:batch_size]:
                from companion_core.reply_audience import Audience, read
                scope = self.scope(incoming[0])
                audience = read(self.store,Audience("qq",self.account,scope.kind),db)
                window = self.native_inbox.state(scope.kind,scope.target_id,db=db) if audience["enabled"] else None
                # Folding the exclusion revision into the frozen audience revision makes
                # any batch frozen before an exclusion or lift stale, so its members are
                # never answered as one merged reply.
                batch = self.merged.freeze(incoming, since=run["started_at"], generation=run["generation"],
                                           audience_revision=revision(self.store, scope, audience["version"], db=db),
                                           force_catchup=bool(window and window.get("catchup")))
                if batch is None:
                    continue
                event = Event.from_dict(events.get(self.store, batch["anchor"])["event"])
                saved = events.get(self.store, event.id)
                stamp = seconds(event.occurred_at)
                key = "qq-admission:" + event.id
                admission = self.store.state(key, None, db)
                if (admission is None or admission["version"] != saved["version"]
                        or not self.participation.eligible(admission["ticket"], run["generation"])):
                    if event.visibility == "group" and batch["catchup"]:
                        if event.metadata["qq"]["trigger"] not in {"mention", "reply"}:
                            self.merged.finish(batch, "no_trigger")
                            continue
                        ticket = self.participation.admit_backlog(self.scope(event), event.id, run["generation"],
                                                                  batch["id"], message_time=stamp)
                    else:
                        ticket = self.participation.admit(self.scope(event), event.id, event.metadata["qq"]["trigger"],
                                                          run["generation"], message_time=stamp)
                    if ticket is None:
                        if event.visibility == "group":
                            self.merged.finish(batch, "not_admitted")
                        continue
                    admission = {"ticket": ticket, "version": saved["version"]}
                    self.store.set_state(key, admission, db)
                if batch["catchup"] or delivery.eligible(self.core, event, saved["version"], run):
                    result.append(event)
            self.store.set_state(self.queue_key, retained, db)
        return result

    def decision_batch(self, event):
        return self.merged.for_event(event)

    def validate_context_event(self, event):
        require(event.platform == "qq" and event.account == self.account, "qq_event_scope")
        require(self.ready, "qq_provider_unavailable")
        self.core.guard()
        require(digest(events.get(self.store, event.id)["event"]) == digest(event.as_dict()), "qq_event_conflict")
        require(ReplyAuthorizations(self.store).version(self.scope(event)) is not None, "reply_not_authorized")

    def finish_batch(self, event, action):
        admission = self.store.state("qq-admission:" + event.id)
        if admission and admission["ticket"].get("backlog") and action is None:
            self.participation.end(admission["ticket"], admission["ticket"]["generation"])

    async def receive(self, cursor):
        return await self.runtime_poll(), {}

    def validate_event(self, event):
        require(event.platform == "qq" and event.account == self.account, "qq_event_scope")
        require(self.ready, "qq_provider_unavailable")
        run = self.core.guard()
        saved = events.get(self.store, event.id)
        require(digest(saved["event"]) == digest(event.as_dict()), "qq_event_conflict")
        admission = self.store.state("qq-admission:" + event.id)
        require(admission is not None and admission["version"] == saved["version"], "qq_event_untrusted")
        self.participation.check(admission["ticket"], run["generation"])
        return admission["ticket"], run["generation"]

    @asynccontextmanager
    async def decision_material(self, event, *, guard):
        from companion_core.media_budget import prepare_unread

        await asyncio.to_thread(guard)
        ticket, generation = self.validate_event(event)
        media = prepare_unread(self.store, self.merged, event)
        material = {**self.participation.context(ticket, generation), "images_read": 0,
                    "attachments_read": 0, "attachment_status": "not_processed", "media_budget": media,
                    "attachment_guidance": "仅依据本轮实际提供的附件片段回答；未提供图片或语音时不能声称看过或听过。"}
        if callable(getattr(self.provider, 'send_bundle_action', None)):
            from companion_core.sticker_library import StickerLibrary

            material['stickers'] = {'items': StickerLibrary(self.store).candidates(event.text),
                'guidance': '仅按名称和标签选择；这里没有提供图片视觉内容。不必每次使用，可以单独回复一张或附在正文之后。'}
        from companion_core.media_originals import MediaOriginals

        owner = 'qiban-media:' + str(os.getpid()) + ':' + self.core.guard()['owner']
        task = stable_key('media-material', event.id)
        try:
            if callable(getattr(self.provider, 'save_attachment', None)):
                from threading import Event as CancelEvent

                from .native_documents import prepare_documents

                cancelled = CancelEvent()

                def media_guard():
                    if cancelled.is_set():
                        raise AssistantError('media_cancelled', '附件处理已取消。')
                    guard()

                work = asyncio.create_task(asyncio.to_thread(prepare_documents, self, media,
                    owner=owner, task=task, guard=media_guard))
                try:
                    material.update(await asyncio.shield(work))
                except asyncio.CancelledError:
                    cancelled.set()
                    self.provider.interrupt()
                    try:
                        await work
                    except AssistantError:
                        pass
                    raise
            if callable(getattr(self.provider, 'send_bundle_action', None)):
                from .file_replies import ReplyFiles
                scope = self.scope(event)
                authorization = ReplyAuthorizations(self.store).version(scope)
                material['files'] = ReplyFiles(self.store).candidates(scope, authorization, event.text)
                MediaOriginals(self.store).hold(owner, task, [item['id'].split(':',1)[1]
                    for item in material['files']['items'] if item['id'].startswith('attachment:')])
            await asyncio.to_thread(guard)
            yield {"context": material, "images": material.pop("images", ())}
        finally:
            MediaOriginals(self.store).release(owner, task)

    def decision_context_ready(self, event, material):
        # Bind only candidates that survived the final shared context budget.
        self.validate_context_event(event)
        self.store.set_state('qq-sticker-selection:' + event.id, {
            'generation': self.core.guard()['generation'],
            'event_version': events.get(self.store, event.id)['version'],
            'items': {item['id']: item['revision'] for item in
                material.get('event_material', {}).get('stickers', {}).get('items', [])}})

        from .file_replies import FileReplySettings
        offered = material.get('event_material', {}).get('files', {})
        self.store.set_state('qq-file-selection:' + event.id, {
            'generation': self.core.guard()['generation'],
            'event_version': events.get(self.store, event.id)['version'],
            'authorization': ReplyAuthorizations(self.store).version(self.scope(event)),
            'limits': offered.get('limits', FileReplySettings(self.store).read()),
            'items': {item['id']: item for item in offered.get('items', [])}})

    def selected_files(self, event, identifiers):
        selected = self.store.state('qq-file-selection:' + event.id)
        require(selected is not None and selected['generation'] == self.core.guard()['generation']
                and selected['event_version'] == events.get(self.store, event.id)['version'], 'qq_file_selection_expired')
        require(all(value in selected['items'] for value in identifiers), 'qq_file_not_offered')
        value = {**selected, 'items': {identifier: selected['items'][identifier] for identifier in identifiers},
                 'owner': 'qiban-media:' + str(os.getpid()) + ':' + self.core.guard()['owner']}
        from .file_replies import ReplyFiles
        ReplyFiles(self.store).validate(self.scope(event), value)
        return value

    def selected_sticker_revision(self, event, identifier):
        selected = self.store.state('qq-sticker-selection:' + event.id)
        require(selected is not None and selected['generation'] == self.core.guard()['generation']
                and selected['event_version'] == events.get(self.store, event.id)['version'], 'sticker_selection_expired')
        revision = selected['items'].get(identifier)
        require(revision is not None, 'sticker_not_offered')
        return revision

    async def read(self, reference):
        raise AssistantError("qq_read_unsupported", "QQ模块不提供任意聊天读取")

    def _submit(self, event, capability, arguments, guard, prepared_reply=None):
        submitted, action_id = False, None
        try:
            with self.store.lock:
                guard()
                ticket, generation = self.validate_event(event)
                key = stable_key("dispatch", "qq", self.account, event.id, capability)
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM core_actions WHERE dedupe=?", (key,)).fetchone()
                require(row is not None and row["status"] == "dispatching" and row["generation"] == generation, "qq_action_missing")
                body = self.store.ledger_decode("core_actions.body", row["body"])
                require(digest(body["event"]) == digest(event.as_dict()) and body["arguments"] == arguments, "qq_action_mismatch")
                if capability == "conversation.end":
                    self.participation.end(ticket, generation)
                    return ActionResult("succeeded", {"participation_ended": True})
                require(capability == "message.reply", "qq_action_unsupported")
                bundled = 'sticker_id' in arguments or 'file_ids' in arguments
                if bundled:
                    from .reply_bundle import reply_parts

                    reply_parts(arguments)
                    require(callable(getattr(self.provider, 'send_bundle_action', None)), 'qq_sticker_unavailable')
                    sticker_revision = self.selected_sticker_revision(event, arguments['sticker_id']) if 'sticker_id' in arguments else None
                    file_selection = self.selected_files(event, arguments['file_ids']) if 'file_ids' in arguments else None
                text = arguments.get("text")
                require(bundled or isinstance(text, str) and 0 < len(text) <= 1000 and text.strip(), "qq_text_invalid")
                self.provider.identity()
                guard()
                self.participation.check(ticket, generation)
                action_id = row["id"]
                self.participation.reserve(ticket, generation, action_id)
                submitted = True
                reference = None
                if bundled:
                    result = self.provider.send_bundle_action(action_id, self.scope(event), arguments,
                        prepared_reply, sticker_revision=sticker_revision, guard=guard,
                        **({'file_selection': file_selection} if file_selection is not None else {}))
                    for part in result.receipt['parts']:
                        if part['status'] == 'confirmed':
                            for value in part['receipt'].values():
                                if value is not None:
                                    self.store.set_state(self.sent_key(event.visibility, self.scope(event).target_id, value), {'action': action_id})
                    self.participation.settle(action_id, 'uncertain' if result.status == 'uncertain' else
                        'succeeded' if result.receipt['confirmed_count'] else result.status)
                    self.tainted |= result.status == 'uncertain'
                    if prepared_reply:
                        result.receipt.update(format=prepared_reply['format'], fallback=prepared_reply['fallback'])
                    return result
                if prepared_reply is not None:
                    from .native_sender import NativeSendReceipt

                    receipt=self.provider.send_reply_action(action_id,self.scope(event),arguments,prepared_reply,guard=guard)
                    require(isinstance(receipt,NativeSendReceipt),'qq_send_unconfirmed')
                    identifier,reference=receipt.message_id,receipt.reference
                elif callable(getattr(self.provider, "send_action", None)):
                    from .native_sender import NativeSendReceipt

                    receipt = self.provider.send_action(action_id, self.scope(event), text)
                    require(isinstance(receipt, NativeSendReceipt), "qq_send_unconfirmed")
                    identifier, reference = receipt.message_id, receipt.reference
                else:
                    identifier = self.provider.send(self.scope(event), text)
                self.store.set_state(self.sent_key(event.visibility, self.scope(event).target_id, identifier), {"action": action_id})
                if reference is not None:
                    self.store.set_state(self.sent_key(event.visibility, self.scope(event).target_id, reference), {"action": action_id})
                self.participation.settle(action_id, "succeeded")
                return ActionResult("succeeded", {"message_id": identifier, "provider_accepted": True,
                    **({'format':prepared_reply['format'],'fallback':prepared_reply['fallback']} if prepared_reply else {})})
        except BaseException as exc:
            outcome = "uncertain" if submitted else "cancelled"
            if submitted:
                self.tainted = True
            if action_id:
                self.participation.settle(action_id, outcome)
            if not isinstance(exc, Exception):
                raise
            return ActionResult(outcome, error="qq_send_unconfirmed" if submitted else getattr(exc, "code", "qq_preflight_failed"))

    async def execute(self, capability, arguments, *, guard):
        async with self.send_lock:
            event = Event.from_dict(arguments["_event"])
            values = {k: v for k, v in arguments.items() if not k.startswith("_")}
            from threading import Event as CancelEvent

            cancelled = CancelEvent()
            def task_guard():
                if cancelled.is_set():
                    raise AssistantError('media_cancelled', '回复已取消。')
                guard()
            prepared_reply=None
            submit_guard=task_guard
            if capability=='message.reply' and 'format' in values:
                from .reply_material import prepare_reply
                from .voice_settings import VoiceSettings

                settings=VoiceSettings(self.store).read()
                def check():
                    task_guard()
                    self.validate_event(event)
                    require(VoiceSettings(self.store).read()['revision']==settings['revision'],'qq_voice_settings_changed')
                check()
                require(callable(getattr(self.provider,'send_reply_action',None)),'qq_voice_unavailable')
                work=asyncio.create_task(asyncio.to_thread(prepare_reply,{k:v for k,v in values.items() if k not in {'sticker_id', 'file_ids'}},voice_enabled=settings['enabled'],
                    max_audio_ms=settings['seconds']*1000,guard=check))
                try:
                    prepared_reply=await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled.set()
                    try:
                        await work
                    except AssistantError:
                        pass
                    raise
                check()
                submit_guard=check
            # Cancellation cannot abandon a background transport submit and falsely
            # report it unsent. Its bounded outcome must finish journaling.
            task = asyncio.create_task(asyncio.to_thread(self._submit, event, capability, values, submit_guard, prepared_reply))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                await task
                raise

    def runtime_interrupt(self, generation):
        if self.native_generation == generation and self.provider and hasattr(self.provider, "interrupt"):
            self.ready = False
            self.provider.interrupt()

    def runtime_stopped(self, generation):
        self.ready = False
        self.runtime_interrupt(generation)

    async def idle(self):
        await self.close()

    async def close(self):
        # Native submits are shielded and journalled before the bridge can exit.
        async with self.send_lock:
            await self._close()

    async def _close(self):
        self.ready = False
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.socket:
            await self.socket.close()
            self.socket = None
        if self.provider:
            self.provider.close()
            self.provider = None
        self.native_generation = None
        if self.bridge:
            await asyncio.to_thread(self.bridge.stop)

    def runtime_status(self):
        return {"state": "connected" if self.ready else (
                    self.bridge.state if self.bridge and self.options.get("launch") else
                    "retrying" if self.failures or self.code else "new") if self.provider else "paused",
                "code": self.code, "pending": len(self.store.state(self.queue_key, [])) + self.native_inbox.count(),
                "uncertain": self.tainted, "automatic_start": False}


def configured_module(store, project):
    return QQModule(store, project) if (Path(project) / ".dev/qq/module.json").exists() else None
