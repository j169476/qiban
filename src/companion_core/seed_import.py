"""Explicit fresh-start import; retain persona seeds and operational receipts.

This selection is an installer operation, never a model or ordinary web tool.
Imported personal material stays quarantined until the local owner sets a password.
No platform identities or cross-account grants are inferred from a persona card.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy

import yaml

from .access import RecordPolicy
from .errors import AssistantError
from .governance import resource_id
from .storage import digest

COUNTERS = ("model-budget:", "model-cycles:", "core-model:", "action-budget-",
            "search-count:", "video-gaps:", "native:", "knowledge-review:")


def select(snapshot, profile, *, quota_tables=()):
    """Build a new deterministic snapshot without touching the source snapshot."""
    records = {(r["kind"], r["id"]): r for r in snapshot["records"]}
    persona = records["persona", None]["data"]
    identifiers = persona.get("foundation_memories", [])
    if len(identifiers) > 100 or len(set(identifiers)) != len(identifiers):
        raise AssistantError("seed_selection", "基础记忆清单无效")
    wanted = {(kind, None) for kind in profile.config} | {("memories", i) for i in identifiers}
    if not wanted <= records.keys():
        raise AssistantError("seed_missing", "人物卡引用的基础记忆缺失，未开始迁移")
    for identifier in identifiers:
        if records["memories", identifier]["data"].get("source", {}).get("type") != "user_instruction":
            raise AssistantError("seed_source", "基础记忆必须有明确的用户设定来源，不能把经历升级为设定")
    selected = [deepcopy(r) for r in snapshot["records"] if (r["kind"], r["id"]) in wanted]
    growth = deepcopy(profile.defaults["growth"])
    raw = yaml.safe_dump(growth, allow_unicode=True, sort_keys=False).encode("utf-8")
    selected.append({"kind": "growth", "id": None, "data": growth, "raw": raw, "version": digest(raw)})
    preserved = {"sessions", "actions", "core_actions"} | set(quota_tables)
    if not set(quota_tables) <= profile.extra_tables:
        raise AssistantError("seed_selection", "额度账本不属于当前平台约定")
    tables = deepcopy(snapshot["tables"])
    for table, block in tables.items():
        if table == "state":
            block["rows"] = [row for row in block["rows"] if row["key"].startswith(COUNTERS)]
            block["rows"].append({"key": "run", "value": json.dumps({"status": "paused", "reason": "seed_import"})})
        elif table not in preserved:
            block["rows"] = []
    paths = {"assets/persona/" + item["filename"] for item in (persona.get("visual_identity") or {}).get("references", [])}
    assets = [deepcopy(a) for a in snapshot.get("assets", []) if a["path"] in paths]
    if {a["path"] for a in assets} != paths:
        raise AssistantError("seed_missing", "人物参考图片缺失，未开始迁移")
    selection = {"mode": "persona_seed", "foundation_ids": identifiers,
                 "retained_records": [(r["kind"], r["id"], r["version"]) for r in selected],
                 "omitted_records": len(snapshot["records"]) - len(wanted) - 1,
                 "reset_growth": True, "retained_ledger_tables": sorted(preserved)}
    fingerprint = hashlib.sha256(json.dumps({"source": snapshot["fingerprint"], "selection": selection},
                                            sort_keys=True).encode()).hexdigest()
    return {"records": selected, "tables": tables, "assets": assets, "fingerprint": fingerprint,
            "source_fingerprint": snapshot["fingerprint"], "selection": selection}


def stage_owner_claim(store, selection, db):
    """Freeze the exact seed content and its still-quarantined permission version."""
    from .governance import GovernanceRepository

    repository = GovernanceRepository(store)
    resources = [("persona", None), *(("memories", i) for i in selection["foundation_ids"])]
    pins = []
    for kind, identifier in resources:
        policy = repository.policy(resource_id(kind, identifier))
        if not policy or policy.classification != "unclassified" or policy.sources or policy.owners:
            raise AssistantError("seed_policy", "基础资料权限已变化，不能自动接管")
        pins.append({"kind": kind, "id": identifier, "version": store.version(kind, identifier),
                     "policy_version": policy.version})
    store.set_state("seed-owner-claim", {"version": 1, "pending": True, "pins": pins}, db)


def claim_local_owner(repository, actor, db):
    """Called only inside successful first-password setup, in its transaction."""
    store = repository.store
    plan = store.state("seed-owner-claim", {}, db)
    if not plan or not plan.get("pending"):
        return
    receipt = store.state("legacy-import", {}, db).get("selection", {})
    if receipt.get("mode") != "persona_seed" or plan.get("version") != 1:
        raise AssistantError("seed_policy", "基础资料接管记录无效")
    expected = {("persona", None)} | {("memories", i) for i in receipt["foundation_ids"]}
    pins = plan.get("pins", [])
    if len(pins) != len(expected) or {(p["kind"], p["id"]) for p in pins} != expected:
        raise AssistantError("seed_policy", "基础资料接管范围已变化")
    for pin in pins:
        resource = resource_id(pin["kind"], pin["id"])
        policy = repository.policy(resource)
        if (not policy or policy.version != pin["policy_version"] or policy.classification != "unclassified"
                or policy.sources or policy.owners or not policy.valid
                or store.version(pin["kind"], pin["id"]) != pin["version"]):
            raise AssistantError("seed_changed", "基础资料在初始化前已变化，请管理端重新核对")
        repository.set_policy(RecordPolicy(resource, secrets.token_hex(16), "personal", frozenset({actor.key})),
                              policy.version, "owner-setup:" + actor.key, db=db)
    store.set_state("seed-owner-claim", {**plan, "pending": False, "actor": actor.key}, db)
