"""Publish periodic health snapshots through an atomic current link."""

import copy
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import staticreports_health as health

DEFAULT_HEALTH_DATA_DIR = Path("/srv/staticreports/health")
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


def _ensure_data_dirs(data_dir: Path) -> Path:
    """Ensure the root-owned data tree used for snapshot publication exists."""
    if data_dir.is_symlink():
        raise OSError("health data directory must not be a symlink")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o755)
    shutil.chown(data_dir, "root", "root")

    snapshots_dir = data_dir / "snapshots"
    if snapshots_dir.is_symlink():
        raise OSError("health snapshots directory must not be a symlink")
    snapshots_dir.mkdir(exist_ok=True)
    os.chmod(snapshots_dir, 0o755)
    shutil.chown(snapshots_dir, "root", "root")
    return snapshots_dir


def _write_snapshot_file(path: Path, content: str) -> None:
    """Write one file inside a newly created, root-only snapshot directory."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o644)


def _previous_snapshot_name(data_dir: Path) -> str | None:
    """Return the snapshot name currently selected by the health link."""
    current = data_dir / "current"
    try:
        return current.readlink().name
    except OSError:
        return None


def _switch_current(data_dir: Path, snapshot: Path) -> None:
    """Atomically point the health link at a complete snapshot directory."""
    staging = Path(tempfile.mkdtemp(prefix=".current.", dir=data_dir))
    try:
        link = staging / "current"
        link.symlink_to(Path("snapshots") / snapshot.name, target_is_directory=True)
        os.replace(link, data_dir / "current")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _cleanup_snapshots(snapshots_dir: Path, keep: set[str]) -> None:
    """Retain the current and previous snapshots; remove older ones."""
    try:
        children = list(snapshots_dir.iterdir())
    except OSError as error:
        logger.warning("Could not inspect old health snapshots: %s", error)
        return
    for child in children:
        if child.name in keep or child.is_symlink() or not child.is_dir():
            continue
        shutil.rmtree(child, ignore_errors=True)


def _remove_incomplete_snapshot(snapshot: Path) -> None:
    """Remove a staged snapshot after a failed publication attempt."""
    shutil.rmtree(snapshot, ignore_errors=True)


def publish_health_document(
    document: dict[str, Any],
    *,
    data_dir: Path | None = None,
    max_history_bytes: int = MAX_HEALTH_HISTORY_BYTES,
) -> bool:
    """Publish a complete health snapshot and bounded state history.

    A new root-only snapshot is prepared in full before one atomic symlink
    switch makes its files visible together. A failed publication leaves the
    previous complete snapshot selected.
    """
    directory = data_dir or DEFAULT_HEALTH_DATA_DIR
    snapshot: Path | None = None
    try:
        snapshots_dir = _ensure_data_dirs(directory)
        previous_name = _previous_snapshot_name(directory)
        history_path = directory / "current" / "history.jsonl"
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
        snapshot = Path(tempfile.mkdtemp(prefix="snapshot.", dir=snapshots_dir))
        _write_snapshot_file(
            snapshot / "health.json", json.dumps(document, indent=2, sort_keys=True)
        )
        _write_snapshot_file(snapshot / "health.txt", health.format_text(document) + "\n")
        _write_snapshot_file(snapshot / "history.jsonl", _serialize_history(entries))

        os.chmod(snapshot, 0o755)
        shutil.chown(snapshot, "root", "root")
        snapshot_name = snapshot.name
        _switch_current(directory, snapshot)
        snapshot = None
        _cleanup_snapshots(snapshots_dir, {snapshot_name, previous_name or ""})
        return True
    except (OSError, LookupError, TypeError, ValueError) as error:
        logger.warning("Could not publish health snapshot: %s", error)
        if snapshot is not None:
            _remove_incomplete_snapshot(snapshot)
        return False
