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


def test_install_packages_calls_apt_update_before_adding_packages(monkeypatch):
    called = []

    def fake_update():
        called.append("update")

    def fake_add_package(pkg):
        called.append(pkg)

    monkeypatch.setattr(staticreports.apt, "update", fake_update)
    monkeypatch.setattr(staticreports.apt, "add_package", fake_add_package)
    sr = staticreports.StaticReports()

    sr._install_packages()

    assert called[0] == "update"
    assert set(called[1:]) == set(staticreports.PACKAGES)


def test_install_packages_raises_when_apt_update_fails(monkeypatch):
    monkeypatch.setattr(
        staticreports.apt, "update", Mock(side_effect=CalledProcessError(1, "apt"))
    )
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr._install_packages()


def test_install_creates_srv_directories_and_copies_scripts(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    run_mock = Mock()
    monkeypatch.setattr(staticreports, "run", run_mock)

    ops = []

    monkeypatch.setattr(
        staticreports.os,
        "makedirs",
        lambda dir_path, exist_ok=True: ops.append(("makedirs", dir_path)),
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

    for dir_path, dir_user, dir_group in staticreports.SRV_DIRS:
        assert ("makedirs", dir_path) in ops
        if dir_user is not None:
            assert ("chown", dir_path, dir_user, dir_group) in ops

    assert ("copy", "src/script/update-sync-blocklist", "/usr/bin") in ops
    assert ("copy", "src/script/update-seeds", "/usr/bin") in ops
    assert (
        "copy",
        "src/nginx/staticreports.conf",
        staticreports.NGINX_SITE_CONFIG_PATH,
    ) in ops

    assert run_mock.called


def test_install_raises_when_script_copy_fails(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dir_path, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports, "run", lambda *a, **k: Mock())

    def bad_copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(staticreports.shutil, "copy", bad_copy)
    sr = staticreports.StaticReports()

    with pytest.raises(OSError):
        sr.install()


def test_start_restarts_nginx_and_starts_all_report_services(monkeypatch):
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
        assert any(
            call
            for call in calls
            if call[0] == "start" and svc + ".service" in call and "--no-block" in call
        )


def test_start_raises_when_systemd_service_start_fails(monkeypatch):
    monkeypatch.setattr(staticreports.systemd, "service_restart", lambda *args: None)

    def bad_start(*args):
        raise CalledProcessError(1, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr.start()


def test_refresh_report_starts_all_static_report_services(monkeypatch):
    starts = []
    monkeypatch.setattr(staticreports.systemd, "service_start", lambda *args: starts.append(args))
    sr = staticreports.StaticReports()

    sr.refresh_report()

    for svc in staticreports.UBUNTU_STATIC_REPORT_SERVICES:
        assert any(call for call in starts if svc + ".service" in call)


def test_refresh_report_raises_when_service_start_fails(monkeypatch):
    def bad_start(*args):
        raise CalledProcessError(2, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr.refresh_report()


def test_setup_systemd_unit_writes_service_and_timer_with_proxy_environment(monkeypatch):
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "https://secure.example:8443")

    sr = staticreports.StaticReports()

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
    assert "Environment=HTTP_PROXY=http://proxy.example:8080" in written[svc_path]
    assert "Environment=HTTPS_PROXY=https://secure.example:8443" in written[svc_path]
    assert enabled and enabled[0][0] == "--now"


def test_setup_systemd_unit_writes_rsync_proxy_environment_variable(monkeypatch):
    """When HTTP proxy is configured, rsync proxy should also be injected into systemd units."""
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy.example:8080")

    sr = staticreports.StaticReports()

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
    monkeypatch.setattr(staticreports.systemd, "service_enable", lambda *args, **kwargs: None)

    sr.setup_systemd_unit("update-seeds")

    svc_path = "/etc/systemd/system/update-seeds.service"
    assert "Environment=RSYNC_PROXY=proxy.example:8080" in written[svc_path]


def test_setup_systemd_unit_without_proxy_environment_variables(monkeypatch):
    """When no proxy is configured, no proxy environment variables should be injected."""
    sr = staticreports.StaticReports()

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
    monkeypatch.setattr(staticreports.systemd, "service_enable", lambda *args, **kwargs: None)

    sr.setup_systemd_unit("update-seeds")

    svc_path = "/etc/systemd/system/update-seeds.service"
    assert "Environment=HTTP_PROXY" not in written[svc_path]
    assert "Environment=HTTPS_PROXY" not in written[svc_path]
    assert "Environment=RSYNC_PROXY" not in written[svc_path]


def test_setup_systemd_units_configures_all_report_services(monkeypatch):
    called = []
    monkeypatch.setattr(
        staticreports.StaticReports, "setup_systemd_unit", lambda self, s: called.append(s)
    )
    sr = staticreports.StaticReports()

    sr.setup_systemd_units()

    assert set(called) == set(staticreports.UBUNTU_STATIC_REPORT_SERVICES)


def test_install_packages_raises_when_package_not_found(monkeypatch):
    monkeypatch.setattr(staticreports.apt, "update", lambda: None)

    def bad_add(pkg):
        raise staticreports.PackageNotFoundError(pkg)

    monkeypatch.setattr(staticreports.apt, "add_package", bad_add)
    sr = staticreports.StaticReports()

    with pytest.raises(staticreports.PackageNotFoundError):
        sr._install_packages()


def test_install_packages_raises_when_package_installation_fails(monkeypatch):
    monkeypatch.setattr(staticreports.apt, "update", lambda: None)

    def bad_add(pkg):
        raise staticreports.PackageError("install failed")

    monkeypatch.setattr(staticreports.apt, "add_package", bad_add)
    sr = staticreports.StaticReports()

    with pytest.raises(staticreports.PackageError):
        sr._install_packages()


def test_install_raises_when_directory_creation_fails(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)

    def boom(dir_path, exist_ok=True):
        raise OSError("no space")

    monkeypatch.setattr(staticreports.os, "makedirs", boom)
    sr = staticreports.StaticReports()

    with pytest.raises(OSError):
        sr.install()


def test_configure_url_logs_configured_url(caplog):
    sr = staticreports.StaticReports()
    caplog.clear()

    with caplog.at_level("DEBUG"):
        sr.configure_url("http://example.local:80")

    assert "The url in use is http://example.local:80" in caplog.text


def test_setup_systemd_unit_raises_when_service_enable_fails(monkeypatch):
    monkeypatch.setattr(staticreports.Path, "read_text", lambda self: "[Service]")
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, t: None)
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )

    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://p:1")

    def bad_enable(*args, **kwargs):
        raise CalledProcessError(3, "systemctl")

    monkeypatch.setattr(staticreports.systemd, "service_enable", bad_enable)
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr.setup_systemd_unit("update-seeds")


def test_refresh_report_logs_stdout_when_service_start_fails(monkeypatch, caplog):
    """Test error stdout logging on service start failure.

    When systemd service_start fails, the error stdout should be logged for debugging.
    """

    def bad_start(name):
        e = CalledProcessError(5, "systemctl")
        e.stdout = b"failed output"
        raise e

    monkeypatch.setattr(staticreports.systemd, "service_start", bad_start)
    sr = staticreports.StaticReports()
    caplog.clear()

    with caplog.at_level("DEBUG"):
        with pytest.raises(CalledProcessError):
            sr.refresh_report()

    assert "failed output" in caplog.text


def test_install_clones_git_repositories_into_configured_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)

    ops = []
    monkeypatch.setattr(
        staticreports.shutil, "copy", lambda src, dst: ops.append(("copy", str(src), str(dst)))
    )

    repo_target = tmp_path / "ubuntu-archive-tools"
    monkeypatch.setattr(
        staticreports,
        "REPO_URLS",
        [("https://git.launchpad.net/ubuntu-archive-tools", "main", repo_target)],
    )

    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return Mock()

    monkeypatch.setattr(staticreports, "run", fake_run)
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    sr = staticreports.StaticReports()

    sr.install()

    assert any(
        isinstance(cmd, (list, tuple)) and "clone" in cmd and cmd[-1] == repo_target
        for cmd in run_calls
    ), f"expected clone to {repo_target}, got {run_calls}"


