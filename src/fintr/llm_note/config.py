"""Config (``configs/llm_note/config_v1.json``), prompt and output schema,
each with its SHA-256 for the record. Everything that shapes the model call
is a file in Git; nothing is hidden in code except the CLI flag names
(``runner.build_command``)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


CONFIG_SCHEMA = "fintr_llm_note_config_v1"


class ConfigError(ValueError):
    """The config, prompt or output schema is missing or malformed."""


@dataclass(frozen=True, slots=True)
class NoteConfig:
    path: Path
    sha256: str
    model: str
    effort: str
    cli_path: dict
    timeout_seconds: float
    version_timeout_seconds: float
    max_packet_bytes: int
    fallback_window_days: int
    strip_env: list
    prompt_path: Path
    prompt_sha256: str
    schema_path: Path
    schema_text: str
    schema_sha256: str


def _positive(document: dict, key: str, kind: type) -> float | int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or (
        kind is int and not isinstance(value, int)
    ):
        raise ConfigError(f"{key}: expected a positive {kind.__name__}")
    return value


def load_config(path: Path, *, project_root: Path) -> NoteConfig:
    try:
        raw = Path(path).read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("unreadable_config") from None
    if not isinstance(document, dict) or document.get("schema") != CONFIG_SCHEMA:
        raise ConfigError("unsupported_config_schema")
    for key in ("model", "effort", "prompt_file", "output_schema_file"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise ConfigError(f"{key}: expected a non-empty string")
    cli_path = document.get("cli_path")
    if not isinstance(cli_path, dict) or not all(
        isinstance(cli_path.get(key), str) and cli_path[key] for key in ("windows", "default")
    ):
        raise ConfigError("cli_path: expected windows and default strings")
    strip_env = document.get("strip_env", [])
    if not isinstance(strip_env, list) or not all(isinstance(item, str) for item in strip_env):
        raise ConfigError("strip_env: expected a list of strings")
    prompt_path = Path(project_root) / document["prompt_file"]
    schema_path = Path(project_root) / document["output_schema_file"]
    try:
        prompt_bytes = prompt_path.read_bytes()
        schema_bytes = schema_path.read_bytes()
        schema_text = json.dumps(json.loads(schema_bytes), ensure_ascii=False, separators=(",", ":"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("unreadable_prompt_or_schema") from None
    return NoteConfig(
        path=Path(path), sha256=hashlib.sha256(raw).hexdigest(),
        model=document["model"], effort=document["effort"], cli_path=cli_path,
        timeout_seconds=float(_positive(document, "timeout_seconds", float)),
        version_timeout_seconds=float(_positive(document, "version_timeout_seconds", float)),
        max_packet_bytes=int(_positive(document, "max_packet_bytes", int)),
        fallback_window_days=int(_positive(document, "fallback_window_days", int)),
        strip_env=list(strip_env), prompt_path=prompt_path,
        prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(), schema_path=schema_path,
        schema_text=schema_text, schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
    )
