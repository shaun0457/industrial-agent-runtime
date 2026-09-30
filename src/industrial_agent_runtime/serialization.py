"""Strict JSON conversion and immutable snapshots, with no provider objects."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Return only JSON primitives; reject non-finite numbers and opaque objects."""
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name))
                for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"Not a JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def freeze_json(value: Any) -> Any:
    """Defensively copy JSON values so nested mutation cannot change artifacts."""
    clean = to_jsonable(value)
    if isinstance(clean, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in clean.items()})
    if isinstance(clean, list):
        return tuple(freeze_json(item) for item in clean)
    return clean
