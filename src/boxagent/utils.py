"""Shared utility functions used across multiple BoxAgent modules."""

import sys
from copy import deepcopy


def deep_merge_dicts(base: dict, override: dict) -> dict:
    """Recursively merge dictionaries; override values win."""
    result = deepcopy(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(base_value, value)
        else:
            result[key] = deepcopy(value)
    return result


def safe_print(text: str, *, file=None) -> None:
    """Print text even when the console encoding cannot represent it."""
    stream = file or sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, file=stream)
