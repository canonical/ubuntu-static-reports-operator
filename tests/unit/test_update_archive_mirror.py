# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Unit tests for update-archive-mirror snapshot consistency."""

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SUITE = "tenacious"
INDICES = {
    "main/source/Sources.gz": b"source metadata",
    "main/binary-amd64/Packages.gz": b"amd64 packages",
    "main/binary-arm64/Packages.gz": b"arm64 packages",
}
DATE = "Tue, 10 Nov 2026  2:15:44 UTC"


def _load_script():
    path = Path(__file__).parents[2] / "src" / "script" / "update-archive-mirror"
    spec = importlib.util.spec_from_loader(
        "update_archive_mirror", SourceFileLoader("update_archive_mirror", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mirror():
    return _load_script()


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Model rsync replacing a file rather than modifying a hardlinked snapshot.
    if path.exists():
        path.unlink()
    path.write_bytes(content)


def _write_index(root: Path, relative: str, content: bytes) -> None:
    _write_file(root / "dists" / SUITE / relative, content)


def _write_release(
    root: Path, entries: dict[str, bytes], algorithm: str = "sha512", date: str = DATE
) -> None:
    field = algorithm.upper()
    lines = [f"Date: {date}", f"{field}:"]
    for relative, content in entries.items():
        digest = hashlib.new(algorithm, content).hexdigest()
        lines.append(f" {digest} {len(content)} {relative}")
    _write_file(root / "dists" / SUITE / "Release", "\n".join(lines).encode() + b"\n")


def _populate_work_trees(primary: Path, ports: Path) -> None:
    for relative in ("main/source/Sources.gz", "main/binary-amd64/Packages.gz"):
        _write_index(primary, relative, INDICES[relative])
    _write_index(ports, "main/binary-arm64/Packages.gz", INDICES["main/binary-arm64/Packages.gz"])
    _write_release(primary, INDICES)
    _write_release(ports, INDICES)


def test_coherent_primary_and_ports_snapshot_publishes(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)

    assert mirror.sync_and_publish(out, "primary", "ports") == 0

    snapshots = list(out.glob("snapshot.*"))
    assert len(snapshots) == 1
    assert (out / "current").resolve() == snapshots[0]
    for relative, content in INDICES.items():
        assert (snapshots[0] / "ubuntu" / "dists" / SUITE / relative).read_bytes() == content


def test_mismatched_ports_index_preserves_current(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    current = (out / "current").resolve()

    _write_index(ports, "main/binary-arm64/Packages.gz", b"newer arm64 packages")
    assert mirror.sync_and_publish(out, "primary", "ports", force=True) == 1

    assert (out / "current").resolve() == current
    assert len(list(out.glob("snapshot.*"))) == 1
    assert not list(out.glob(".staging.*"))
    assert (
        current / "ubuntu" / "dists" / SUITE / "main/binary-arm64/Packages.gz"
    ).read_bytes() == INDICES["main/binary-arm64/Packages.gz"]


def test_validation_falls_back_to_sha256(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    _write_index(root, relative, INDICES[relative])
    _write_release(root, {relative: INDICES[relative]}, algorithm="sha256")

    assert mirror._validate_indices(root) == 1


def test_missing_release_checksum_entry_fails_validation(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    _write_index(root, relative, INDICES[relative])
    _write_release(root, {"main/binary-amd64/Packages.gz": b"unrelated"})

    with pytest.raises(ValueError, match="No SHA512 entry"):
        mirror._validate_indices(root)


def test_malformed_release_fails_validation(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    _write_file(
        root / "dists" / SUITE / "Release",
        f"Date: {DATE}\nSHA512:\n invalid\n".encode(),
    )

    with pytest.raises(ValueError, match="Invalid SHA512 entry"):
        mirror._validate_indices(root)
