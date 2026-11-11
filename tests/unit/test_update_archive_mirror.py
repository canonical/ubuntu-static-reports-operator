# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Unit tests for update-archive-mirror snapshot consistency."""

import hashlib
import importlib.util
import itertools
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
PORTS_DATE = "Tue, 10 Nov 2026  5:16:14 UTC"
OLDER_DATE = "Mon, 09 Nov 2026 21:27:17 UTC"
NEWER_DATE = "Tue, 10 Nov 2026  6:00:00 UTC"


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
def mirror(monkeypatch):
    module = _load_script()
    real_strftime = module.time.strftime
    sequence = itertools.count()

    def distinct_snapshot_times(format_string, value):
        if format_string == "%Y%m%dT%H%M%S":
            return f"20261110T{next(sequence):06d}"
        return real_strftime(format_string, value)

    monkeypatch.setattr(module.time, "strftime", distinct_snapshot_times)
    return module


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
    _write_release(primary, INDICES, date=DATE)
    _write_release(ports, INDICES, date=PORTS_DATE)


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
    stamp = (snapshots[0] / "STAMP").read_text()
    assert "Primary-Release-Date-tenacious: 2026-11-10T02:15:44+00:00" in stamp
    assert "Ports-Release-Date-tenacious: 2026-11-10T05:16:14+00:00" in stamp
    assert "Primary-Release-SHA256-tenacious:" in stamp
    assert "Ports-Release-SHA256-tenacious:" in stamp


def test_empty_ports_release_set_preserves_current(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    current = (out / "current").resolve()

    for release in (ports / "dists").glob("*/Release"):
        release.unlink()
    assert mirror.sync_and_publish(out, "primary", "ports") == 1

    assert (out / "current").resolve() == current
    assert len(list(out.glob("snapshot.*"))) == 1


@pytest.mark.parametrize(
    "ports_suites, expected",
    [
        ({"tenacious"}, "Primary-only suites: \\['noble'\\]"),
        ({"tenacious", "noble", "resolute"}, "ports-only suites: \\['resolute'\\]"),
    ],
)
def test_suite_coverage_rejects_missing_or_extra_suites(mirror, tmp_path, ports_suites, expected):
    primary_root = tmp_path / "primary"
    _write_release(primary_root, INDICES)
    info = mirror._read_releases(primary_root)["tenacious"]
    primary = {"tenacious": info, "noble": info}
    ports = dict.fromkeys(ports_suites, info)

    with pytest.raises(ValueError, match=expected):
        mirror._validate_suite_coverage(primary, ports, True)


def test_suite_coverage_allows_mismatches_when_ports_disabled(mirror, tmp_path):
    primary_root = tmp_path / "primary"
    _write_release(primary_root, INDICES)
    info = mirror._read_releases(primary_root)["tenacious"]

    mirror._validate_suite_coverage({"tenacious": info, "noble": info}, {}, False)


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


@pytest.mark.parametrize("origin", ["primary", "ports"])
def test_older_release_from_either_origin_is_rejected(mirror, monkeypatch, tmp_path, origin):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    current = (out / "current").resolve()

    _write_release(primary if origin == "primary" else ports, INDICES, date=OLDER_DATE)
    assert mirror.sync_and_publish(out, "primary", "ports") == 1

    assert (out / "current").resolve() == current
    assert len(list(out.glob("snapshot.*"))) == 1


def test_rejected_candidate_is_retried_without_rsync_mtime_changes(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    first = (out / "current").resolve()

    validations = 0
    validate = mirror._validate_indices

    def counted_validation(path):
        nonlocal validations
        validations += 1
        return validate(path)

    monkeypatch.setattr(mirror, "_validate_indices", counted_validation)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    assert validations == 0

    _write_release(ports, INDICES, date=NEWER_DATE)
    _write_index(ports, "main/binary-arm64/Packages.gz", b"newer arm64 packages")
    assert mirror.sync_and_publish(out, "primary", "ports") == 1
    assert mirror.sync_and_publish(out, "primary", "ports") == 1
    assert validations == 2
    assert (out / "current").resolve() == first

    _write_index(
        ports,
        "main/binary-arm64/Packages.gz",
        INDICES["main/binary-arm64/Packages.gz"],
    )
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    assert validations == 3
    assert len(list(out.glob("snapshot.*"))) == 2
    assert (out / "current").resolve() != first


def test_old_stamp_requires_validation_and_establishes_baseline(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    first = (out / "current").resolve()
    (first / "STAMP").write_text("Generated: old\nRelease-files: 1\n\n")
    _write_index(ports, "main/binary-arm64/Packages.gz", b"newer arm64 packages")

    assert mirror.sync_and_publish(out, "primary", "ports") == 1
    assert (out / "current").resolve() == first
    assert len(list(out.glob("snapshot.*"))) == 1

    _write_index(
        ports,
        "main/binary-arm64/Packages.gz",
        INDICES["main/binary-arm64/Packages.gz"],
    )
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    assert len(list(out.glob("snapshot.*"))) == 2
    assert (out / "current").resolve() != first


def test_validation_falls_back_to_sha256(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    _write_index(root, relative, INDICES[relative])
    _write_release(root, {relative: INDICES[relative]}, algorithm="sha256")

    assert mirror._validate_indices(root) == 1


@pytest.mark.parametrize("algorithm", ["sha512", "sha256"])
def test_same_size_checksum_mismatch_fails_validation(mirror, tmp_path, algorithm):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    original = INDICES[relative]
    _write_index(root, relative, b"X" * len(original))
    _write_release(root, {relative: original}, algorithm=algorithm)

    with pytest.raises(ValueError, match="Checksum mismatch"):
        mirror._validate_indices(root)


def test_ports_only_suite_fails_validation(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    _write_index(root, relative, INDICES[relative])
    _write_release(root, {relative: INDICES[relative]})
    _write_file(
        root / "dists" / "ports-only/main/binary-arm64/Packages.gz",
        b"ports-only package index",
    )

    with pytest.raises(ValueError, match="No primary Release"):
        mirror._validate_indices(root)


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


def test_malformed_release_date_fails_validation(mirror, tmp_path):
    release = tmp_path / "Release"
    _write_file(release, b"Date: not-a-date\nSHA512:\n")

    with pytest.raises(ValueError, match="Invalid Date field"):
        mirror._read_release(release)
