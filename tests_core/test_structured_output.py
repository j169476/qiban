from copy import deepcopy

import pytest
from jsonschema import ValidationError, validate

from companion_core.schema import obj
from companion_core.structured_output import restore_optional, strict_schema


def test_optional_fields_are_required_nullable_only_on_the_wire():
    schema = obj({"required": {"type": "string"}, "optional": {"type": "array", "uniqueItems": True,
                  "items": obj({"x": {"type": "string"}, "y": {"type": "integer"}}, required=["x"])},
                  "nullable": {"type": ["string", "null"]}}, required=["required"])
    before = deepcopy(schema)
    wire = strict_schema(schema)
    validate({"required": "yes", "optional": None, "nullable": None}, wire)
    assert set(wire["required"]) == set(schema["properties"]) and schema == before
    result = restore_optional({"required": "yes", "optional": None, "nullable": None}, schema)
    assert result == {"required": "yes", "nullable": None}
    validate(result, schema)
    assert restore_optional({"required": "yes", "optional": [{"x": "ok", "y": None}]}, schema) == {
        "required": "yes", "optional": [{"x": "ok"}]}


@pytest.mark.parametrize("value", [{}, {"required": None}, {"required": "yes", "extra": True},
                                  {"required": "yes", "optional": ["same", "same"]}])
def test_domain_schema_still_rejects_missing_fields_unknown_keys_and_duplicate_values(value):
    schema = obj({"required": {"type": "string"}, "optional": {"type": "array", "uniqueItems": True,
                  "items": {"type": "string"}}}, required=["required"])
    with pytest.raises(ValidationError):
        validate(restore_optional(value, schema), schema)


def test_union_objects_restore_nested_optional_fields_without_coercing_types():
    schema = obj({"choice": {"anyOf": [{"type": "null"}, obj({"name": {"type": "string"},
                  "count": {"type": "integer"}}, required=["name"])]}})
    assert restore_optional({"choice": {"name": "value", "count": None}}, schema) == {"choice": {"name": "value"}}
    with pytest.raises(ValidationError):
        validate(restore_optional({"choice": {"name": "value", "count": "wrong"}}, schema), schema)
