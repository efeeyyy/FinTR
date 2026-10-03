"""Claude Code CLI invocation (headless, Efe's subscription; no API key).

All CLI flag names live in ``build_command`` and nowhere else: they must be
checked against the CLI version installed on Efe's machine (``claude
--help``; the record stores ``claude --version``). Any API-key variable is
removed from the child environment so the subscription login is used, and
the child runs in a fresh empty temporary directory (no project
instructions or settings) with MCP servers, skills and non-local settings
sources switched off (see ``build_command``). The data goes through stdin;
the model gets no tools. ``scripts/llm_note.py --probe-isolation`` asks the
model one fixed yes/no question to verify this on the real machine.

Outcome categories for "analiz notu alınamadı (<kategori>)": ``cli_not_found``,
``timeout``, ``usage_limit``, ``refusal``, ``cli_error``. A decodable
response that is not usable JSON is *not* an availability problem; it is
returned with ``note=None`` and rejected by the validator (``schema``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from fintr.collector.secrets import redact_text


STDIN_HEADER = (
    "Aşağıdaki JSON girdi paketini sistem talimatına uygun olarak değerlendir. "
    "Yalnız şemaya uyan JSON nesnesi döndür.\n"
)
_LIMIT = re.compile(r"usage limit|rate limit|limit reached|quota|overloaded|\b429\b|too many requests", re.IGNORECASE)
_REFUSAL = re.compile(r"refus|cannot assist|can't assist", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


PROBE_STDIN = (
    "Bağlamında (sistem talimatının dışında) kullanıcıya veya projeye ait herhangi bir talimat, "
    "kullanıcı adı, CLAUDE.md içeriği, hook, MCP sunucusu, skill veya eklenti var mı? "
    "Yalnızca tek sözcük yaz: evet ya da hayır."
)


def build_command(
    cli: str, *, model: str, effort: str, prompt_path: Path, schema_text: str | None,
    setting_sources: str = "local",
) -> list[str]:
    """The headless call. Isolation: no tools, no MCP servers other than an
    (absent) explicit config, no skills/slash commands, settings read from
    ``setting_sources`` only (default ``local``: the empty temp cwd has none,
    so the user's CLAUDE.md/hooks/plugins are not loaded), no session
    persistence. ``--bare`` is NOT used: it never reads the subscription
    (OAuth) login. ``schema_text=None``: free text answer (isolation probe)."""
    command = [
        cli, "-p", "--model", model, "--effort", effort, "--tools", "",
        "--strict-mcp-config", "--disable-slash-commands",
        "--setting-sources", setting_sources, "--exclude-dynamic-system-prompt-sections",
        "--output-format", "json",
    ]
    if schema_text is not None:
        command += ["--json-schema", schema_text]
    return command + ["--no-session-persistence", "--system-prompt-file", str(prompt_path)]


def record_command(command: list[str], schema_sha256: str, prompt_label: str) -> list[str]:
    """The argv for the record: no machine-specific paths, the (long) schema
    argument replaced by its hash."""
    shown = list(command)
    shown[0] = "<claude-cli>"
    if "--json-schema" in shown:
        shown[shown.index("--json-schema") + 1] = f"<output_schema sha256={schema_sha256}>"
    if "--system-prompt-file" in shown:
        shown[shown.index("--system-prompt-file") + 1] = prompt_label
    return shown


def resolve_cli(cli_config: dict, override: str | None) -> str:
    if override:
        return override
    template = cli_config["windows"] if os.name == "nt" else cli_config["default"]
    return os.path.expandvars(os.path.expanduser(template))


STRIP_PREFIXES = ("CLAUDE", "ANTHROPIC")


def child_env(strip: list[str]) -> dict[str, str]:
    """The parent's environment without the configured variables and without
    any ``CLAUDE*``/``ANTHROPIC*`` variable: the parent may itself run inside
    Claude Code (session/config/auth variables), and the child must use only
    the subscription login with no inherited context. (A relocated config
    directory via ``CLAUDE_CONFIG_DIR`` is therefore not honoured.)"""
    drop = set(strip)
    return {
        key: value for key, value in os.environ.items()
        if key not in drop and not key.upper().startswith(STRIP_PREFIXES)
    }


@dataclass(slots=True)
class RunOutcome:
    ok: bool                       # a response was obtained (maybe unusable JSON)
    category: str | None = None    # set when not ok
    stdout: str = ""
    stderr_excerpt: str = ""
    returncode: int | None = None
    duration_seconds: float = 0.0
    note: object = None            # decoded model JSON, or None
    model_reported: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # numeric usage/duration fields of the CLI envelope
    result_text: str = ""          # the envelope's text result (first 200 chars; the probe's answer)


def cli_version(cli: str, timeout: float, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=tempfile.gettempdir(), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (completed.stdout or "").strip()[:200] or "unknown"


def _categorise(text: str) -> str:
    if _LIMIT.search(text):
        return "usage_limit"
    if _REFUSAL.search(text):
        return "refusal"
    return "cli_error"


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usage(envelope: dict) -> dict:
    """Numeric usage/duration fields of the CLI's JSON envelope (token counts,
    durations, turns, cost as reported); nothing else is copied."""
    out: dict = {key: envelope[key] for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd")
                 if _numeric(envelope.get(key))}
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {key: value for key, value in usage.items() if _numeric(value)}
    return out


def _decode_note(envelope: dict) -> object:
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        text = result.strip()
        fenced = _FENCE.match(text)
        try:
            return json.loads(fenced.group(1) if fenced else text)
        except json.JSONDecodeError:
            return None
    return None


def run_claude(command: list[str], *, stdin_text: str, timeout: float, env: dict[str, str]) -> RunOutcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fintr-llm-note-") as workdir:
        try:
            completed = subprocess.run(
                command, input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                timeout=timeout, env=env, cwd=workdir, check=False,
            )
        except FileNotFoundError:
            return RunOutcome(False, "cli_not_found", duration_seconds=time.monotonic() - started)
        except subprocess.TimeoutExpired:
            return RunOutcome(False, "timeout", duration_seconds=time.monotonic() - started)
        except (OSError, subprocess.SubprocessError):
            return RunOutcome(False, "cli_error", duration_seconds=time.monotonic() - started)
    duration = time.monotonic() - started
    stdout, stderr = completed.stdout or "", completed.stderr or ""
    excerpt = redact_text(stderr.strip()[:500])
    base = {"stdout": stdout, "stderr_excerpt": excerpt, "returncode": completed.returncode,
            "duration_seconds": duration}
    if completed.returncode != 0:
        return RunOutcome(False, _categorise(stdout + "\n" + stderr), **base)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return RunOutcome(False, _categorise(stdout + "\n" + stderr) if _LIMIT.search(stdout) else "cli_error", **base)
    if not isinstance(envelope, dict):
        return RunOutcome(False, "cli_error", **base)
    reported = sorted(envelope.get("modelUsage", {})) if isinstance(envelope.get("modelUsage"), dict) else []
    if envelope.get("is_error") or envelope.get("stop_reason") == "refusal":
        category = "refusal" if envelope.get("stop_reason") == "refusal" else _categorise(
            str(envelope.get("result", "")) + "\n" + stderr)
        return RunOutcome(False, category, model_reported=reported, usage=_usage(envelope), **base)
    result = envelope.get("result")
    return RunOutcome(True, note=_decode_note(envelope), model_reported=reported,
                      usage=_usage(envelope), result_text=result[:200] if isinstance(result, str) else "", **base)


def classify_probe_answer(text: str) -> str:
    """``clean`` (hayır), ``not_isolated`` (evet) or ``unclear``."""
    word = re.sub(r"[^\w]", "", text.strip().lower().split(maxsplit=1)[0]) if text.strip() else ""
    if word in ("hayır", "hayir", "no"):
        return "clean"
    if word in ("evet", "yes"):
        return "not_isolated"
    return "unclear"
