"""Serialization utilities for OpenTelemetry and other use cases."""

import json
from decimal import Decimal
from datetime import datetime, date
from typing import Any, Set, Union


def safe_key(k: Any) -> str:
    """Convert key to safe string for OpenTelemetry."""
    if isinstance(k, (str, int, float, bool)) or k is None:
        return str(k)
    try:
        if isinstance(k, (bytes, bytearray)):
            return k.decode("utf-8")
    except Exception:
        pass
    return repr(k)


def _to_safe_obj(obj: Any, _visited: Union[Set[int], None] = None) -> Any:
    """Convert object to OpenTelemetry-safe types."""
    if _visited is None:
        _visited = set()

    obj_id = id(obj)
    if obj_id in _visited:
        return f"<Cycle ref id={obj_id}>"
    if isinstance(obj, (list, tuple, set, dict)):
        _visited.add(obj_id)

    try:
        if hasattr(obj, "to_json"):
            return obj.to_json()

        if isinstance(obj, dict):
            return {safe_key(k): _to_safe_obj(v, _visited) for k, v in obj.items()}

        if isinstance(obj, (list, tuple, set)):
            res = [_to_safe_obj(i, _visited) for i in obj]
            if len(res) > 100:
                return res[:100] + [f"... {len(res) - 100} more"]
            return res

        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj

        if isinstance(obj, (bytes, bytearray)):
            try:
                return obj.decode("utf-8")
            except Exception:
                return repr(obj)

        if isinstance(obj, Decimal):
            return str(obj)

        if isinstance(obj, (datetime, date)):
            return obj.isoformat()

        return repr(obj)

    except Exception:
        return repr(obj)


def safe_serialize(obj: Any) -> Union[str, list]:
    """
    Serialize an object to objects allowed by OpenTelemetry.

    Allowed types:
      - str, int, float, bool
      - list[str, int, float, bool]
    """

    def _to_simple(o: Any) -> Any:
        if isinstance(o, (dict, list, tuple)):
            return json.dumps(o)
        return o

    res = _to_safe_obj(obj)
    if res is None:
        return "null"

    if isinstance(res, (list, tuple)):
        return [_to_simple(i) for i in res]

    return _to_simple(res)
