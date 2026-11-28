"""Publish human-readable health snapshots through an atomic current link."""

import copy
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import staticreports_health as health

DEFAULT_HEALTH_DATA_DIR = health.DEFAULT_WEB_ROOT / "health"
MAX_HEALTH_SNAPSHOT_BYTES = 5 * 1024 * 1024
STABLE_FILES = ("health.json", "health.txt")

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


def _ensure_stable_links(data_dir: Path) -> None:
    """Keep stable URLs pointing through the atomically switched current link."""
    for filename in STABLE_FILES:
        link = data_dir / filename
        target = Path("current") / filename
        if link.is_symlink():
            if link.readlink() == target:
                continue
            link.unlink()
        elif link.exists():
            raise OSError(f"health stable path {link} is not a symlink")
        link.symlink_to(target)


def _ensure_data_dirs(data_dir: Path) -> Path:
    """Ensure the root-owned data tree used for snapshot publication exists."""
    if data_dir.is_symlink():
        raise OSError("health data directory must not be a symlink")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o755)
    shutil.chown(data_dir, "root", "root")
    _ensure_stable_links(data_dir)

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


def _snapshot_prefix(document: dict[str, Any]) -> str:
    """Build a readable, time-ordered snapshot directory prefix."""
    timestamp = health.parse_time(str(document.get("generated_at") or "")) or health.utc_now()
    return f"snapshot.{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}."


def _current_snapshot(data_dir: Path) -> Path | None:
    """Return the snapshot directory currently selected by the health link."""
    current = data_dir / "current"
    try:
        target = current.readlink()
    except OSError:
        return None
    snapshot = data_dir / target
    try:
        snapshot = snapshot.resolve(strict=True)
    except OSError:
        return None
    if snapshot.parent != (data_dir / "snapshots").resolve():
        return None
    return snapshot


def _current_document(data_dir: Path) -> dict[str, Any] | None:
    """Read the current snapshot document, returning None when unavailable."""
    snapshot = _current_snapshot(data_dir)
    if snapshot is None:
        return None
    try:
        document = json.loads((snapshot / "health.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        logger.warning("Could not read the current health snapshot: %s", error)
        return None
    return document if isinstance(document, dict) else None


def _switch_current(data_dir: Path, snapshot: Path) -> None:
    """Atomically point the health link at a complete snapshot directory."""
    staging = Path(tempfile.mkdtemp(prefix=".current.", dir=data_dir))
    try:
        link = staging / "current"
        link.symlink_to(Path("snapshots") / snapshot.name, target_is_directory=True)
        os.replace(link, data_dir / "current")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _snapshot_size(snapshot: Path) -> int:
    """Return the byte size of the published files in one snapshot."""
    total = 0
    for filename in STABLE_FILES:
        try:
            total += (snapshot / filename).stat().st_size
        except OSError:
            continue
    return total


def _retained_snapshots(snapshots_dir: Path) -> list[Path]:
    """Return snapshot directories in creation order."""
    snapshots = [
        child
        for child in snapshots_dir.iterdir()
        if child.name.startswith("snapshot.") and child.is_dir() and not child.is_symlink()
    ]
    return sorted(snapshots, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _trim_snapshots(snapshots_dir: Path, current_name: str, max_bytes: int) -> None:
    """Drop the oldest snapshots until the retained files fit the size budget."""
    snapshots = _retained_snapshots(snapshots_dir)
    total = sum(_snapshot_size(snapshot) for snapshot in snapshots)
    for snapshot in snapshots:
        if total <= max_bytes:
            break
        if snapshot.name == current_name:
            continue
        total -= _snapshot_size(snapshot)
        shutil.rmtree(snapshot, ignore_errors=True)


def _remove_snapshot(snapshot: Path) -> None:
    """Remove a snapshot after a failed publication attempt."""
    shutil.rmtree(snapshot, ignore_errors=True)


def publish_health_document(
    document: dict[str, Any],
    *,
    data_dir: Path | None = None,
    max_snapshot_bytes: int = MAX_HEALTH_SNAPSHOT_BYTES,
) -> bool:
    """Publish a complete human-readable health snapshot.

    A changed semantic state creates a new snapshot directory. An unchanged
    state atomically replaces the current snapshot with the latest document
    instead of retaining another duplicate. The newest complete snapshot is
    always kept; older snapshots are removed only when the size budget is
    exceeded.
    """
    directory = data_dir or DEFAULT_HEALTH_DATA_DIR
    snapshot: Path | None = None
    committed = False
    try:
        snapshots_dir = _ensure_data_dirs(directory)
        previous_snapshot = _current_snapshot(directory)
        same_state = previous_snapshot is not None and _same_health_state(
            _current_document(directory) or {}, document
        )

        snapshot = Path(tempfile.mkdtemp(prefix=_snapshot_prefix(document), dir=snapshots_dir))
        _write_snapshot_file(
            snapshot / "health.json", json.dumps(document, indent=2, sort_keys=True)
        )
        _write_snapshot_file(snapshot / "health.txt", health.format_text(document) + "\n")

        os.chmod(snapshot, 0o755)
        shutil.chown(snapshot, "root", "root")
        snapshot_name = snapshot.name
        _switch_current(directory, snapshot)
        snapshot = None
        committed = True

        if (
            same_state
            and previous_snapshot is not None
            and previous_snapshot != (snapshots_dir / snapshot_name).resolve()
        ):
            shutil.rmtree(previous_snapshot, ignore_errors=True)
        _trim_snapshots(snapshots_dir, snapshot_name, max_snapshot_bytes)
        return True
    except (OSError, LookupError, TypeError, ValueError) as error:
        logger.warning("Could not publish health snapshot: %s", error)
        if snapshot is not None and not committed:
            _remove_snapshot(snapshot)
        return False
