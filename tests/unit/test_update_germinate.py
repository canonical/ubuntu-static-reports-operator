# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Unit tests for update-germinate: notify integration and snapshot views."""

import importlib.util
import os
import socket
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def _load_update_germinate():
    path = Path(__file__).parents[2] / "src" / "script" / "update-germinate"
    spec = importlib.util.spec_from_loader(
        "update_germinate", SourceFileLoader("update_germinate", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def update_germinate():
    return _load_update_germinate()


def _tree_file(flavours: Path, flavour: str, series: str, pocket: str, base: str) -> Path:
    """Create a file in the canonical tree, named <base>_<flavour>_<series>_<arch>."""
    path = flavours / flavour / series / pocket / f"{base}_{flavour}_{series}_amd64"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{flavour} {series} {pocket} {base}")
    return path


@pytest.fixture
def staging(tmp_path):
    """Build a staging dir with a minimal two-flavour/two-pocket flavours tree."""
    flavours = tmp_path / "flavours"
    flavours.mkdir()
    (flavours / "germinate.output").write_text("germinate log")
    for pocket in ("release", "proposed"):
        _tree_file(flavours, "ubuntu", "resolute", pocket, "structure")
        _tree_file(flavours, "ubuntu", "resolute", pocket, "desktop")
        _tree_file(flavours, "kubuntu", "resolute", pocket, "desktop")
    return tmp_path


def test_flat_view_links_every_flavour_per_pocket(update_germinate, staging):
    update_germinate._build_views(staging)

    for pocket in ("release", "proposed"):
        flat = staging / "flat" / pocket
        assert {p.name for p in flat.iterdir()} == {
            f"{base}_{flavour}_resolute_amd64"
            for base, flavour in (
                ("structure", "ubuntu"),
                ("desktop", "ubuntu"),
                ("desktop", "kubuntu"),
            )
        }
        tree_file = staging / "flavours" / "ubuntu" / "resolute" / pocket
        tree_file = tree_file / "structure_ubuntu_resolute_amd64"
        assert os.stat(flat / tree_file.name).st_ino == os.stat(tree_file).st_ino


def test_legacy_view_strips_suffix_and_hardlinks(update_germinate, staging):
    update_germinate._build_views(staging)

    legacy = staging / "germinate-output" / "release"
    tree_file = staging / "flavours" / "ubuntu" / "resolute" / "release"
    tree_file /= "desktop_ubuntu_resolute_amd64"
    legacy_file = legacy / "ubuntu.resolute" / "desktop"
    assert legacy_file.read_text() == tree_file.read_text()
    assert os.stat(legacy_file).st_ino == os.stat(tree_file).st_ino
    assert (legacy / "kubuntu.resolute" / "desktop").exists()


def test_legacy_view_covers_proposed(update_germinate, staging):
    update_germinate._build_views(staging)

    legacy = staging / "germinate-output" / "proposed"
    tree_file = staging / "flavours" / "ubuntu" / "resolute" / "proposed"
    tree_file /= "desktop_ubuntu_resolute_amd64"
    legacy_file = legacy / "ubuntu.resolute" / "desktop"
    assert legacy_file.read_text() == tree_file.read_text()
    assert os.stat(legacy_file).st_ino == os.stat(tree_file).st_ino


def test_legacy_view_links_germinate_log(update_germinate, staging):
    update_germinate._build_views(staging)

    log = staging / "flavours" / "germinate.output"
    legacy_log = staging / "germinate-output" / "release" / "ubuntu.resolute" / "_germinate_output"
    assert os.stat(legacy_log).st_ino == os.stat(log).st_ino


def test_views_are_rebuilt_idempotently(update_germinate, staging):
    update_germinate._build_views(staging)
    update_germinate._build_views(staging)

    flat = staging / "flat" / "release"
    assert len(list(flat.iterdir())) == 3


def test_hardlink_raises_on_conflicting_dest(update_germinate, staging):
    src = staging / "flavours" / "germinate.output"
    dest = staging / "flat"
    dest.write_text("different content")

    with pytest.raises(FileExistsError):
        update_germinate._hardlink(src, dest)


def _bound_notify_socket(monkeypatch, tmp_path):
    """Bind a unix datagram socket and point NOTIFY_SOCKET at it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path = tmp_path / "notify.sock"
    sock.bind(str(path))
    sock.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    return sock


def test_sd_notify_sends_datagram(update_germinate, monkeypatch, tmp_path):
    sock = _bound_notify_socket(monkeypatch, tmp_path)
    update_germinate._sd_notify("READY=1")
    assert sock.recv(4096) == b"READY=1"


def test_sd_notify_is_noop_without_socket(update_germinate, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    update_germinate._sd_notify("READY=1")  # must not raise


def test_hardlink_raises_on_symlink_dest(update_germinate, staging):
    src = staging / "flavours" / "germinate.output"
    dest = staging / "flat"
    dest.symlink_to("/nonexistent/target")

    with pytest.raises(FileExistsError):
        update_germinate._hardlink(src, dest)
