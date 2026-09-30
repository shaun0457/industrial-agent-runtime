"""Deterministic, dependency-free JSON Schema subset for the G0 gate.

Unsupported keywords are errors, never silently ignored: a schema the runtime
cannot fully evaluate cannot authorize execution.
"""

from collections.abc import Mapping
import math
import re
from typing import Any

_ANNOTATIONS = frozenset({"title", "description", "examples", "default", "$comment"})
_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
    "maxLength", "minItems", "maxItems", "pattern",
}) | _ANNOTATIONS
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def schema_errors(schema: Any, path: str = "$") -> list[str]:
    """Return every reason the schema is outside the supported subset."""
    if not isinstance(schema, Mapping):
        return [f"{path}: schema must be an object"]
    errors = [f"{path}: unsupported keyword {key!r}" for key in schema if key not in _KEYWORDS]
    kind = schema.get("type")
    kinds = kind if isinstance(kind, (tuple, list)) else (kind,)
    if kind is not None and (not kinds or any(item not in _TYPES for item in kinds)):
        errors.append(f"{path}: invalid type {kind!r}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        errors.append(f"{path}: properties must be an object")
    else:
        for name, child in properties.items():
            errors += schema_errors(child, f"{path}.properties.{name}")
    required = schema.get("required", ())
    if (not isinstance(required, (tuple, list))
            or any(not isinstance(item, str) for item in required)):
        errors.append(f"{path}: required must be a string list")
    extra = schema.get("additionalProperties", True)
    if not isinstance(extra, bool):
        errors += schema_errors(extra, f"{path}.additionalProperties")
    if "items" in schema:
        errors += schema_errors(schema["items"], f"{path}.items")
    if "enum" in schema and not isinstance(schema["enum"], (tuple, list)):
        errors.append(f"{path}: enum must be a list")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _is_number(schema[key]):
            errors.append(f"{path}: {key} must be a finite number")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            errors.append(f"{path}: {key} must be a nonnegative integer")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            errors.append(f"{path}: invalid pattern")
    return errors


def _matches_type(value: Any, kind: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (tuple, list)),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": _is_number(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]


def _equal(left: Any, right: Any) -> bool:
    # JSON equality: booleans are not numbers, sequences compare structurally.
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(map(_equal, left, right))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    return left == right


def instance_errors(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate a JSON value against a schema already accepted by schema_errors."""
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, (tuple, list)) else (kind,)
        if not any(_matches_type(value, item) for item in kinds):
            return [f"{path}: expected {kind}"]
    errors = []
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: must equal const")
    if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
        errors.append(f"{path}: not in enum")
    if _is_number(value) and type(value) is not bool:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: not below exclusiveMaximum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern")
    if isinstance(value, (tuple, list)):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors += instance_errors(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(f"{path}: missing required {name!r}")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                errors += instance_errors(item, properties[name], f"{path}.{name}")
            elif extra is False:
                errors.append(f"{path}: unexpected property {name!r}")
            elif isinstance(extra, Mapping):
                errors += instance_errors(item, extra, f"{path}.{name}")
    return errors
