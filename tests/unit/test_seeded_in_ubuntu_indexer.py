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


def test_generate_indexes_skips_already_processed_snapshot(indexer, staging):
    cfg = _config(indexer, staging)
    indexer.generate_indexes(cfg)
    skipped = staging / "out" / "seeded-resolute-amd64.json.gz"
    skipped.unlink()
    indexer.generate_indexes(cfg)
    assert not skipped.exists()
