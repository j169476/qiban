import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from companion_core import events
from companion_core.backups import create, restore
from companion_core.contracts import ActionResult, ActorRef, Capability, Event, PlatformRegistry
from companion_core.errors import AssistantError
from companion_core.identity import IdentityService
from companion_core.people import bind_relationship, profile
from companion_core.runtime import Companion
from companion_core.schema import obj
from companion_core.storage import Store

NOW = "2026-10-22T04:00:00+00:00"


class Host:
    archived = False

    def identity(self):
        return "role"

    def require_active(self, thread_id=None):
        if self.archived:
            raise AssistantError("chat_archived", "archived")


class Adapter:
    def __init__(self, platform="alpha", account="bot"):
        self.platform, self.account = platform, account
        self.sent, self.incoming = [], []
        self.failure = False
        self.before = lambda: None

    def capabilities(self):
        return [Capability("message.reply", "reply", obj({"text": {"type": "string"}}))]

    async def receive(self, cursor):
        return self.incoming, {"next": 1}

    async def read(self, reference):
        return {"reference": reference, "segments": [{"text": "material"}], "coverage": {"complete": False}}

    async def execute(self, capability, arguments, *, guard):
        self.before()
        guard()
        self.sent.append(arguments)
        if self.failure:
            raise TimeoutError()
        return ActionResult("succeeded", {"id": "receipt"})


class Model:
    def __init__(self):
        self.calls = []
        self.before = lambda: None
        self.proposals = []
        self.memories = []

    async def generate(self, instructions, context, output_schema, images=(), **options):
        self.before()
        self.calls.append(context)
        if "只评估跨平台身份" in instructions:
            return {"identity_proposals": self.proposals}
        return {"note": "", "action": {"capability": "message.reply", "arguments": {"text": "收到"}},
                "memories": self.memories, "identity_proposals": []}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "core")
    assert result.initialize()["ok"]
    return result


def event(platform="alpha", uid="100", text="我的另一平台账号是 7654321，昨天我们讨论过蓝色桌面的草图。", native="one", account="bot", conversation="dm"):
    return Event(platform, account, native, ActorRef(platform, uid), conversation, NOW, text)


def reference(store, item, quote=None):
    receipt = events.ingest(store, item)
    return {"event_id": item.id, "version": receipt["version"], "quote": quote or item.text}


def proposal(left, right, proof, kind="claim", specific=False):
    return {"left": left.as_dict(), "right": right.as_dict(), "kind": kind,
            "reason": "双方提及昨日约定的蓝色桌面草图", "specific_correspondence": specific, "evidence": proof}


def suspected(store):
    first = event()
    second = event("beta", "7654321", "我是昨天和你讨论蓝色桌面草图的那个人。")
    service = IdentityService(store)
    a = reference(store, first)
    link = service.apply(proposal(first.actor, second.actor, [a]))
    return service, first, second, link, a


def test_core_import_graph_has_no_platform_or_host_dependency():
    root = Path(__file__).resolve().parents[1] / "src/companion_core"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(m.startswith(("bili_assistant", "bilibili_api", "companion_codex")) for m in modules), path


def test_actor_event_and_conversation_ids_are_scoped():
    first = event()
    other = event("beta")
    third = event(account="other-bot")
    assert first.actor.key != other.actor.key
    assert len({e.id for e in (first, other, third)}) == 3
    assert len({e.thread for e in (first, other, third)}) == 3
    assert Event.from_dict(first.as_dict()) == first


def test_self_claim_is_only_suspected_and_replay_is_idempotent(store):
    service, first, second, old, proof = suspected(store)
    assert old["data"]["status"] == "suspected"
    assert len(service.person(first.actor)["members"]) == 1
    again = service.apply(proposal(first.actor, second.actor, [proof]))
    assert again["version"] == old["version"]


def test_independent_correspondence_forms_reversible_hard_link(store):
    service, first, second, old, a = suspected(store)
    b = reference(store, second)
    hard = service.apply(proposal(first.actor, second.actor, [a, b], "correspondence", True))
    assert hard["data"]["status"] == "hard"
    assert len(service.person(first.actor)["members"]) == 2
    service.revoke(old["data"]["id"], hard["version"], "人工确认不是同一个人")
    assert len(service.person(first.actor)["members"]) == 1
    with pytest.raises(AssistantError, match="裁决"):
        service.apply(proposal(first.actor, second.actor, [a, b], "correspondence", True))
    assert events.get(store, second.id)["event"]["text"] == second.text


