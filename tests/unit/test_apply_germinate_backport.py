# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for the temporary Germinate backport helper and its systemd integration."""

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def _load_helper():
    path = Path(__file__).parents[2] / "src" / "script" / "apply-germinate-backport"
    spec = importlib.util.spec_from_loader(
        "apply_germinate_backport", SourceFileLoader("apply_germinate_backport", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    return _load_helper()


def test_git_apply_targets_system_package_directory(helper):
    assert helper.GIT_APPLY == (
        "git",
        "-C",
        "/usr/lib/python3/dist-packages",
        "apply",
        "--include=germinate/germinator.py",
    )


def test_applies_patch_when_forward_check_succeeds(helper, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.main() == 0
    assert [command for command, _ in calls] == [
        [*helper.GIT_APPLY, "--check", str(helper.PATCH)],
        [*helper.GIT_APPLY, str(helper.PATCH)],
    ]
    assert calls[1][1] == {"check": True}


def test_accepts_already_applied_patch_without_writing(helper, monkeypatch):
    calls = []
    results = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="forward failed"),
            subprocess.CompletedProcess([], 0, stdout="reverse succeeded"),
        ]
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.main() == 0
    assert calls == [
        [*helper.GIT_APPLY, "--check", str(helper.PATCH)],
        [*helper.GIT_APPLY, "--check", "--reverse", str(helper.PATCH)],
    ]


def test_rejects_incompatible_source_without_applying(helper, monkeypatch, caplog):
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="mismatch"),
    )

    assert helper.main() == 1
    assert helper.BACKPORT_URL in caplog.text
    assert "Forward check:\nmismatch" in caplog.text
    assert "Already-installed check:\nmismatch" in caplog.text


def test_update_germinate_applies_backport_as_root_before_running_as_ubuntu():
    unit = Path(__file__).parents[2] / "src" / "systemd" / "update-germinate.service"
    content = unit.read_text()

    assert "ExecStartPre=!/usr/bin/apply-germinate-backport" in content
    assert "User=ubuntu" in content
    assert "ExecStart=/usr/bin/update-germinate" in content
