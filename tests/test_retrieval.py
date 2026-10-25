import os
from copy import deepcopy

import pytest

from bili_assistant.api import call
from bili_assistant.conversations import append, context_for_events
from bili_assistant.storage import Store


def add(store, memory, item_id, content, kind="experience", date="2026-01-01T00:00:00Z"):
    doc = deepcopy(memory)
    doc.update(id=item_id, content=content, kind=kind)
    doc["source"]["recorded_at"] = date
    return store.write("memories", doc, None, "test", item_id)


def test_rare_evidence_ranks_above_recent_generic_overlap(store, memory):
    add(store, memory, "old", "发布时建立 Git 标签以便回滚。", date="2024-01-01T00:00:00Z")
    for i in range(150):
        add(store, memory, f"recent-{i}", "发布时发生问题，需要检查发布状态，讨论回去怎么解决。")
    assert store.search("发布时出问题 Git 标签回滚")[0]["data"]["id"] == "old"


@pytest.mark.parametrize("expanded", [False, True])
def test_hash_cache_detects_same_size_same_mtime_edit_and_invalid_delete(store, memory, expanded):
    def search(term):
        return store.search("unrelated" if expanded else term, queries=[term] if expanded else [])

    add(store, memory, "entry", "alpha example")
    assert search("alpha")
    path = store.path("memories", "entry")
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"alpha", b"gamma"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert not search("alpha")
    assert search("gamma")[0]["data"]["content"] == "gamma example"
    path.write_text("bad: [", encoding="utf-8")
    assert not search("gamma")
    path.unlink()
    assert not search("gamma")


def test_cached_read_isolation_and_restart(store, memory):
    add(store, memory, "entry", "canonical data")
    returned = store.read("memories", "entry")
    returned["data"]["content"] = "unsaved mutation"
    assert store.read("memories", "entry")["data"]["content"] == "canonical data"
    assert Store(store.root).search("canonical")[0]["data"]["id"] == "entry"


def test_many_preferences_do_not_hide_relevant_old_context(store, memory):
    for i in range(60):
        add(store, memory, f"preference-{i}", f"日常偏好 {i}", kind="user_preference")
    add(store, memory, "old-method", "独特的玻璃退火温度曲线记录", date="2024-01-01T00:00:00Z")
    result = store.context("玻璃退火温度")
    assert any(r["data"]["id"] == "old-method" for r in result["records"])
    assert any(r["id"] == "old-method" for r in result["catalog"])
    assert len(result["records"]) <= store.read("companion")["data"]["context_record_limit"]


def test_english_tokens_do_not_match_substrings(store, memory):
    add(store, memory, "noise", "case task database")
    assert not store.search("as")


@pytest.mark.asyncio
async def test_frontend_search_accepts_same_expansions_and_relationship_scope_as_background(role, memory):
    add(role.store, memory, "audio", "Sparse frame sampling cannot establish continuous audiovisual coverage.")
    role.relationship("789", "同名用户", 0, "one", facts=["喜欢现场演奏"])
    role.relationship("790", "同名用户", 0, "two", facts=["喜欢安静陶艺"])
    args = {"query": "少量截图等于看完了吗", "queries": ["sparse frame sampling coverage"], "uids": ["789"]}
    actual = await call(role, "memory_search", args)
    expected = role.store.search(args["query"], queries=args["queries"], uids=args["uids"])
    assert [r["data"]["id"] for r in actual] == [r["data"]["id"] for r in expected]
    assert any(r["data"]["id"] == "audio" for r in actual)
    scoped = await call(role, "memory_search", {"query": "同名用户", "uids": ["789"]})
    assert [r["data"]["id"] for r in scoped if r["kind"] == "relationships"] == ["789"]
    context = await call(role, "memory_context", {"queries": ["同名用户"], "uids": ["789"]})
    assert [r["data"]["id"] for r in context["records"] if r["kind"] == "relationships"] == ["789"]