@pytest.mark.parametrize("flaw", ["single_platform", "generic_similarity", "invented_quote", "unprovided", "wrong_actor", "partial_number"])
def test_insufficient_or_forged_identity_evidence_never_promotes(store, flaw):
    service, first, second, _, a = suspected(store)
    b = reference(store, second)
    data = proposal(first.actor, second.actor, [a, b], "correspondence", True)
    allowed = None
    if flaw == "single_platform":
        data["evidence"] = [a]
    elif flaw == "generic_similarity":
        data["specific_correspondence"] = False
    elif flaw == "invented_quote":
        data["evidence"][1] = {**b, "quote": "原始消息中没有这句话"}
    elif flaw == "unprovided":
        allowed = {first.id}
    elif flaw == "wrong_actor":
        data["evidence"][1] = reference(store, event("beta", "stranger"))
    elif flaw == "partial_number":
        data = proposal(first.actor, ActorRef("beta", "765432"), [a])
    with pytest.raises(AssistantError):
        service.apply(data, allowed_evidence=allowed)
    assert all(r["effective_status"] != "hard" for r in service.links())


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_source_edit_or_delete_invalidates_hard_link_in_database(store, change):
    service, first, second, old, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    if change == "edit":
        events.ingest(store, event("beta", "7654321", "之前的话有误，我并不是那个人。"))
    else:
        events.forget(store, second.id)
    assert service.links()[0]["effective_status"] == "disputed"
    assert not service.person(first.actor)["inferred"]
    with store.db() as db:
        assert db.execute("SELECT status FROM core_identity_links WHERE id=?", (old["data"]["id"],)).fetchone()[0] == "disputed"


def test_person_view_preserves_scores_and_local_edits(store):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    for actor, score in ((first.actor, 60), (second.actor, 52)):
        store.write("relationships", {"schema_version": 1, "id": actor.key, "display_name": actor.platform,
                    "affinity": score, "interactions": 1, "events": [], "locked": False,
                    "impression": "喜欢讨论设计", "facts": []}, None, "test", actor.key)
        bind_relationship(store, actor, actor.key)
    assert sorted(i["affinity"] for i in profile(store, first.actor)["impressions"]) == [52, 60]
    path = store.path("relationships", second.actor.key)
    path.write_text(path.read_text(encoding="utf-8").replace("52", "55"), encoding="utf-8")
    assert sorted(i["affinity"] for i in profile(store, first.actor)["impressions"]) == [55, 60]


@pytest.mark.asyncio
async def test_two_platform_decisions_share_core_without_mixing_threads_or_actions(store):
    model, host = Model(), Host()
    alpha, beta = Adapter(), Adapter("beta")
    core = Companion(store, model, host, [alpha, beta], clock=lambda: datetime.fromisoformat(NOW))
    assert store.state("core-run") is None
    with pytest.raises(AssistantError):
        await core.handle(event())
    core.start()
    model.memories = ["只属于甲平台会话的经历"]
    assert (await core.handle(event()))["action"]["status"] == "succeeded"
    model.memories = []
    assert (await core.handle(event("beta")))["action"]["status"] == "succeeded"
    assert (await core.handle(event()))["duplicate"]
    assert len(alpha.sent) == len(beta.sent) == 1
    assert model.calls[-1]["memory"]["records"] == []
    assert all(turn["data"]["channel"] == "beta" for turn in model.calls[-1]["conversation"]["turns"])


@pytest.mark.asyncio
async def test_uncertain_action_is_not_resent_after_restart(store):
    adapter, host = Adapter(), Host()
    adapter.failure = True
    core = Companion(store, Model(), host, [adapter], clock=lambda: datetime.fromisoformat(NOW))
    core.start()
    assert (await core.handle(event()))["action"]["status"] == "uncertain"
    core.stop()
    restored = Companion(Store(store.root), Model(), host, [adapter])
    restored.start()
    assert (await restored.handle(event()))["duplicate"]
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_archive_before_dispatch_cancels_all_platforms(store):
    host, adapter = Host(), Adapter()
    core = Companion(store, Model(), host, [adapter])
    core.start()
    adapter.before = lambda: setattr(host, "archived", True)
    result = await core.dispatch(event(), {"capability": "message.reply", "arguments": {"text": "test"}})
    assert result["status"] == "cancelled"
    assert not adapter.sent
    assert store.state("core-run")["status"] == "paused"


@pytest.mark.asyncio
async def test_queued_event_survives_poll_consumer_interruption(store):
    adapter, host = Adapter(), Host()
    adapter.incoming = [event()]
    core = Companion(store, Model(), host, [adapter], clock=lambda: datetime.fromisoformat(NOW))
    core.start()
    assert len(await core.poll()) == 1
    adapter.incoming = []
    assert len(await core.poll()) == 1
    await core.handle((await core.poll())[0])
    assert await core.poll() == []


@pytest.mark.asyncio
async def test_model_context_change_prevents_writes_and_send(store):
    model, adapter = Model(), Adapter()
    core = Companion(store, model, Host(), [adapter])
    core.start()
    def change():
        old = store.read("persona")
        old["data"]["name"] = "changed"
        store.write("persona", old["data"], old["version"], "user")
    model.before = change
    with pytest.raises(AssistantError, match="配置"):
        await core.handle(event())
    assert not adapter.sent