def test_install_raises_when_git_clone_fails(monkeypatch):
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)

    def bad_run(cmd, **kwargs):
        raise CalledProcessError(2, "git")

    monkeypatch.setattr(staticreports, "run", bad_run)
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr.install()


def test_staticreports_init_configures_proxy_environment_from_juju_vars(monkeypatch):
    monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "https://secure.example:3129")

    sr = StaticReports()

    assert "http" in sr.proxies
    assert "https" in sr.proxies
    assert sr.env.get("HTTP_PROXY") == "http://proxy.example:3128"
    assert sr.env.get("HTTPS_PROXY") == "https://secure.example:3129"


@patch("staticreports.pathops.LocalPath")
def test_configure_lpoauthkey_returns_false_on_write_errors(localpathmock):
    """Test configure_lpoauthkey handles write errors gracefully.

    configure_lpoauthkey should gracefully handle file write errors by returning False
    instead of raising exceptions, allowing the charm to detect and report the failure.
    """
    inst = localpathmock.return_value
    inst.parent = Path("/nonexistent/home/ubuntu")
    sr = StaticReports()
    import os as _os

    _os_makedirs = _os.makedirs

    # Test FileNotFoundError
    inst.write_text.side_effect = FileNotFoundError()
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs

    # Test LookupError
    inst.write_text.side_effect = LookupError()
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs

    # Test PermissionError
    inst.write_text.side_effect = PermissionError()
    try:
        _os.makedirs = lambda *a, **k: None
        assert sr.configure_lpoauthkey("data") is False
    finally:
        _os.makedirs = _os_makedirs
