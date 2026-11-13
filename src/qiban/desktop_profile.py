"""One desktop database, with shared identity/configuration and Bilibili activity records."""
from dataclasses import replace

from bili_assistant import schema
from bili_assistant.profiles import migrate
from companion_core.errors import AssistantError
from companion_core.profiles import default_profile


def desktop_profile():
    common = default_profile()
    schemas = dict(common.schemas)
    bili_only = schema.SCHEMAS.keys() - schemas.keys()
    shared_variants = {kind for kind in common.collections if kind in schema.SCHEMAS
                       and schemas[kind] != schema.SCHEMAS[kind]}
    for kind in bili_only:
        schemas[kind] = schema.SCHEMAS[kind]
    for kind in shared_variants:
        schemas[kind] = {"anyOf":[schemas[kind],schema.SCHEMAS[kind]]}

    def validate(kind, data):
        if kind in bili_only:
            return schema.validate(kind,data)
        try:
            return common.validate(kind,data)
        except AssistantError:
            if kind not in shared_variants:
                raise
            return schema.validate(kind,data)

    return replace(common,schemas=schemas,validator=validate,extra_tables={"videos"},migrator=migrate)
