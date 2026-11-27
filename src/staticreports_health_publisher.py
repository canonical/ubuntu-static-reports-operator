"""Publish periodic health snapshots to the nginx-served web root."""

import copy
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import staticreports_health as health

DEFAULT_HEALTH_WEB_DIR = health.DEFAULT_WEB_ROOT / "health"
MAX_HEALTH_HISTORY_BYTES = 5 * 1024 * 1024

logger = logging.getLogger(__name__)


def _normalized_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a comparison copy that ignores fields that change every sample."""
    normalized = copy.deepcopy(document)
    normalized.pop("generated_at", None)
    for report in normalized.get("reports", []):
        if isinstance(report, dict):
            report.pop("running_seconds", None)
    return normalized


def _same_health_state(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return whether two documents represent the same semantic health state."""
    return _normalized_document(left) == _normalized_document(right)


def _read_history(path: Path) -> list[dict[str, Any]]:
    """Read valid history entries, skipping malformed lines with a warning."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        logger.warning("Could not read health history %s: %s", path, error)
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            logger.warning("Skipping malformed health history line: %s", error)
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("document"), dict):
            logger.warning("Skipping health history line without a document object")
            continue
        if not isinstance(entry.get("sample_count"), int):
            logger.warning("Skipping health history line with an invalid sample count")
            continue
        if not isinstance(entry.get("first_seen_at"), str) or not isinstance(
            entry.get("last_seen_at"), str
        ):
            logger.warning("Skipping health history line with invalid interval times")
            continue
        entries.append(entry)
    return entries


def _serialize_history(entries: list[dict[str, Any]]) -> str:
    """Render history entries as compact newline-delimited JSON."""
    return "".join(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n" for entry in entries
    )


def _trim_history(entries: list[dict[str, Any]], max_history_bytes: int) -> list[dict[str, Any]]:
    """Drop oldest entries until the serialized history fits the size budget."""
    while len(entries) > 1:
        if len(_serialize_history(entries).encode("utf-8")) <= max_history_bytes:
            break
        entries.pop(0)
    return entries


def _history_entry(document: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """Build the first history entry for a newly observed health state."""
    return {
        "schema_version": health.SCHEMA_VERSION,
        "first_seen_at": generated_at,
        "last_seen_at": generated_at,
        "sample_count": 1,
        "document": copy.deepcopy(document),
    }


def _atomic_write(path: Path, content: str) -> bool:
    """Atomically replace a health file with ubuntu-owned, world-readable bytes."""
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o644)
        shutil.chown(temporary, "ubuntu", "ubuntu")
        os.replace(temporary, path)
    except (OSError, LookupError) as error:
        logger.warning("Could not publish health file %s: %s", path, error)
        temporary.unlink(missing_ok=True)
        return False
    return True


def publish_health_document(
    document: dict[str, Any],
    *,
    health_dir: Path | None = None,
    max_history_bytes: int = MAX_HEALTH_HISTORY_BYTES,
) -> bool:
    """Publish the latest health snapshot and bounded state history.

    The latest files always use stable names. History entries retain the full
    document, but unchanged states update the newest interval instead of adding
    a duplicate line.
    """
    directory = health_dir or DEFAULT_HEALTH_WEB_DIR
    history_path = directory / "history.jsonl"
    entries = _read_history(history_path)
    generated_at = str(document.get("generated_at") or health.iso_time())

    if entries and _same_health_state(entries[-1]["document"], document):
        latest = entries[-1]
        latest["document"] = copy.deepcopy(document)
        latest["last_seen_at"] = generated_at
        latest["sample_count"] = int(latest.get("sample_count", 0)) + 1
    else:
        entries.append(_history_entry(document, generated_at))

    entries = _trim_history(entries, max_history_bytes)
    history = _serialize_history(entries)
    latest_json = json.dumps(document, indent=2, sort_keys=True) + "\n"
    latest_text = health.format_text(document) + "\n"

    published_json = _atomic_write(directory / "health.json", latest_json)
    published_text = _atomic_write(directory / "health.txt", latest_text)
    published_history = _atomic_write(history_path, history)
    return published_json and published_text and published_history
