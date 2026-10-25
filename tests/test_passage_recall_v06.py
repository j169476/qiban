"""Long-source retrieval must expose actual spans without changing YAML authority."""
import json
import os
import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest
from test_cognition import material
from test_retrieval import add

from bili_assistant.cognition import packet, verify_versions
from bili_assistant.storage import Store
from companion_core.errors import AssistantError
from companion_core.passages import LONG_RECORD_CHARS, split, valid


def settings(store, **changes):
    record = store.read("companion")
    record["data"].update(changes)
    store.write("companion", record["data"], record["version"], "test")


@pytest.mark.parametrize(("query", "expansions", "target"), [
    ("玻璃退火 温度阈值", [], "玻璃退火温度阈值是 740 摄氏度；不适用于金属热处理，必须先确认材料种类。"),
    ("光学测量应避免哪类噪声", ["heterodyne shot noise"],
     "Heterodyne shot noise is not removed by averaging the reference drift; use independent calibration."),
])
def test_late_evidence_ranks_and_fits_context_with_real_source_offsets(store, memory, query, expansions, target):
    settings(store, context_char_limit=4000, context_record_limit=6)
    content = "背景介绍：这一节只描述一般记录流程，并没有目标实验结论。\n" * 350 + target + "\n后记。"
    source = add(store, memory, "long-target", content, date="2024-01-01T00:00:00Z")
    for i in range(45):
        add(store, memory, f"noise-{i}", "讨论测量温度、噪声、资料和实验结果，还没有完成任何独立校准。")
    found = store.search(query, limit=5, queries=expansions)
    assert found[0]["data"]["id"] == "long-target"
    assert found[0]["data"]["content"] == content  # Search still provides a canonical full record.
    assert found[0]["matched_excerpt"]["excerpt"]["ranges"][0][0] > 7000
    result = store.context(query, queries=expansions)
    assert result["context_chars"] <= 4000
    assert result["catalog_chars"] == len(json.dumps(result["catalog"], ensure_ascii=False))
    assert result["total_context_chars"] == len(json.dumps(result, ensure_ascii=False))
    selected = next(r for r in result["records"] if r["data"]["id"] == "long-target")
    assert target in selected["data"]["content"]
    assert selected["version"] == source["version"] == selected["excerpt"]["version"]
    assert selected["content_truncated"] and selected["excerpt"]["omitted_chars"] > 0
    assert not selected["excerpt"]["complete"]
    assert selected["excerpt"]["total_chars"] == len(content)
    for start, end in selected["excerpt"]["ranges"]:
        assert content[start:end] in selected["data"]["content"]
    store.profile.validate("memories", selected["data"])
    assert store.read("memories", "long-target") == source
    assert target in result["catalog"][0]["summary"]


def test_separate_dated_corrections_remain_explicit_excerpts_not_a_false_full_read(store, memory):
    settings(store, context_char_limit=10000)
    content = ("记录背景。" * 400 + "\n2021 年约定称呼为小青。\n" + "无关的器材清单。" * 500
               + "\n2026 年更正约定称呼为青禾，不再使用小青。\n" + "后记。" * 800)
    add(store, memory, "corrections", content)
    result = store.context("2021 2026 约定称呼 小青 青禾")
    selected = result["records"][0]
    assert "2021 年约定" in selected["data"]["content"]
    assert "2026 年更正" in selected["data"]["content"]
    assert len(selected["excerpt"]["ranges"]) >= 2
    assert "[…未提供的原文…]" in selected["data"]["content"]
    assert selected["content_truncated"]


@pytest.mark.parametrize("position", ["middle", "tail"])
def test_rare_local_fact_is_not_penalized_by_unrelated_long_background(store, memory, position):
    settings(store, context_char_limit=4000)
    fact = "水准测量的高程基准只能取稳定岩层上的已知高程点，不能用临时施工架。"
    background = "这部分罗列通用目录、历史背景、资料整理和未开展的日常讨论。\n"
    content = background * 220 + fact + "\n" + (background * 220 if position == "middle" else "")
    add(store, memory, "level-survey", content, date="2024-01-01T00:00:00Z")
    for i in range(35):
        add(store, memory, f"brief-{i}", "讨论具体方法、实际操作和资料里的技术问题，没有形成结论。")
    for i in range(200):
        add(store, memory, f"unrelated-{i}", "午后的散步路线经过树木和草坪。")
    query = "水准测量 高程基准 具体 方法 实际 操作 资料 技术 问题"
    result = store.search(query, limit=5)
    assert "level-survey" in {r["data"]["id"] for r in result}
    chosen = next(r for r in store.context(query)["records"] if r["data"]["id"] == "level-survey")
    assert fact in chosen["data"]["content"]


