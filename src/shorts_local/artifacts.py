import hashlib
import json
from pathlib import Path

from .process import atomic_json


def artifact_key(stage: str, inputs: dict) -> str:
    value = json.dumps({"schema": 1, "stage": stage, "inputs": inputs}, sort_keys=True,
                       ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def read_artifact(path: Path, key: str) -> dict | None:
    try:
        value = json.loads(path.read_text())
        if value["schema"] == 1 and value["key"] == key and isinstance(value["data"], dict):
            return value["data"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_artifact(path: Path, key: str, data: dict) -> None:
    atomic_json(path, {"schema": 1, "key": key, "data": data})
