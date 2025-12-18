# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for `src/staticreports.py`.

These tests mock external side-effects (apt, systemd, os, shutil, and
pathlib writes) so they can run as unit tests without touching the host.
"""

from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import Mock, patch

import pytest

import staticreports
from staticreports import StaticReports


def test__install_packages_success(monkeypatch):
    called = []

    def fake_update():
        called.append("update")

    def fake_add_package(pkg):
        called.append(pkg)

    monkeypatch.setattr(staticreports.apt, "update", fake_update)
    monkeypatch.setattr(staticreports.apt, "add_package", fake_add_package)

    sr = staticreports.StaticReports()
    sr._install_packages()

    # first call is update, then all packages were requested
    assert called[0] == "update"
    assert set(called[1:]) == set(staticreports.PACKAGES)


def test__install_packages_update_fails(monkeypatch):
    monkeypatch.setattr(
        staticreports.apt, "update", Mock(side_effect=CalledProcessError(1, "apt"))
    )

    sr = staticreports.StaticReports()
    with pytest.raises(CalledProcessError):
        sr._install_packages()


def test_install_creates_dirs_and_copies(monkeypatch):
    # avoid performing package operations
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    # avoid invoking real git during this test; record calls
    run_mock = Mock()
    monkeypatch.setattr(staticreports, "run", run_mock)

    ops = []

    monkeypatch.setattr(
        staticreports.os, "makedirs", lambda dname, exist_ok=True: ops.append(("makedirs", dname))
    )
    monkeypatch.setattr(
        staticreports.shutil, "chown", lambda path, u, g: ops.append(("chown", path, u, g))
    )
    monkeypatch.setattr(
        staticreports.shutil, "copy", lambda src, dst: ops.append(("copy", src, dst))
    )
    monkeypatch.setattr(
        staticreports.Path,
        "unlink",
        lambda self, missing_ok=True: ops.append(("unlink", str(self))),
    )

    sr = staticreports.StaticReports()
    sr.install()

    # directories created and chown called
    for dname, duser, dgroup in staticreports.SRV_DIRS:
        assert ("makedirs", dname) in ops
        if duser is not None:
            assert ("chown", dname, duser, dgroup) in ops

    # files copied (script files and nginx config)
    assert ("copy", "src/script/update-sync-blocklist", "/usr/bin") in ops
    assert ("copy", "src/script/update-seeds", "/usr/bin") in ops
    assert ("copy", "src/nginx/staticreports.conf", staticreports.NGINX_SITE_CONFIG_PATH) in ops

    # ensure git/repo handling was invoked
    assert run_mock.called


def test_install_copy_failure_propagates(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)

    # avoid invoking real git during this test
    monkeypatch.setattr(staticreports, "run", lambda *a, **k: Mock())

    def bad_copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(staticreports.shutil, "copy", bad_copy)

    sr = staticreports.StaticReports()
    with pytest.raises(OSError):
        sr.install()


def test_start_success_and_failure(monkeypatch):
    calls = []

    monkeypatch.setattr(
        staticreports.systemd, "service_restart", lambda *args: calls.append(("restart",) + args)
    )
    monkeypatch.setattr(
        staticreports.systemd, "service_start", lambda *args: calls.append(("start",) + args)
    )

    sr = staticreports.StaticReports()
    sr.start()

    assert ("restart", "nginx") in calls
    for svc in staticreports.UBUNTU_STATIC_REPORT_SERVICES:
        # ensure we call service_start with the unit and the --no-block flag
        assert any(
            call
            for call in calls
            if call[0] == "start" and svc + ".service" in call and "--no-block" in call
        )

    # now make service_start raise
    def bad_start(*args):
        raise CalledProcessError(1, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    with pytest.raises(CalledProcessError):
        sr.start()


def test_refresh_report_success_and_failure(monkeypatch):
    starts = []
    monkeypatch.setattr(staticreports.systemd, "service_start", lambda *args: starts.append(args))

    sr = staticreports.StaticReports()
    sr.refresh_report()
    for svc in staticreports.UBUNTU_STATIC_REPORT_SERVICES:
        # refresh_report may or may not use --no-block; accept either form
        assert any(call for call in starts if svc + ".service" in call)

    # failure case
    def bad_start(*args):
        raise CalledProcessError(2, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    with pytest.raises(CalledProcessError):
        sr.refresh_report()


def test_setup_systemd_unit_writes_units_and_enables(monkeypatch):
    # simulate proxies coming from environment
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "https://secure.example:8443")

    sr = staticreports.StaticReports()

    # return different content depending on suffix
    def fake_read_text(self):
        return "[Service]\nExecStart=/bin/true" if self.suffix == ".service" else "[Timer]"

    written = {}

    def fake_write_text(self, text):
        written[str(self)] = text

    monkeypatch.setattr(staticreports.Path, "read_text", fake_read_text)
    monkeypatch.setattr(staticreports.Path, "write_text", fake_write_text)
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=False, exist_ok=False: None
    )

    enabled = []

    def fake_enable(*args, **kwargs):
        enabled.append(args)

    monkeypatch.setattr(staticreports.systemd, "service_enable", fake_enable)

    sr.setup_systemd_unit("update-seeds")

    svc_path = "/etc/systemd/system/update-seeds.service"
    timer_path = "/etc/systemd/system/update-seeds.timer"
    assert svc_path in written
    assert timer_path in written
    # ensure proxy Environment lines were appended
    assert "Environment=HTTP_PROXY=http://proxy.example:8080" in written[svc_path]
    assert "Environment=HTTPS_PROXY=https://secure.example:8443" in written[svc_path]
    assert enabled and enabled[0][0] == "--now"


def test_setup_systemd_units_calls_each(monkeypatch):
    called = []
    monkeypatch.setattr(
        staticreports.StaticReports, "setup_systemd_unit", lambda self, s: called.append(s)
    )
    sr = staticreports.StaticReports()
    sr.setup_systemd_units()
    assert set(called) == set(staticreports.UBUNTU_STATIC_REPORT_SERVICES)


def test__install_packages_package_not_found(monkeypatch):
    # make update succeed but adding package raise PackageNotFoundError
    monkeypatch.setattr(staticreports.apt, "update", lambda: None)

    def bad_add(pkg):
        raise staticreports.PackageNotFoundError(pkg)

    monkeypatch.setattr(staticreports.apt, "add_package", bad_add)

    sr = staticreports.StaticReports()
    with pytest.raises(staticreports.PackageNotFoundError):
        sr._install_packages()


def test__install_packages_package_error(monkeypatch):
    monkeypatch.setattr(staticreports.apt, "update", lambda: None)

    def bad_add(pkg):
        raise staticreports.PackageError("install failed")

    monkeypatch.setattr(staticreports.apt, "add_package", bad_add)

    sr = staticreports.StaticReports()
    with pytest.raises(staticreports.PackageError):
        sr._install_packages()


def test_install_directory_creation_failure(monkeypatch):
    # ensure package install is skipped
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    # make os.makedirs raise OSError
    def boom(dname, exist_ok=True):
        raise OSError("no space")

    monkeypatch.setattr(staticreports.os, "makedirs", boom)

    sr = staticreports.StaticReports()
    with pytest.raises(OSError):
        sr.install()


def test_configure_logs_url(caplog):
    sr = staticreports.StaticReports()
    caplog.clear()
    with caplog.at_level("DEBUG"):
        sr.configure_url("http://example.local:80")
    assert "The url in use is http://example.local:80" in caplog.text


def test_setup_systemd_unit_enable_failure(monkeypatch):
    # prepare minimal read_text/write_text to avoid file IO
    monkeypatch.setattr(staticreports.Path, "read_text", lambda self: "[Service]")
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, t: None)
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )

    # set proxies so proxy lines are built
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://p:1")

    def bad_enable(*args, **kwargs):
        raise CalledProcessError(3, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_enable", bad_enable)

    sr = staticreports.StaticReports()
    with pytest.raises(CalledProcessError):
        sr.setup_systemd_unit("update-seeds")


def test_refresh_report_logs_stdout_on_failure(monkeypatch, caplog):
    # raise CalledProcessError with stdout set to verify logging
    def bad_start(name):
        e = CalledProcessError(5, "systemctl")
        # CalledProcessError in this environment doesn't accept `stdout=` kwarg;
        # ensure the attribute exists so the code under test can log it.
        e.stdout = b"failed output"
        raise e

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    sr = staticreports.StaticReports()
    caplog.clear()
    with caplog.at_level("DEBUG"):
        with pytest.raises(CalledProcessError):
            sr.refresh_report()
    assert "failed output" in caplog.text


def test_install_triggers_git_clone_and_copies(monkeypatch, tmp_path):
    # Ensure install doesn't touch system dirs
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)

    ops = []
    monkeypatch.setattr(
        staticreports.shutil, "copy", lambda src, dst: ops.append(("copy", str(src), str(dst)))
    )

    # Use tmp_path for the repository target to avoid touching project tree
    repo_target = tmp_path / "ubuntu-archive-tools"
    monkeypatch.setattr(
        staticreports,
        "REPO_URLS",
        [("https://git.launchpad.net/ubuntu-archive-tools", "main", repo_target)],
    )

    run_calls = []

    def fake_run(cmd, **kwargs):
        # record the command that would be executed (no filesystem changes)
        run_calls.append(cmd)
        return Mock()

    monkeypatch.setattr(staticreports, "run", fake_run)

    # avoid package installs
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    sr = staticreports.StaticReports()
    sr.install()

    # verify a git clone was requested and the target equals our tmp_path target
    assert any(
        isinstance(cmd, (list, tuple)) and "clone" in cmd and cmd[-1] == repo_target
        for cmd in run_calls
    ), f"expected clone to {repo_target}, got {run_calls}"


def test_install_git_clone_failure_raises(monkeypatch):
    # Ensure install doesn't touch system dirs
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)

    # Make run raise to simulate git failure
    def bad_run(cmd, **kwargs):
        raise CalledProcessError(2, "git")

    monkeypatch.setattr(staticreports, "run", bad_run)
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    sr = staticreports.StaticReports()
    with pytest.raises(CalledProcessError):
        sr.install()


def test_staticreports_init_with_proxies(monkeypatch):
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "https://secure.example:3129")
    sr = StaticReports()
    assert "http" in sr.proxies
    assert "https" in sr.proxies
    assert sr.env.get("HTTP_PROXY") == "http://proxy.example:3128"
    assert sr.env.get("HTTPS_PROXY") == "https://secure.example:3129"


@patch("staticreports.pathops.LocalPath")
def test_configure_lpoauthkey_exceptions(localpathmock):
    # parent exists but write_text raises FileNotFoundError
    inst = localpathmock.return_value
    inst.parent = Path("/nonexistent/home/ubuntu")
    inst.write_text.side_effect = FileNotFoundError()
    sr = StaticReports()
    # avoid attempting to create /nonexistent on the test host
    import os as _os

    _os_makedirs = _os.makedirs
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs

    # raise LookupError
    inst.write_text.side_effect = LookupError()
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs

    # raise PermissionError
    inst.write_text.side_effect = PermissionError()
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs
