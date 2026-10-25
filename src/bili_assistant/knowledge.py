"""Versioned source review and method evidence; judgments never replace source notes."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from companion_core.knowledge import annotations as annotations
from companion_core.knowledge import latest_reviews as latest_reviews
from companion_core.knowledge import ref as ref
from companion_core.knowledge import related_records as related_records
from companion_core.knowledge import review_job as review_job
from companion_core.knowledge import review_usage as review_usage
from companion_core.knowledge import same_versions as same_versions
from companion_core.knowledge import status as status
from companion_core.knowledge import trial_status as trial_status
from companion_core.knowledge import valid_review as valid_review
from companion_core.knowledge import validate_quotes as validate_quotes

from .cognition import BOUNDARIES, stable_id, verify_versions
from .errors import AssistantError
from .schema import REVIEW_RESULT
from .storage import recorded_time, search_terms

REVIEW_PROMPT = BOUNDARIES + (
    "复查 target 知识笔记，与 related 比较。笔记是转述，sources 才是实际取得的材料。"
    "区分事实、观点、推断、方法，列出适用条件。supported_by_material 仅表示材料支持，"
    "不代表已证明客观真理；不能仅凭模型常识或多个视频重复说法裁决。"
    "每个有支持或纠错结论必须引用 sources.segments 中逐字存在的短句和 source_id。"
    "画面记录没有可核对文字时用 needs_evidence，不能编造字幕或引用。"
    "区分同义、条件不同、时间变化、真正矛盾和无关，逐一比较给出的 related。"
    "prefer_target/prefer_other/both_conditional 必须引用双方实际材料，解释条件、时间或证据差异；"
    "材料不够则 unresolved。保留反例，不能把知识笔记当成独立的第二份证据。"
    "sources 可能为摘取片段，检查 omitted_segments 和 coverage，不能把未覆盖部分当成已核实。"
    "method_trials 是方法推演或用户反馈，未独立实测；失败时检查原方法的适用条件，"
    "不能把个别失败报告直接当成所有场景都无效。target_truncated=true 时整条评估必须 needs_evidence。"
    "summary 写明材料内可支持的范围；follow_up 列出可检索的具体未决问题。"
)






def page_index(url):
    try:
        return max(0, int(parse_qs(urlsplit(url).query).get("p", ["1"])[0]) - 1)
    except (TypeError, ValueError):
        return 0


def snapshot(engine, video):
    """Capture actual retrieved text, not an LLM-written paraphrase or title-only claim."""
    if video["coverage"]["kind"] == "metadata_only":
        return None
    store = engine.store
    payload = {
        "account_uid": store.read("manifest")["data"]["account_uid"], "bvid": video["bvid"],
        "url": video["url"], "title": video["title"], "owner_uid": video.get("owner_uid", ""),
        "coverage": video["coverage"], "segments": video.get("segments", []),
        "visual_note": video.get("visual_note", "") if video.get("images") else "",
        "visual_samples": [{"kind": image.get("kind", "image"),
                            "timestamps_seconds": image.get("timestamps_seconds", [])[:100]}
                           for image in video.get("images", [])[:4]],
    }
    scope = getattr(store, "scope_key", "")
    item_id = stable_id("source", json.dumps(payload, ensure_ascii=False, sort_keys=True) + (":" + scope if scope else ""))
    try:
        return store.read("source_snapshots", item_id)
    except AssistantError as exc:
        if exc.code != "not_found":
            raise
    return store.write("source_snapshots", {
        "schema_version": 1, "id": item_id, **payload,
        "source": {"type": "video", "recorded_at": engine.clock().isoformat(), "reference": video["url"]},
        "locked": False,
    }, None, engine.identity(), item_id)


def source_for(store, knowledge, account):
    doc = knowledge["data"]
    if doc.get("source_ref"):
        try:
            source = store.read("source_snapshots", doc["source_ref"]["id"])
            if source["data"]["account_uid"] == account and source["version"] == doc["source_ref"]["version"]:
                return source
        except AssistantError:
            pass
        return None
    choices = [
        r for r in store.records("source_snapshots")[0]
        if r["data"]["account_uid"] == account and r["data"]["bvid"] == doc["coverage"]["bvid"]
        and r["data"]["coverage"]["kind"] != "web"
        and page_index(r["data"]["url"]) == page_index(doc["source"]["reference"])
    ]
    return max(choices, key=lambda r: recorded_time(r["data"]), default=None)


















def source_excerpt(source, query, budget):
    data = source["data"]
    terms = search_terms(query)
    ranked = sorted(enumerate(data["segments"]),
                    key=lambda pair: (-sum(t in pair[1]["text"].casefold() for t in terms), pair[0]))
    selected, size, truncated = [], 0, 0
    for index, segment in ranked:
        length = len(json.dumps(segment, ensure_ascii=False))
        if size + length <= budget:
            selected.append((index, segment))
            size += length
        elif not selected and budget >= 200:
            # Web pages can arrive as one long paragraph. Supply a real bounded substring,
            # not an empty source just because the original segment exceeds this cycle's budget.
            text = segment["text"]
            positions = [text.casefold().find(term) for term in terms if term in text.casefold()]
            start = max(0, min(positions, default=0) - 80)
            part = {**segment, "text": text[start:start + max(1, budget - 120)]}
            while len(json.dumps(part, ensure_ascii=False)) > budget:
                part["text"] = part["text"][:len(part["text"]) // 2]
            selected.append((index, part))
            size += len(json.dumps(part, ensure_ascii=False))
            truncated += 1
    return {
        "id": data["id"], "version": source["version"], "bvid": data["bvid"], "url": data["url"], "coverage": data["coverage"],
        "segments": [s for _, s in sorted(selected)],
        "omitted_segments": len(data["segments"]) - len(selected),
        "truncated_segments": truncated,
        "visual_note": data["visual_note"][:1000],
    }




def matching_sources(note, provided):
    if note.get("source_ref"):
        reference = note["source_ref"]
        return {s["id"] for s in provided.values()
                if s["id"] == reference["id"] and s.get("version") == reference["version"]}
    return {s["id"] for s in provided.values() if s["bvid"] and s["bvid"] == note["coverage"]["bvid"]
            and page_index(s["url"]) == page_index(note["source"]["reference"])}


def save_review(engine, job, sources, payload, result):
    store = engine.store
    with store.lock:
        verify_versions(store, [
            ("knowledge", r["data"]["id"], r["version"]) for r in [job["target"], *job["related"]]
        ] + [("source_snapshots", r["data"]["id"], r["version"]) for r in sources]
          + [("method_trials", r["data"]["id"], r["version"]) for r in job.get("method_trials", [])])
        if payload.get("target_truncated") and result["assessment"] != "needs_evidence":
            raise AssistantError("review_incomplete", "只看过部分笔记时不能裁定整条知识")
        provided = {r["id"]: r for r in payload["sources"]}
        target_sources = matching_sources(job["target"]["data"], provided)
        for claim in result["claims"]:
            cited = validate_quotes(claim["evidence"], provided)
            if claim["assessment"] != "needs_evidence" and not cited & target_sources:
                raise AssistantError("ungrounded_review", "支持或纠错结论缺少该笔记实际来源的引用")
        if result["assessment"] == "supported_by_material" and any(
            c["assessment"] != "supported_by_material" for c in result["claims"]
        ):
            raise AssistantError("review_inconsistent", "仍有未决主张时不能将整条知识标为材料支持")
        related = {r["data"]["id"]: r for r in job["related"]}
        ids = [c["other_id"] for c in result["comparisons"]]
        if set(ids) != set(related) or len(ids) != len(set(ids)):
            raise AssistantError("ungrounded_review", "知识比较必须完整对应本轮相关条目")
        for comparison in result["comparisons"]:
            cited = validate_quotes(comparison["evidence"], provided)
            other = related[comparison["other_id"]]["data"]
            other_sources = matching_sources(other, provided)
            if comparison["resolution"] != "unresolved" and (
                not cited & target_sources or not cited & other_sources or not comparison["reason"].strip()
            ):
                raise AssistantError("ungrounded_review", "裁决需要双方材料及明确理由")
            if comparison["relation"] == "unrelated" and comparison["resolution"] != "unresolved":
                raise AssistantError("review_inconsistent", "无关资料不能互相裁决")
        source = {"type": "reflection", "recorded_at": engine.clock().isoformat(),
                  "reference": job["target"]["data"]["id"]}
        payload = {
            "account_uid": store.read("manifest")["data"]["account_uid"],
            "target": ref(job["target"]), "related": [ref(r) for r in job["related"]],
            "sources": [ref(r) for r in sources], "result": result, "source": source, "locked": False,
            "method_trials": [ref(r) for r in job.get("method_trials", [])],
        }
        item_id = stable_id("review", json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return store.write("knowledge_reviews", {"schema_version": 1, "id": item_id, **payload},
                           None, engine.identity(), item_id)


async def maintain(scheduler):
    engine, store = scheduler.engine, scheduler.engine.store
    engine.role_run()
    settings = store.read("companion")
    if not settings["data"].get("knowledge_review", True) or not scheduler.remaining_model_calls():
        return None
    job = review_job(engine)
    if not job:
        return None
    with store.lock, store.db() as db:
        quota = review_usage(engine)
        if not quota["remaining"]:
            return None
        store.set_state(quota["key"], quota["used"] + 1, db)
    target = job["target"]["data"]
    # Revisit one real source per review, under the existing video budget.
    if target["coverage"]["kind"] != "web":
        try:
            await engine.video_context(target["coverage"]["bvid"], page_index(target["source"]["reference"]))
        except AssistantError as exc:
            if exc.code not in ("video_quota", "platform_read_error", "read_timeout"):
                raise
            store.audit("learning", {"event": "review_source_unavailable", "reason": exc.code})
    scheduler.alive()
    sources = {}
    for note in [job["target"], *job["related"]]:
        source = source_for(store, note, engine.run(check_host=False)["account"])
        if source:
            sources[source["data"]["id"]] = source
    # Bound long notes as well as raw subtitles. Truncation is explicit and prevents a full verdict.
    from copy import deepcopy

    payload = deepcopy(job)
    note_budget = max(300, settings["data"]["context_char_limit"] // (3 * (1 + len(job["related"]))))
    payload["target_truncated"] = len(target["content"]) > note_budget
    for note in [payload["target"], *payload["related"]]:
        note["content_truncated"] = len(note["data"]["content"]) > note_budget
        note["data"]["content"] = note["data"]["content"][:note_budget]
    payload["method_trials"] = [{
        **ref(t), "mode": t["data"]["mode"], "case": t["data"]["case"][:500],
        "observed": t["data"]["observed"][:500], "outcome": t["data"]["outcome"],
    } for t in job.get("method_trials", [])]
    remaining = settings["data"]["context_char_limit"] - len(json.dumps(payload, ensure_ascii=False))
    size = max(0, (remaining - 1500) // max(1, len(sources)))
    payload["sources"] = [source_excerpt(s, target["content"], size) for s in sources.values()]
    result = await scheduler.model(REVIEW_PROMPT, payload, REVIEW_RESULT)
    scheduler.alive()
    verify_versions(store, [("companion", None, settings["version"])])
    saved = save_review(engine, job, list(sources.values()), payload, result)
    store.audit("learning", {"event": "knowledge_review", "id": saved["data"]["id"]})
    return {"type": "knowledge_review", "saved": saved["data"]["id"]}


def user_evidence(engine, turn_id, quote):
    """Only a real user turn from this role chat, never an invented platform message."""
    engine.mode("role")
    store = engine.store
    from .conversations import thread_id

    record = store.read("conversation_turns", turn_id)
    doc = record["data"]
    account = store.read("manifest")["data"]["account_uid"]
    if (
        doc["account_uid"] != account or doc["thread_id"] != thread_id(account, "codex", engine.identity())
        or doc["speaker"] != "user" or doc["status"] != "received"
        or not quote.strip() or quote not in doc["content"]
    ):
        raise AssistantError("unverified_user_request", "需要本角色聊天已同步用户原话及其准确引用")
    return record


def record_trial(engine, skill_id, mode, case, expected, observed, outcome, limitations, evidence_id, evidence_quote):
    engine.mode("role")
    store = engine.store
    skill = store.read("skills", skill_id)
    if mode == "user_report":
        evidence = user_evidence(engine, evidence_id, evidence_quote)
    else:
        evidence = store.read("knowledge", evidence_id)
        if evidence_id not in skill["data"]["knowledge_ids"] or evidence_quote not in evidence["data"]["content"]:
            raise AssistantError("unverified_method", "推演必须引用技能卡的真实知识依据")
    if not evidence_quote.strip() or not all(s.strip() for s in (case, expected, observed, limitations)):
        raise AssistantError("unverified_method", "需要具体案例、预期、观察、局限和依据")
    payload = {
        "account_uid": store.read("manifest")["data"]["account_uid"], "skill": ref(skill),
        "mode": mode, "case": case, "expected": expected, "observed": observed,
        "outcome": outcome, "limitations": limitations, "evidence": ref(evidence), "evidence_quote": evidence_quote,
    }
    item_id = stable_id("trial", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    with store.lock:
        try:
            return store.read("method_trials", item_id)
        except AssistantError as exc:
            if exc.code != "not_found":
                raise
        return store.write("method_trials", {
            "schema_version": 1, "id": item_id, **payload,
            "source": {"type": "conversation" if mode == "user_report" else "reflection",
                       "recorded_at": engine.clock().isoformat(), "reference": evidence_id}, "locked": False,
        }, None, engine.identity(), item_id)
