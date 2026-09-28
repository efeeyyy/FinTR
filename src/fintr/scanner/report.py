"""Assemble the scanner's ``fintr_scanner_observations_v1`` output document.

Mirrors ``fintr.research.report``'s role for the study engine: nothing here
computes a new financial figure, it only serializes what
``fintr.scanner.observe``/``fintr.scanner.freshness`` already computed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import platform
import subprocess

from fintr.evaluation.symbol_guard import render_guard_lines
from fintr.scanner.freshness import FreshnessReport
from fintr.scanner.observe import NOTICE_TEXT_TR, RuleObservation


RESULT_SCHEMA = "fintr_scanner_observations_v1"

_GIT_CANDIDATES = ("git", r"C:\Program Files\Git\cmd\git.exe")


def resolve_code_commit(cwd: Path) -> str | None:
    """Best-effort commit hash; ``None`` (never fabricated) if git is
    unavailable under any known name."""
    for git in _GIT_CANDIDATES:
        try:
            completed = subprocess.run(
                [git, "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        commit = completed.stdout.strip()
        if commit:
            return commit
    return None


def _decimal_str(value) -> str | None:
    return None if value is None else str(value)


def _param_json(value):
    return list(value) if isinstance(value, tuple) else value


def _feature_payload(feature) -> dict:
    return {
        "name": feature.name,
        "params": {key: _param_json(value) for key, value in feature.params.items()},
        "formula": feature.formula,
        "value": _decimal_str(feature.value),
        "data_families": list(feature.data_families),
        "min_date": None if feature.min_date is None else feature.min_date.isoformat(),
        "max_date": None if feature.max_date is None else feature.max_date.isoformat(),
        "institution_labels": feature.institution_labels,
    }


def observation_payload(observation: RuleObservation) -> dict:
    return {
        "rule_id": observation.rule_id,
        "family_role": observation.family_role,
        "as_of": observation.as_of.isoformat(),
        "suppressed": observation.suppressed,
        "condition_met": observation.condition_met,
        "not_evaluable_reason": observation.not_evaluable_reason,
        "features": [_feature_payload(feature) for feature in observation.features],
        "validation_status": observation.validation_status,
    }


def freshness_payload(report: FreshnessReport) -> dict:
    def _d(value):
        return None if value is None else value.isoformat()
    return {
        "last_akd_server_date": _d(report.last_akd_server_date),
        "last_custody_date": _d(report.last_custody_date),
        "last_price_date": _d(report.last_price_date),
        "custody_behind_akd_server_dates": report.custody_behind_akd_server_dates,
        "custody_flagged_stale_t_plus_2": report.custody_flagged_stale,
    }


def build_document(
    *, project_root: Path, rules_file: str, rules_file_sha256: str,
    split_guard: dict, symbols: dict,
) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_commit": resolve_code_commit(project_root),
        "python_version": platform.python_version(),
        "rules_file": rules_file,
        "rules_file_sha256": rules_file_sha256,
        "split_guard": split_guard,
        "warning": NOTICE_TEXT_TR,
        "symbols": symbols,
    }


_FAMILY_ROLE_TR = {"primary": "birincil", "secondary_descriptive": "ikincil/tanımlayıcı"}


def render_text(document: dict) -> str:
    lines = [document["warning"], ""]
    guard = document.get("split_guard")
    if guard is not None:
        lines[:0] = render_guard_lines(guard)
    for symbol, block in document["symbols"].items():
        lines.append(f"{symbol} (as_of kaynağı: {block['as_of_source']}):")
        for scan in block["scans"]:
            freshness = scan["freshness"]
            stale = " [T+2 GECİKMİŞ]" if freshness["custody_flagged_stale_t_plus_2"] else ""
            lines.append(
                f"  as_of={scan['as_of']}: son AKD={freshness['last_akd_server_date'] or 'yok'}, "
                f"son takas={freshness['last_custody_date'] or 'yok'}{stale}, "
                f"son fiyat={freshness['last_price_date'] or 'yok'}"
            )
            for obs in scan["observations"]:
                role = _FAMILY_ROLE_TR.get(obs["family_role"], obs["family_role"])
                if obs["suppressed"]:
                    lines.append(f"    {obs['rule_id']} ({role}): bastırıldı ({obs['suppressed']})")
                    continue
                if obs["condition_met"] is None:
                    met = f"belirsiz ({obs['not_evaluable_reason']})"
                else:
                    met = "evet" if obs["condition_met"] else "hayır"
                lines.append(f"    {obs['rule_id']} ({role}): koşul_sağlandı={met}")
        lines.append("")
    return "\n".join(lines) + "\n"
