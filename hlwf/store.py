"""Small on-disk cache helpers (gzipped JSON, atomic writes)."""
from __future__ import annotations

import gzip
import json
import os
from typing import Any


def load_gz(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with gzip.open(path, "rt") as f:
        return json.load(f)


def save_gz(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: str, obj: Any, indent: int | None = 1) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent, default=str)
    os.replace(tmp, path)
