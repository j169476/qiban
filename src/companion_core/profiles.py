"""Inject data contracts and optional application behavior into the core store."""
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import AssistantError
from .schema import IDENTITY_SCHEMAS, MEMORY_COLLECTIONS, validate_identity_link

BASE_TABLES = {"sessions", "state", "actions", "events", "relationship_pending"}
CORE_TABLES = {"core_events", "core_actions", "core_identity_links", "core_observations"}


def migrate_core(store):
    with store.db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS core_events(
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL,
                actor TEXT NOT NULL, conversation TEXT NOT NULL, occurred TEXT NOT NULL,
                body TEXT NOT NULL, version TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS core_actions(
                id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, generation TEXT NOT NULL,
                platform TEXT NOT NULL, account TEXT NOT NULL, capability TEXT NOT NULL,
                body TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS core_identity_links(
                id TEXT PRIMARY KEY, left_actor TEXT NOT NULL, right_actor TEXT NOT NULL,
                status TEXT NOT NULL, version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS core_observations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL,
                started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL,
                error_code TEXT, duration_ms INTEGER, input_tokens INTEGER,
                cached_input_tokens INTEGER, output_tokens INTEGER,
                reasoning_output_tokens INTEGER, usage_events INTEGER NOT NULL DEFAULT 0);
        """)


@dataclass
class DataProfile:
    schemas: dict
    defaults: dict
    config: tuple = ("manifest", "persona", "preferences", "policy", "companion")
    memory_collections: tuple = MEMORY_COLLECTIONS
    extra_tables: set = field(default_factory=set)
    validator: object = None
    migrator: object = None
    annotator: object = None

    @property
    def collections(self):
        return tuple(k for k in self.schemas if k not in (*self.config, "growth"))

    @property
    def tables(self):
        return BASE_TABLES | CORE_TABLES | self.extra_tables

    def validate(self, kind, data):
        if self.validator and kind not in IDENTITY_SCHEMAS:
            return self.validator(kind, data)
        schema = self.schemas.get(kind)
        if schema is None:
            raise AssistantError("schema_unknown", kind)
        errors = list(Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(data))
        if errors:
            raise AssistantError("invalid_data", f"{kind}: 数据不符合结构约束")
        if kind == "identity_links":
            validate_identity_link(data)
        return data

    def migrate(self, store):
        migrate_core(store)
        if self.migrator:
            self.migrator(store)

    def annotate(self, store, records):
        if self.annotator:
            return self.annotator(store, records)
        from .persona import annotate_relationship
        persona = store.read("persona")["data"]
        return [annotate_relationship(r, persona) for r in records]


def default_profile():
    from .algorithm_policy import DEFAULTS, SCHEMA
    from .context_budget import DEFAULTS as CONTEXT_DEFAULTS
    from .context_budget import PROPERTIES as CONTEXT_PROPERTIES
    from .input_images import SCHEMA as IMAGE_SCHEMA
    from .interaction_growth import SCHEMA as GROWTH_SCHEMA
    from .service_policy import PROPERTIES
    from .task_contracts import SCHEMAS
    from .temporal_facts import FACT

    root = Path(__file__).parent
    schemas = {**json.loads((root / "schemas.json").read_text(encoding="utf-8")), **IDENTITY_SCHEMAS}
    schemas["companion"]["properties"]["algorithm_routing"] = SCHEMA
    schemas["companion"]["properties"].update(CONTEXT_PROPERTIES)
    schemas.update(SCHEMAS)
    schemas["memories"]["properties"]["fact"] = FACT
    schemas["policy"]["properties"].update(PROPERTIES)
    schemas["interaction_growth"] = GROWTH_SCHEMA
    schemas["input_images"] = IMAGE_SCHEMA
    defaults = json.loads((root / "defaults.json").read_text(encoding="utf-8"))
    defaults["companion"]["algorithm_routing"] = deepcopy(DEFAULTS)
    defaults["companion"].update(CONTEXT_DEFAULTS)
    return DataProfile(schemas, defaults)
