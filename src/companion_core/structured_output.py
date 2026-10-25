"""Adapt optional JSON fields for strict transports without relaxing domain validation."""
from copy import deepcopy

from jsonschema import Draft202012Validator


def nullable(schema):
    return Draft202012Validator(schema).is_valid(None)


def strict_schema(schema):
    def visit(node):
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: visit(value) for key, value in node.items() if key != "uniqueItems"}
        if "properties" in node:
            required = set(node.get("required", []))
            for name, value in node["properties"].items():
                if name not in required and not nullable(value):
                    result["properties"][name] = {"anyOf": [result["properties"][name], {"type": "null"}]}
            result["required"] = list(node["properties"])
        return result

    return visit(schema)


def restore_optional(value, schema):
    """Only transport-added nulls become absent fields; required/unknown values survive validation."""
    if not isinstance(schema, dict):
        return deepcopy(value)
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            for variant in schema[keyword]:
                restored = restore_optional(value, variant)
                if Draft202012Validator(variant).is_valid(restored):
                    return restored
    if isinstance(value, dict):
        properties, required = schema.get("properties", {}), set(schema.get("required", []))
        return {key: restore_optional(item, properties.get(key, {})) for key, item in value.items()
                if not (key in properties and key not in required and item is None and not nullable(properties[key]))}
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [restore_optional(item, schema["items"]) for item in value]
    return deepcopy(value)
