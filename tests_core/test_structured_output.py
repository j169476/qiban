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

@pytest.mark.parametrize('node,expected', [
    ({'const':'message.reply'},'string'), ({'const':1},'integer'),
    ({'const':False},'boolean'), ({'const':None},'null'),
    ({'enum':['text','voice']},'string'), ({'enum':[1.5,2.5]},'number'),
    ({'enum':['text',None]},['string','null']),
])
def test_strict_transport_adds_types_to_enum_and_const_without_changing_values(node,expected):
    before=deepcopy(node)
    wire=strict_schema(obj({'value':node}))['properties']['value']
    assert wire['type']==expected and node==before
    assert {k:v for k,v in wire.items() if k!='type'}==node


def test_bilibili_favorite_tool_discriminators_are_explicitly_typed():
    from bili_assistant.favorite_workflow import SCHEMA
    wire=strict_schema(SCHEMA)
    for variant in wire['properties']['call']['anyOf']:
        assert variant['properties']['tool']['type']=='string'
    result={'call':{'tool':'done','arguments':{}}}
    validate(result,wire)
    validate(restore_optional(result,SCHEMA),SCHEMA)
