"""Bilibili's existing file formats and table migration, injected into the core."""
from pathlib import Path

import yaml

from companion_core.profiles import DataProfile, default_profile
from companion_core.schema import IDENTITY_SCHEMAS


def migrate(store):
    with store.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS videos(account TEXT,day TEXT,bvid TEXT,PRIMARY KEY(account,day,bvid))")


def annotate(store, records):
    from .knowledge import annotations
    from .persona import annotate_relationship
    persona = store.read("persona")["data"]
    return [annotate_relationship(r, persona) for r in annotations(store, records)]


def profile():
    from . import schema
    defaults = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8"))
                for p in (Path(__file__).parent / "defaults").glob("*.yaml")}
    common = default_profile()
    shared = {"relationships", "conversation_turns", "conversation_summaries", "emotion_events",
              "task_requests", "task_results", "task_assets", "interaction_growth", "input_images"}

    def validate(kind, data):
        return common.validate(kind, data) if kind in shared else schema.validate(kind, data)

    return DataProfile({**schema.SCHEMAS, **IDENTITY_SCHEMAS, **{kind: common.schemas[kind] for kind in shared}},
                        defaults, extra_tables={"videos"}, validator=validate, migrator=migrate, annotator=annotate)
