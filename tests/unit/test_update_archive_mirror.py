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


@pytest.mark.parametrize("ports_enabled", [True, False])
def test_valid_release_without_indices_fails_validation(mirror, tmp_path, ports_enabled):
    root = tmp_path / "ubuntu"
    _write_release(root, {"main/i18n/Translation-en": b"translation metadata"})

    with pytest.raises(ValueError, match="No Packages/Sources index files"):
        mirror._validate_indices(root, ports_enabled=ports_enabled)


@pytest.mark.parametrize(
    "origin, relative",
    [
        ("primary", "main/source/Sources.gz"),
        ("ports", "main/binary-arm64/Packages.gz"),
    ],
)
def test_missing_required_index_preserves_current(mirror, monkeypatch, tmp_path, origin, relative):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    current = (out / "current").resolve()

    (primary if origin == "primary" else ports).joinpath("dists", SUITE, relative).unlink()
    assert mirror.sync_and_publish(out, "primary", "ports", force=True) == 1

    assert (out / "current").resolve() == current
    assert len(list(out.glob("snapshot.*"))) == 1


@pytest.mark.parametrize("ports_enabled", [True, False])
def test_listed_but_unexported_variants_are_not_required(mirror, tmp_path, ports_enabled):
    root = tmp_path / "ubuntu"
    exported = {
        "main/source/Sources.gz": INDICES["main/source/Sources.gz"],
        "main/binary-amd64/Packages.gz": INDICES["main/binary-amd64/Packages.gz"],
    }
    for relative, content in exported.items():
        _write_index(root, relative, content)
    listed = {
        **exported,
        "main/source/Sources": b"unexported sources",
        "main/source/Sources.xz": b"unexported sources.xz",
        "main/binary-amd64/Packages": b"unexported packages",
        "main/binary-amd64/Packages.xz": b"unexported packages.xz",
    }
    _write_release(root, listed)

    assert mirror._validate_indices(root, ports_enabled=ports_enabled) == len(exported)


def test_other_index_formats_do_not_replace_gzip(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    exported = {
        "main/source/Sources.gz": INDICES["main/source/Sources.gz"],
        "main/binary-amd64/Packages": b"uncompressed packages",
        "main/binary-amd64/Packages.xz": b"xz packages",
    }
    for relative, content in exported.items():
        _write_index(root, relative, content)
    _write_release(root, {**exported, "main/binary-amd64/Packages.gz": b"gzip packages"})

    with pytest.raises(ValueError, match=r"Missing index .*binary-amd64/Packages\.gz"):
        mirror._validate_indices(root)


def test_primary_only_mode_does_not_require_ports_indices(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    for relative in ("main/source/Sources.gz", "main/binary-amd64/Packages.gz"):
        _write_index(primary, relative, INDICES[relative])
    _write_release(primary, INDICES)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)

    assert mirror.sync_and_publish(out, "primary", "") == 0

    snapshots = list(out.glob("snapshot.*"))
    assert len(snapshots) == 1
    assert not (
        snapshots[0] / "ubuntu" / "dists" / SUITE / "main/binary-arm64/Packages.gz"
    ).exists()


@pytest.mark.parametrize(
    "missing",
    [
        "main/source/Sources.gz",
        "main/binary-amd64/Packages.gz",
        "main/binary-amd64v3/Packages.gz",
        "main/binary-i386/Packages.gz",
    ],
)
def test_primary_only_missing_required_index_preserves_current(
    mirror, monkeypatch, tmp_path, missing
):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    primary_indices = {
        **INDICES,
        "main/binary-amd64v3/Packages.gz": b"amd64v3 packages",
        "main/binary-i386/Packages.gz": b"i386 packages",
    }
    for relative, content in primary_indices.items():
        if "binary-arm64" not in relative:
            _write_index(primary, relative, content)
    _write_release(primary, primary_indices)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "") == 0
    current = (out / "current").resolve()

    (primary / "dists" / SUITE / missing).unlink()
    assert mirror.sync_and_publish(out, "primary", "", force=True) == 1

    assert (out / "current").resolve() == current
    assert len(list(out.glob("snapshot.*"))) == 1


def test_disabled_ports_ignore_stale_metadata_and_indices(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    first = (out / "current").resolve()

    _write_file(ports / "dists" / SUITE / "Release", b"malformed stale metadata")
    _write_index(ports, "main/binary-arm64/Packages.gz", b"corrupt stale packages")
    assert mirror.sync_and_publish(out, "primary", "") == 0

    second = (out / "current").resolve()
    assert second != first
    assert not (second / "ubuntu" / "dists" / SUITE / "main/binary-arm64/Packages.gz").exists()
    assert "Ports-Release" not in (second / "STAMP").read_text()

    assert mirror.sync_and_publish(out, "primary", "") == 0
    assert len(list(out.glob("snapshot.*"))) == 2


def test_unlisted_indices_are_not_required(mirror, tmp_path):
    root = tmp_path / "ubuntu"
    relative = "main/source/Sources.gz"
    _write_index(root, relative, INDICES[relative])
    _write_release(root, {relative: INDICES[relative]})

    assert mirror._validate_indices(root) == 1


def test_content_hashes_decide_cross_origin_coherence(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    first = (out / "current").resolve()

    # Primary advances while ports remains at an older but non-regressed date.
    updated = dict(INDICES)
    updated["main/binary-arm64/Packages.gz"] = b"ARM64 packages"
    _write_release(primary, updated, date=NEWER_DATE)
    assert mirror.sync_and_publish(out, "primary", "ports") == 1
    assert (out / "current").resolve() == first

    # The same progression is valid when the selected content matches the new manifest.
    _write_release(primary, INDICES, date=NEWER_DATE)
    assert mirror.sync_and_publish(out, "primary", "ports") == 0
    assert len(list(out.glob("snapshot.*"))) == 2
    assert (out / "current").resolve() != first


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

    def counted_validation(path, **kwargs):
        nonlocal validations
        validations += 1
        return validate(path, **kwargs)

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
    _write_index(root, "main/binary-amd64/Packages.gz", b"unrelated")
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


def test_stale_unlisted_ports_indices_are_not_merged(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    _write_index(ports, "main/binary-powerpc/Packages.bz2", b"dropped architecture")
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: None)

    assert mirror.sync_and_publish(out, "primary", "ports") == 0

    dists = (out / "current").resolve() / "ubuntu" / "dists" / SUITE
    assert (dists / "main/binary-arm64/Packages.gz").exists()
    assert not (dists / "main/binary-powerpc").exists()


def test_lagging_ports_mirror_is_resynced_until_coherent(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    _write_index(ports, "main/binary-arm64/Packages.gz", b"previous arm64 packages")
    syncs = 0

    def ports_catch_up_on_second_sync(*args):
        nonlocal syncs
        syncs += 1
        if syncs == 2:
            _write_index(
                ports, "main/binary-arm64/Packages.gz", INDICES["main/binary-arm64/Packages.gz"]
            )

    sleeps = []
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", ports_catch_up_on_second_sync)
    monkeypatch.setattr(mirror.time, "sleep", sleeps.append)

    assert mirror.sync_and_publish(out, "primary", "ports", attempts=3, retry_delay=60) == 0

    assert syncs == 2
    assert sleeps == [60]
    assert len(list(out.glob("snapshot.*"))) == 1


def test_retries_are_bounded(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    _write_index(ports, "main/binary-arm64/Packages.gz", b"never coherent")
    syncs = []
    sleeps = []
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: syncs.append(args))
    monkeypatch.setattr(mirror.time, "sleep", sleeps.append)

    assert mirror.sync_and_publish(out, "primary", "ports", attempts=3, retry_delay=5) == 1

    assert len(syncs) == 3
    assert sleeps == [5, 5]
    assert not list(out.glob("snapshot.*"))
    assert not list(out.glob(".staging.*"))


def test_retries_stop_when_budget_is_exhausted(mirror, monkeypatch, tmp_path):
    out = tmp_path / "archive"
    primary = out / "work" / "ubuntu"
    ports = out / "work" / "ubuntu-ports"
    _populate_work_trees(primary, ports)
    _write_index(ports, "main/binary-arm64/Packages.gz", b"never coherent")
    clock = itertools.count(step=mirror.RETRY_BUDGET // 2)
    syncs = []
    monkeypatch.setattr(mirror, "HOME", tmp_path)
    monkeypatch.setattr(mirror, "sync_archive", lambda *args: syncs.append(args))
    monkeypatch.setattr(mirror.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mirror.time, "sleep", lambda seconds: None)

    assert mirror.sync_and_publish(out, "primary", "ports", attempts=5, retry_delay=1) == 1

    assert len(syncs) == 2


def test_negative_retry_delay_is_rejected(mirror, monkeypatch):
    with pytest.raises(SystemExit):
        mirror.parse_args(["--retry-delay", "-1"])
    monkeypatch.setenv("SYNC_RETRY_DELAY", "-1")
    with pytest.raises(SystemExit):
        mirror.parse_args([])
