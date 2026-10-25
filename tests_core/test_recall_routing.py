"""Cached plans must never turn into cached facts or cross-scope evidence."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from companion_core.errors import AssistantError
from companion_core.recall_routing import CACHE_KEY, resolve
from companion_core.storage import Store

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "core")
    result.initialize()
    return result


def write(store, key, content):
    path = store.path("memories", key)
    version = store.read("memories", key)["version"] if path.exists() else None
    return store.write("memories", {
        "schema_version": 1, "id": key, "kind": "experience", "content": content,
        "tags": [], "confidence": "observed", "locked": False,
        "source": {"type": "conversation", "reference": "synthetic", "recorded_at": NOW.isoformat()},
    }, version, "test", key)


def configure(store, **changes):
    row = store.read("companion")
    row["data"]["algorithm_routing"]["recall"].update(changes)
    store.write("companion", row["data"], row["version"], "test")


async def expansion(store, query, generate, **kwargs):
    return await resolve(store, query, generate=generate, guard=kwargs.pop("guard", lambda: None),
                         clock=kwargs.pop("clock", lambda: NOW),
                         scope=kwargs.pop("scope", {"platform": "alpha", "account": "bot", "thread": "alice"}),
                         **kwargs)


@pytest.mark.asyncio
async def test_cache_reuses_expansion_across_restart_but_reads_current_yaml(store):
    write(store, "evidence", "alpha needle")
    calls = []

    async def generate(*args):
        calls.append(args)
        return {"queries": ["alpha", "gamma"]}

    first = await expansion(store, "某个旧约定", generate)
    assert first["route"] == "model"
    path = store.path("memories", "evidence")
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"alpha", b"gamma"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    restarted = Store(store.root)
    second = await expansion(restarted, "某个旧约定", generate, remaining=1)
    assert second["route"] == "cache" and len(calls) == 1
    assert restarted.search("某个旧约定", queries=second["queries"])[0]["data"]["content"] == "gamma needle"
    path.unlink()
    assert not restarted.search("某个旧约定", queries=second["queries"])


@pytest.mark.asyncio
async def test_scope_policy_and_ttl_are_part_of_cache_validity(store):
    calls = []

    async def generate(*args):
        calls.append(args)
        return {"queries": ["durable journal"]}

    for scope in ({"platform": "alpha", "account": "bot", "thread": "alice"},
                  {"platform": "beta", "account": "bot", "thread": "alice"},
                  {"platform": "alpha", "account": "other", "thread": "alice"},
                  {"platform": "alpha", "account": "bot", "thread": "bob"}):
        assert (await expansion(store, "如何恢复", generate, scope=scope))["route"] == "model"
    assert len(calls) == 4
    configure(store, cache_ttl_hours=1)
    assert (await expansion(store, "如何恢复", generate))["route"] == "model"
    assert (await expansion(store, "如何恢复", generate, clock=lambda: NOW + timedelta(hours=2)))["route"] == "model"
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_exact_ids_and_explicit_aliases_are_local_but_overlap_is_not_confidence(store):
    write(store, "m001", "source record")
    write(store, "distractor", "我在文件里改了记忆，为什么搜索还吐出旧内容？还没有查明具体办法。")
    calls = []

    async def generate(*args):
        calls.append(args)
        return {"queries": ["搜索索引刷新 缓存失效 变更检测"]}

    assert (await expansion(store, "m001", generate))["reason"] == "explicit_record_id"
    configure(store, aliases={"冷启动": ["cold start", "初始化"]})
    alias = await expansion(store, "冷启动", generate)
    assert alias["route"] == "algorithm" and alias["queries"] == ["cold start", "初始化"]
    for question in ("我在文件里改了记忆，为什么搜索还吐出旧内容？",
                     "不再喜欢汽车榜单了吗", "少量截图等于看完了吗"):
        assert (await expansion(store, question, generate))["route"] == "model"
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["model_failed", "model_timeout", "model_output_invalid"])
async def test_failure_is_not_cached_and_pause_is_not_swallowed(store, error):
    async def generate(*args):
        raise AssistantError(error, "failure")

    assert (await expansion(store, "旧资料", generate))["route"] == "fallback"
    assert not store.state(CACHE_KEY)

    def paused():
        raise AssistantError("not_running", "paused")

    with pytest.raises(AssistantError, match="paused"):
        await expansion(store, "旧资料", generate, guard=paused)


@pytest.mark.asyncio
async def test_cache_is_bounded_and_no_budget_still_supports_local_alias(store):
    configure(store, cache_entries=2, aliases={"显式简称": ["explicit alias"]})

    async def generate(*args):
        return {"queries": ["valid query"]}

    for i in range(4):
        await expansion(store, f"unknown {i}", generate, clock=lambda: NOW + timedelta(seconds=i))
    assert len(store.state(CACHE_KEY)) == 2
    assert (await expansion(store, "no budget", generate, remaining=1))["reason"] == "model_budget"
    assert (await expansion(store, "显式简称", generate, remaining=1))["reason"] == "configured_alias"


@pytest.mark.asyncio
async def test_edit_during_expansion_cannot_commit_cached_plan(store):
    async def generate(*args):
        configure(store, aliases={"new alias": ["new concept"]})
        return {"queries": ["old concept"]}

    with pytest.raises(AssistantError, match="配置"):
        await expansion(store, "unknown", generate)
    assert not store.state(CACHE_KEY)


@pytest.mark.asyncio
async def test_disabled_router_preserves_model_path_and_does_not_consume_cache(store):
    async def generate(*args):
        return {"queries": ["valid query"]}

    configure(store, enabled=False)
    write(store, "m001", "source")
    for _ in range(2):
        assert (await expansion(store, "m001", generate))["route"] == "model"
    assert not store.state(CACHE_KEY)


@pytest.mark.asyncio
async def test_new_exact_record_takes_precedence_over_old_expansion_plan(store):
    async def generate(*args):
        return {"queries": ["generic concept"]}

    assert (await expansion(store, "new-record", generate))["route"] == "model"
    write(store, "new-record", "newly supplied exact reference")
    result = await expansion(store, "new-record", generate)
    assert result["reason"] == "explicit_record_id"
    assert result["references"] == [{"kind": "memories", "id": "new-record"}]
