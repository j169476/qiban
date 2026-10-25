"""Verify a standalone core wheel without importing either integration package."""
import argparse
import importlib.abc
import json
import sys
import tempfile
import zipfile
from pathlib import Path


class BlockIntegrations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"bili_assistant", "bilibili_api", "companion_codex", "companion_service",
                                     "companion_wechat", "companion_qq"}:
            raise ImportError("Standalone core imported integration: " + fullname)


def check(wheel):
    with tempfile.TemporaryDirectory(prefix="companion-core-") as work:
        root = Path(work)
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert all(n.startswith("companion_core/") or ".dist-info/" in n for n in names)
            metadata = archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
            assert "Requires-Dist: bilibili" not in metadata
            archive.extractall(root / "installed")
        sys.path.insert(0, str(root / "installed"))
        sys.meta_path.insert(0, BlockIntegrations())
        import companion_core.runtime
        from companion_core import __version__
        from companion_core.backup_maintenance import BackupMaintenance
        from companion_core.backups import create, restore
        from companion_core.context_budget import pack
        from companion_core.conversation_maintenance import ConversationMaintenance
        from companion_core.conversation_query import page
        from companion_core.conversation_recall import recall
        from companion_core.fact_management import FactManagement
        from companion_core.input_images import InputImages
        from companion_core.interaction_growth import InteractionGrowth
        from companion_core.merge_candidates import plan
        from companion_core.recall_routing import resolve
        from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTargetProvider
        from companion_core.reply_policy import DEFAULTS, Participation, ReplyPolicies
        from companion_core.reply_trace import listing
        from companion_core.runtime_keys import PLATFORMS
        from companion_core.selection import Candidate, choose
        from companion_core.storage import Store
        from companion_core.structured_output import restore_optional, strict_schema
        from companion_core.temporal_facts import project
        assert Path(companion_core.runtime.__file__).is_relative_to(root / "installed")
        store = Store(root / "data")
        assert store.initialize()["ok"]
        create(store, root / "snapshot.zip")
        restore(root / "snapshot.zip", root / "restored")
        restored = Store(root / "restored")
        assert restored.validate_critical()["ok"]
        assert restored.state("core-run") is None
        assert choose([Candidate("a", "动画分镜")], settings={}, weights={"动画": 1})["selected"] == "a"
        assert callable(resolve) and not list(plan([], set(), {}, {}, 0)["pairs"])
        assert ConversationMaintenance.key("account", "test-platform", "conversation").startswith("private-compression:thread-")
        assert all(callable(value) for value in (BackupMaintenance, page, InputImages, InteractionGrowth))
        assert recall([], "synthetic missing history") == []
        assert callable(project) and not listing(store) and len(PLATFORMS) == 3
        assert 'companion_core/sql/008_input_images.sql' in names
        assert 'companion_core/sql/009_personal_fact_index.sql' in names
        assert callable(FactManagement)
        assert ReplyAuthorizations(store).version(ReplyScope("qq", "10001", "private", "10002")) is None
        assert callable(ReplyTargetProvider)
        assert ReplyPolicies(store).get("qq", "10001")["settings"] == DEFAULTS and callable(Participation)
        assert pack({"event": {"text": "synthetic"}}, {})[0]["event"]["text"] == "synthetic"
        optional = {"type": "object", "properties": {"value": {"type": "string"}}}
        assert strict_schema(optional)["required"] == ["value"]
        assert restore_optional({"value": None}, optional) == {}
        return {"ok": True, "version": __version__, "integration_imports_blocked": True,
                "wheel_only_core": True, "initialize_backup_restore": True,
                "algorithm_routing_imports_and_selection": True, "private_compression_independent": True,
                 "daily_core_components_independent": True,
                "conversation_recall_independent": True,
                "facts_and_reply_trace_independent": True,
                "reply_authorization_platform_independent": True,
                "reply_policy_platform_independent": True,
                "fact_management_budget_schema_and_migration": True,
                "role_started": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check(args.wheel)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