def test_passage_cache_reuses_restart_and_detects_same_size_same_mtime_edit(store, memory):
    content = "一般背景\n" * 800 + " alpha-needle 仅供本条条件使用。"
    add(store, memory, "entry", content)
    assert store.search("alpha needle")[0]["data"]["id"] == "entry"
    reopened = Store(store.root)
    assert reopened.search("alpha needle")[0]["data"]["id"] == "entry"
    assert reopened.index_stats["passage_hits"] == 1
    assert reopened.index_stats.get("passage_builds", 0) == 0
    path = store.path("memories", "entry")
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"alpha-needle", b"gamma-needle"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    updated = Store(store.root)
    assert not updated.search("alpha")
    assert "gamma-needle" in updated.context("gamma needle")["records"][0]["data"]["content"]
    assert updated.index_stats["passage_builds"] == 1
    path.unlink()
    assert not Store(store.root).search("gamma")
    with closing(sqlite3.connect(store.root / "cache/parsed.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM passages WHERE id='entry'").fetchone()[0] == 0


def test_corrupt_passage_cache_rebuilds_and_invalid_yaml_never_survives_as_excerpt(store, memory):
    content = "前文资料\n" * 800 + " 锆石年龄校准结论。"
    add(store, memory, "entry", content)
    store.context("锆石")
    with closing(sqlite3.connect(store.root / "cache/parsed.sqlite3")) as db, db:
        db.execute("UPDATE passages SET spans=? WHERE id='entry'", (json.dumps([[0, 999999]]),))
    reopened = Store(store.root)
    result = reopened.context("锆石")
    assert "锆石年龄" in result["records"][0]["data"]["content"]
    assert reopened.index_stats["passage_builds"] == 1
    store.path("memories", "entry").write_text("bad: [", encoding="utf-8")
    assert not Store(store.root).context("锆石")["records"]


def test_visibility_filters_before_fragment_ranking_and_fallback_catalog(store, memory):
    settings(store, context_record_limit=6)
    for i in range(5):
        add(store, memory, f"hidden-{i}", "私密资料\n" * 800 + " 罕见专名冷光转换结论。")
    add(store, memory, "visible", "普通资料\n" * 800 + " 公开可读的冷光转换资料。")
    visible = store.context("罕见专名 冷光转换", record_filter=lambda r: r["data"]["id"] == "visible")
    assert visible["total_records"] == 1
    assert {r["data"]["id"] for r in visible["records"]} == {"visible"}
    assert {r["id"] for r in visible["catalog"]} == {"visible"}


def test_excerpt_keeps_full_source_version_for_model_guard(role, memory):
    settings(role.store, context_char_limit=4000)
    source = add(role.store, memory, "long-note", "背景资料\n" * 800 + " 单光子干涉实验限制。")
    video = material()
    video["title"] = "单光子干涉"
    result, dependencies = packet(role, {"events": [], "video": video})
    selected = next(r for r in result["memory"]["records"] if r["data"]["id"] == "long-note")
    assert selected["content_truncated"]
    assert ("memories", "long-note", source["version"]) in dependencies
    data = deepcopy(source["data"])
    data["content"] = data["content"].replace("背景资料", "历史资料", 1)  # Even an omitted character matters.
    role.store.write("memories", data, source["version"], "user", "long-note")
    with pytest.raises(AssistantError, match="变化"):
        verify_versions(role.store, dependencies)


@pytest.mark.parametrize("prefix", [580, 1000, 1160, 2200])
def test_boundary_overlap_keeps_short_negative_conditions(prefix):
    clause = "针对绿辉石荧光测定，不适用于透辉石样品，必须先完成矿物鉴定。"
    content = "背景。" * (prefix // 3) + clause + "\n" + "附录。" * 700
    spans = split(content)
    assert len(content) > LONG_RECORD_CHARS and valid(spans, len(content))
    assert any(clause in content[start:end] for start, end in spans)


def test_unicode_offsets_and_complete_short_records_are_preserved(store, memory):
    settings(store, context_char_limit=4000)
    content = "🦎背景资料。\n" * 400 + " 尾段限定只适用于冰晶，不能用于液滴。"
    add(store, memory, "unicode", content)
    result = store.context("冰晶 液滴")
    selected = result["records"][0]
    for start, end in selected["excerpt"]["ranges"]:
        assert content[start:end] in selected["data"]["content"]
    short = add(store, memory, "short", "精确的短记录保持完整。")
    selected = next(r for r in store.context("精确 短记录")["records"] if r["data"]["id"] == "short")
    assert selected["data"] == short["data"] and "excerpt" not in selected


def test_casefold_expansion_does_not_shift_catalog_offsets(store, memory):
    content = "ß " * 400 + "optical heterodyne has a local boundary condition. " + "附录内容。" * 300
    add(store, memory, "casefold", content)
    found = store.search("optical heterodyne")[0]["matched_excerpt"]
    assert "optical heterodyne" in found["summary"]
    start, end = found["excerpt"]["ranges"][0]
    assert found["summary"] == content[start:end]
