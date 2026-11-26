# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Outcome tests for the snapshot-consuming report wrappers."""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


def _load_script(name: str):
    path = Path(__file__).parents[2] / "src" / "script" / name
    module_name = name.replace("-", "_")
    spec = importlib.util.spec_from_loader(module_name, SourceFileLoader(module_name, str(path)))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mismatch():
    return _load_script("update-mismatches")


@pytest.fixture
def nbs(monkeypatch):
    launchpadlib = ModuleType("launchpadlib")
    launchpad_module = ModuleType("launchpadlib.launchpad")
    setattr(launchpad_module, "Launchpad", object)
    setattr(launchpadlib, "launchpad", launchpad_module)
    monkeypatch.setitem(sys.modules, "launchpadlib", launchpadlib)
    monkeypatch.setitem(sys.modules, "launchpadlib.launchpad", launchpad_module)
    return _load_script("update-nbs")


def _already_processed_config(root: Path) -> SimpleNamespace:
    snapshot = root / "snapshot.current"
    snapshot.mkdir()
    (snapshot / "STAMP").write_text("Generated: test\n")
    return SimpleNamespace(
        out=root / "out",
        archive_root=snapshot,
        state_file=root / ".state",
        force=False,
    )


def _assert_already_processed(module, monkeypatch, tmp_path):
    cfg = _already_processed_config(tmp_path)
    cfg.state_file.write_text("snapshot.current")
    monkeypatch.setattr(module, "HOME", tmp_path)
    outcomes = []
    monkeypatch.setattr(
        module.health,
        "record_outcome",
        lambda service, outcome, reason="", **kwargs: outcomes.append((outcome, kwargs)),
    )
    generate = (
        module.generate_reports if hasattr(module, "generate_reports") else module.generate_report
    )
    assert generate(cfg) == 0
    assert outcomes == [
        (
            "already_processed",
            {
                "input_snapshot": "snapshot.current",
            },
        )
    ]


def test_mismatches_records_already_processed_snapshot(mismatch, monkeypatch, tmp_path):
    _assert_already_processed(mismatch, monkeypatch, tmp_path)


def test_nbs_records_already_processed_snapshot(nbs, monkeypatch, tmp_path):
    _assert_already_processed(nbs, monkeypatch, tmp_path)


def test_missing_snapshot_records_missing_input(mismatch, monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        out=tmp_path / "out",
        archive_root=tmp_path / "missing-current",
        state_file=tmp_path / ".state",
        force=False,
    )
    monkeypatch.setattr(mismatch, "HOME", tmp_path)
    outcomes = []
    monkeypatch.setattr(
        mismatch.health,
        "record_outcome",
        lambda service, outcome, reason="", **kwargs: outcomes.append(outcome),
    )
    assert mismatch.generate_reports(cfg) == 0
    assert outcomes == ["missing_input"]


def test_mismatches_records_published_snapshot(mismatch, monkeypatch, tmp_path):
    cfg = _already_processed_config(tmp_path)
    cfg.state_file.write_text("")
    cfg.mirror = tmp_path / "mirror"
    cfg.germinate_dir = tmp_path / "germinate"
    cfg.germinate_proposed = tmp_path / "germinate-proposed"
    cfg.package_team_mapping = "mapping.json"
    cfg.devel = "resolute"
    monkeypatch.setattr(mismatch, "HOME", tmp_path)
    monkeypatch.setattr(mismatch, "_report_steps", lambda config: [])
    outcomes = []
    monkeypatch.setattr(
        mismatch.health,
        "record_outcome",
        lambda service, outcome, reason="", **kwargs: outcomes.append((outcome, kwargs)),
    )
    assert mismatch.generate_reports(cfg) == 0
    assert outcomes == [("published", {"input_snapshot": "snapshot.current"})]


def test_nbs_records_published_snapshot(nbs, monkeypatch, tmp_path):
    archive_root = tmp_path / "snapshot.current"
    archive_root.mkdir()
    (archive_root / "STAMP").write_text("Generated: test\n")
    cfg = nbs.Config(
        uat=tmp_path,
        archive_root=archive_root,
        out=tmp_path / "out",
        state_file=tmp_path / ".state",
        distribution="ubuntu",
        devel="resolute",
        force=False,
    )
    monkeypatch.setattr(nbs, "HOME", tmp_path)
    monkeypatch.setattr(nbs, "_stub_missing_arches", lambda path: None)
    monkeypatch.setattr(nbs, "_candidate_nbs", lambda config, path: [])
    monkeypatch.setattr(nbs, "_current_binaries", lambda path, series: set())
    monkeypatch.setattr(nbs, "_build_report", lambda *args: 0)
    outcomes = []
    monkeypatch.setattr(
        nbs.health,
        "record_outcome",
        lambda service, outcome, reason="", **kwargs: outcomes.append((outcome, kwargs)),
    )
    assert nbs.generate_report(cfg) == 0
    assert outcomes == [("published", {"input_snapshot": "snapshot.current"})]


def test_lock_contention_is_recorded(mismatch, monkeypatch, tmp_path):
    cfg = _already_processed_config(tmp_path)
    monkeypatch.setattr(mismatch, "HOME", tmp_path)
    monkeypatch.setattr(mismatch.fcntl, "flock", Mock(side_effect=OSError("locked")))
    outcomes = []
    monkeypatch.setattr(
        mismatch.health,
        "record_outcome",
        lambda service, outcome, reason="", **kwargs: outcomes.append(outcome),
    )
    assert mismatch.generate_reports(cfg) == 0
    assert outcomes == ["locked"]


def test_mismatches_does_not_use_launchpad_credentials(mismatch, monkeypatch):
    monkeypatch.setenv("LP_CREDENTIALS_FILE", "/tmp/must-not-be-propagated")
    environment = mismatch._tool_env(SimpleNamespace(uat=Path("/uat")))
    assert "LP_CREDENTIALS_FILE" not in environment
    unit = (
        Path(__file__).parents[2] / "src" / "systemd" / "update-mismatches.service"
    ).read_text()
    assert "ConditionFileNotEmpty" not in unit
    assert "lp-ubuntu-archive-unprivileged-bot.oauth" not in unit