@pytest.mark.parametrize(("query", "concept", "content"), [
    ("我在文件里改了记忆，为什么搜索还吐出旧内容？", "搜索索引刷新 缓存失效 变更检测",
     "外部编辑 YAML 后必须重新计算内容摘要。检索缓存只是派生数据，原文件被删除或改坏时旧条目应失效。"),
    ("好感已经入账，文件还没写完就断电，又恰好被我改过，如何恢复？",
     "durable transaction journal pending projection concurrent user edits reconciliation",
     "A durable transaction journal commits the receipt before projecting editable YAML. After a crash, replay "
     "the pending projection; preserve concurrent user edits for explicit reconciliation."),
])
def test_complete_concepts_recall_old_notes_among_recent_paraphrases(store, memory, query, concept, content):
    add(store, memory, "target", content, date="2024-01-01T00:00:00Z")
    for index in range(50):
        add(store, memory, f"noise-{index}", query + " 还没有查明具体办法。")
    queries = [query + " 搜索结果", concept]
    assert "target" in [r["data"]["id"] for r in store.search(query, limit=5, queries=queries)]
    settings = store.read("companion")
    settings["data"].update(context_record_limit=6, reflection_min_memories=6, catalog_limit=10)
    store.write("companion", settings["data"], settings["version"], "test")
    context = store.context(query, queries=queries)
    assert "target" in [r["data"]["id"] for r in context["records"]]
    assert "target" in [r["id"] for r in context["catalog"][:5]]


def test_duplicate_and_empty_queries_do_not_vote_again_or_reorder_single_query(store, memory):
    add(store, memory, "a", "ceramic garden garden")
    add(store, memory, "b", "ceramic astronomy")
    add(store, memory, "c", "astronomy orbit")
    def ids(results):
        return [r["data"]["id"] for r in results]
    assert ids(store.search("ceramic garden")) == ids(store.search(
        "ceramic garden", queries=["GARDEN ceramic", "ceramic garden", "", "!!!"]))
    assert ids(store.search("ceramic garden", queries=["orbit"])) == ids(store.search(
        "ceramic garden", queries=["orbit", "ORBIT", "garden ceramic", ""]))
    assert not store.search("unmatched", queries=["", "stillunmatched"])


def test_later_query_has_own_term_budget_and_expansion_only_catalog_is_ranked(store, memory):
    add(store, memory, "target", "zygomatic fracture", date="2024-01-01T00:00:00Z")
    for index in range(12):
        add(store, memory, f"recent-{index}", "unrelated recent note")
    query = " ".join(f"a{i:03}" for i in range(250))
    assert store.search(query, queries=["zygomatic fracture"])[0]["data"]["id"] == "target"
    assert store.context(queries=["zygomatic fracture"])["catalog"][0]["id"] == "target"


def test_cross_user_and_thread_corrections_remain_attributed_and_ordered(store):
    append(store, "123", "dm", "789", "old", "我喜欢汽车测评。", "user", "789", "received",
           "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    append(store, "123", "dm", "789", "new", "纠正一下，我只爱看拆解，不想看购车榜单。", "user", "789", "received",
           "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z")
    for index in range(30):
        append(store, "123", "dm", str(800 + index), f"other-{index}", "我很喜欢购车榜单。",
               "user", str(800 + index), "received", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z")
    append(store, "999", "dm", "789", "another-account", "另一账号的消息。", "user", "789", "received",
           "2026-01-04T00:00:00Z", "2026-01-04T00:00:00Z")
    result = context_for_events(store, "123", [{"kind": "dm_reply", "uid": "789", "id": "new"}])
    turns = result[0]["turns"]
    assert [r["data"]["message_id"] for r in turns] == ["old", "new"]
    assert all(r["data"]["sender_uid"] == "789" for r in turns)
    assert "纠正" in turns[-1]["data"]["content"]
    changed = turns[-1]
    changed["data"]["content"] = "更正：现在只想看陶艺。"
    store.write("conversation_turns", changed["data"], changed["version"], "user", changed["data"]["id"])
    reopened = Store(store.root)
    again = context_for_events(reopened, "123", [{"kind": "dm_reply", "uid": "789", "id": "new"}])
    assert again[0]["turns"][-1]["data"]["content"] == "更正：现在只想看陶艺。"
    store.path("conversation_turns", changed["data"]["id"]).unlink()
    assert len(context_for_events(reopened, "123", [{"kind": "dm_reply", "uid": "789", "id": "new"}])[0]["turns"]) == 1