@pytest.mark.asyncio
async def test_identity_model_review_is_automatic_but_private_evidence_is_not_reply_context(store):
    service, first, second, _, a = suspected(store)
    b = reference(store, second)
    model = Model()
    model.proposals = [proposal(first.actor, second.actor, [a, b], "correspondence", True)]
    core = Companion(store, model, Host(), [Adapter("beta")])
    core.start()
    await core.handle(second)
    assert service.links()[0]["effective_status"] == "hard"
    assert model.calls[-1]["identity"]["person"]["inferred"]
    assert first.text not in json.dumps(model.calls[-1], ensure_ascii=False)


def test_full_backup_restores_identity_and_stays_paused(store, tmp_path):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    core = Companion(store, Model(), Host(), [Adapter()])
    core.start()
    target = tmp_path / "core.full.zip"
    create(store, target)
    restore(target, tmp_path / "restored")
    recovered = Store(tmp_path / "restored")
    assert recovered.state("core-run")["status"] == "paused"
    assert IdentityService(recovered).person(first.actor)["inferred"]


def test_duplicate_connector_rejected():
    with pytest.raises(AssistantError):
        PlatformRegistry([Adapter(), Adapter()])


def test_deleted_evidence_cannot_be_resurrected_by_connector_replay(store):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    events.forget(store, second.id)
    assert events.ingest(store, second)["deleted"]
    assert service.links()[0]["effective_status"] == "disputed"


def test_revoked_direct_link_cannot_be_bypassed_through_a_third_platform(store):
    service, first, second, _, a = suspected(store)
    b = reference(store, second)
    original = service.apply(proposal(first.actor, second.actor, [a, b], "correspondence", True))
    third = event("gamma", "7654321", "我的另一账号是 7654321，昨天讨论过蓝色桌面草图。")
    c = reference(store, third)
    for left, right, proofs in ((first.actor, third.actor, [a, c]), (third.actor, second.actor, [c, b])):
        service.apply(proposal(left, right, [proofs[0]]))
        service.apply(proposal(left, right, proofs, "correspondence", True))
    assert len(service.person(first.actor)["members"]) == 3
    service.revoke(original["data"]["id"], original["version"], "两人身份不同")
    assert not service.person(first.actor)["inferred"]


@pytest.mark.asyncio
async def test_hard_identity_is_reviewed_when_later_dialogue_contradicts_it(store):
    service, first, second, _, a = suspected(store)
    service.apply(proposal(first.actor, second.actor, [a, reference(store, second)], "correspondence", True))
    correction = event("beta", "7654321", "此前说错了，我并不是昨天和你讨论蓝色桌面的那个人。", native="two")
    ref = reference(store, correction)
    model = Model()
    model.proposals = [proposal(first.actor, second.actor, [ref], "contradiction")]
    core = Companion(store, model, Host(), [Adapter("beta")])
    core.start()
    await core.handle(correction)
    assert service.links()[0]["effective_status"] == "disputed"
    assert not service.person(first.actor)["inferred"]


@pytest.mark.asyncio
async def test_new_connector_requires_explicit_run_reconfiguration(store):
    core = Companion(store, Model(), Host(), [Adapter()])
    core.start()
    other = Adapter("beta")
    core.platforms.register(other)
    with pytest.raises(AssistantError, match="不属于"):
        await core.dispatch(event("beta"), {"capability": "message.reply", "arguments": {"text": "test"}})
    assert not other.sent


@pytest.mark.asyncio
async def test_action_quotas_are_isolated_per_platform_account(store):
    policy = store.read("policy")
    policy["data"]["daily_action_limit"] = 1
    store.write("policy", policy["data"], policy["version"], "test")
    first, second = Adapter(), Adapter("beta")
    core = Companion(store, Model(), Host(), [first, second])
    core.start()
    for platform in ("alpha", "beta"):
        await core.handle(event(platform))
    with pytest.raises(AssistantError, match="额度"):
        await core.handle(event(native="two"))
    assert len(first.sent) == len(second.sent) == 1


@pytest.mark.asyncio
async def test_invalid_model_identity_citation_does_not_break_conversation(store):
    service, first, second, _, a = suspected(store)
    b = reference(store, second)
    model, adapter = Model(), Adapter("beta")
    wrong = proposal(first.actor, second.actor, [a, {**b, "quote": "这是模型改写后的文字，并不在原始材料里"}], "correspondence", True)
    model.proposals = [wrong]
    core = Companion(store, model, Host(), [adapter])
    core.start()
    result = await core.handle(second)
    assert result["action"]["status"] == "succeeded"
    assert service.links()[0]["effective_status"] == "suspected"
    assert store.state("identity-rejections")[-1]["error"] == "identity_evidence_changed"
