# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Unit tests for the seeded-in-ubuntu reverse package indexer."""

import gzip
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def _load_indexer():
    path = Path(__file__).parents[2] / "src" / "script" / "seeded-in-ubuntu-indexer"
    spec = importlib.util.spec_from_loader(
        "seeded_in_ubuntu_indexer", SourceFileLoader("seeded_in_ubuntu_indexer", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def indexer():
    return _load_indexer()


def test_default_archive_root_uses_srv(indexer, monkeypatch):
    monkeypatch.delenv("ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("MIRROR_DIR", raising=False)
    args = indexer.parse_args([])
    assert args.archive_root == Path("/srv/staticreports/germinate/current")


def _tree_file(
    flavours: Path, flavour: str, series: str, arch: str, seed: str, packages: list[str]
) -> Path:
    """Create a germinate package list <seed>_<flavour>_<series>_<arch> in the snapshot tree."""
    path = flavours / flavour / series / "release" / f"{seed}_{flavour}_{series}_{arch}"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Package | Source", "--------|--------"]
    for pkg in packages:
        lines.append(f"{pkg} | {pkg}")
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_release(staging: Path, series: str, architectures: tuple[str, ...]) -> None:
    release = staging / "germinate" / "snapshot.20250101T000000Z"
    release = release / "archive" / "ubuntu" / "dists" / series / "Release"
    release.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f" deadbeef 1 main/binary-{architecture}/Packages.gz" for architecture in architectures
    ]
    release.write_text("\n".join(lines) + "\n")


@pytest.fixture
def staging(tmp_path):
    """Build a germinate snapshot (flavours + flat views) and the seed checkouts."""
    snapshot = tmp_path / "germinate" / "snapshot.20250101T000000Z"
    flavours = snapshot / "flavours"
    flat_release = snapshot / "flat" / "release"
    flat_release.mkdir(parents=True)

    # The flat view carries one file per arch so the archs are discoverable.
    for arch in ("amd64", "arm64"):
        (flat_release / f"desktop_ubuntu_resolute_{arch}").write_text("flat")

    _tree_file(flavours, "ubuntu", "resolute", "amd64", "desktop", ["adduser", "bash"])
    _tree_file(flavours, "ubuntu", "resolute", "amd64", "server", ["nginx"])
    _tree_file(flavours, "ubuntu", "resolute", "arm64", "desktop", ["ubuntu-x13s-settings"])
    _tree_file(flavours, "kubuntu", "resolute", "amd64", "desktop", ["kde-full", "bash"])
    # Files germinate emits that must not be treated as seeds.
    _tree_file(flavours, "ubuntu", "resolute", "amd64", "structure", [])
    seed_depends = flavours / "ubuntu" / "resolute" / "release"
    (seed_depends / "desktop.depends_ubuntu_resolute_amd64").write_text(
        "Package | Source\n--------|--------\nnot-seeded | No reason to seed that"
    )

    seeds = tmp_path / "seeds"
    for name in ("ubuntu.resolute", "kubuntu.resolute", "i386.resolute", "platform.resolute"):
        (seeds / name).mkdir(parents=True)
    _write_release(tmp_path, "resolute", ("amd64", "arm64"))

    return tmp_path


def _config(indexer, staging):
    return indexer.parse_args(
        [
            "--archive-root",
            str(staging / "germinate" / "snapshot.20250101T000000Z"),
            "--output-dir",
            str(staging / "out"),
            "--seeds-directory",
            str(staging / "seeds"),
            "--state-file",
            str(staging / ".state"),
        ]
    )


def test_discover_flavors_skips_platform_and_i386(indexer, staging):
    flavors = indexer.discover_flavors(staging / "seeds", "resolute")
    assert flavors == ["kubuntu", "ubuntu"]


def test_parse_package_list_skips_non_germinate_output(indexer, tmp_path):
    malformed = tmp_path / "malformed"
    malformed.write_text("not a germinate list\n")
    assert indexer.parse_package_list(malformed) == set()


def test_build_index_maps_packages_to_flavors_and_seeds(indexer, staging):
    index = indexer.build_index(
        "resolute", "amd64", staging / "seeds", staging / "germinate" / "snapshot.20250101T000000Z"
    )
    assert index == {
        "adduser": {"ubuntu": ["desktop"]},
        "bash": {"ubuntu": ["desktop"], "kubuntu": ["desktop"]},
        "nginx": {"ubuntu": ["server"]},
        "kde-full": {"kubuntu": ["desktop"]},
    }
    index = indexer.build_index(
        "resolute", "arm64", staging / "seeds", staging / "germinate" / "snapshot.20250101T000000Z"
    )
    assert index == {
        "ubuntu-x13s-settings": {"ubuntu": ["desktop"]},
    }


def test_generate_indexes_writes_gzipped_json_per_series_arch(indexer, staging):
    indexer.generate_indexes(_config(indexer, staging))

    out = staging / "out"
    assert {p.name for p in out.iterdir()} == {
        "seeded-resolute-amd64.json.gz",
        "seeded-resolute-arm64.json.gz",
    }

    with gzip.open(out / "seeded-resolute-amd64.json.gz", "rt", encoding="utf-8") as f:
        index = json.load(f)
    assert index == {
        "adduser": {"ubuntu": ["desktop"]},
        "bash": {"ubuntu": ["desktop"], "kubuntu": ["desktop"]},
        "nginx": {"ubuntu": ["server"]},
        "kde-full": {"kubuntu": ["desktop"]},
    }


def test_generate_indexes_records_processed_snapshot(indexer, staging):
    indexer.generate_indexes(_config(indexer, staging))
    assert (staging / ".state").read_text() == "snapshot.20250101T000000Z"


def test_generate_indexes_records_written_and_skipped_cells(indexer, staging, monkeypatch):
    health_dir = staging / "health"
    monkeypatch.setenv("STATIC_REPORTS_HEALTH_DIR", str(health_dir))
    monkeypatch.setenv("INVOCATION_ID", "first-run")
    cfg = _config(indexer, staging)
    indexer.generate_indexes(cfg)

    record_path = health_dir / "seeded-in-ubuntu-indexer.json"
    first = json.loads(record_path.read_text())
    assert first["outcome"] == "published"
    assert first["input_snapshot"] == "snapshot.20250101T000000Z"
    assert first["details"]["complete"] is True
    assert len(first["details"]["written"]) == 2
    assert first["details"]["skipped"] == [
        {
            "series": "resolute",
            "architecture": "arm64",
            "flavor": "kubuntu",
            "reason": "no germinate output",
            "expected": True,
        }
    ]

    monkeypatch.setenv("INVOCATION_ID", "second-run")
    indexer.generate_indexes(cfg)
    second = json.loads(record_path.read_text())
    assert second["outcome"] == "already_processed"
    assert second["details"] == first["details"]
    assert second["last_publication_at"] == first["last_publication_at"]


def test_expected_archs_are_derived_from_release_metadata(indexer, staging):
    _write_release(staging, "trusty", ("amd64",))
    snapshot = staging / "germinate" / "snapshot.20250101T000000Z"

    assert indexer._expected_archs(snapshot, "resolute") == (True, {"amd64", "arm64"})
    assert indexer._expected_archs(snapshot, "trusty") == (True, {"amd64"})
    assert indexer._expected_archs(snapshot, "xenial") == (False, set())


def test_unsupported_series_does_not_mark_indexes_incomplete(indexer, staging, monkeypatch):
    (staging / "seeds" / "ubuntu.trusty").mkdir()
    monkeypatch.setenv("STATIC_REPORTS_HEALTH_DIR", str(staging / "health"))

    indexer.generate_indexes(_config(indexer, staging))

    record = json.loads((staging / "health" / "seeded-in-ubuntu-indexer.json").read_text())
    assert record["details"]["complete"] is True
    assert {
        "series": "trusty",
        "reason": "unsupported series",
        "expected": True,
    } in record["details"]["skipped"]


def test_missing_expected_cell_marks_indexes_incomplete(indexer, staging, monkeypatch):
    monkeypatch.setenv("STATIC_REPORTS_HEALTH_DIR", str(staging / "health"))
    original_build_index = indexer.build_index

    def missing_arm64(series, arch, seed_source, germinate_dir, skipped=None):
        if arch == "arm64":
            return {}
        return original_build_index(series, arch, seed_source, germinate_dir, skipped)

    monkeypatch.setattr(indexer, "build_index", missing_arm64)
    indexer.generate_indexes(_config(indexer, staging))

    record = json.loads((staging / "health" / "seeded-in-ubuntu-indexer.json").read_text())
    assert record["details"]["complete"] is False
    assert any(
        skip.get("reason") == "no data" and skip.get("expected") is False
        for skip in record["details"]["skipped"]
    )


def test_missing_release_metadata_marks_indexes_incomplete(indexer, staging, monkeypatch):
    (staging / "seeds" / "ubuntu.trusty").mkdir()
    (
        staging
        / "germinate"
        / "snapshot.20250101T000000Z"
        / "archive"
        / "ubuntu"
        / "dists"
        / "trusty"
    ).mkdir(parents=True)
    monkeypatch.setenv("STATIC_REPORTS_HEALTH_DIR", str(staging / "health"))

    indexer.generate_indexes(_config(indexer, staging))

    record = json.loads((staging / "health" / "seeded-in-ubuntu-indexer.json").read_text())
    assert record["details"]["complete"] is False
    assert {
        "series": "trusty",
        "reason": "missing Release architecture metadata",
        "expected": False,
    } in record["details"]["skipped"]


def test_generate_indexes_skips_already_processed_snapshot(indexer, staging):
    cfg = _config(indexer, staging)
    indexer.generate_indexes(cfg)
    skipped = staging / "out" / "seeded-resolute-amd64.json.gz"
    skipped.unlink()
    indexer.generate_indexes(cfg)
    assert not skipped.exists()
