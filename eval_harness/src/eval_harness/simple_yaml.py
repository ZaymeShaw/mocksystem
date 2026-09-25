"""Minimal YAML subset loader for flat harness configs (no PyYAML required)."""
from __future__ import annotations

from typing import Any


def load_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a restricted YAML: top-level key: value, lists via [a, b], booleans, ints, strings."""
    data: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if val == "":
            data[key] = ""
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                data[key] = []
            else:
                items = []
                for part in inner.split(","):
                    items.append(_scalar(part.strip()))
                data[key] = items
            continue
        data[key] = _scalar(val)
    return data


def _scalar(val: str) -> Any:
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    low = val.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
            return int(val)
    except Exception:
        pass
    try:
        if "." in val:
            return float(val)
    except Exception:
        pass
    return val
