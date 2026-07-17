# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for `src/staticreports.py`.

These tests mock external side-effects (apt, systemd, os, shutil, and
pathlib writes) so they can run as unit tests without touching the host.
"""

from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
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


UNATTENDED_UPGRADES_SAMPLE_CONFIG = """Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    // only security is enabled by default
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
//  "${distro_id}:${distro_codename}-updates";
//  "${distro_id}:${distro_codename}-proposed";
//  "${distro_id}:${distro_codename}-backports";
};
"""


def test_configure_unattended_upgrades_enables_auto_updates_via_dpkg_reconfigure(monkeypatch):
    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return Mock()

    monkeypatch.setattr(staticreports, "run", fake_run)
    monkeypatch.setattr(
        staticreports.Path, "read_text", lambda self: UNATTENDED_UPGRADES_SAMPLE_CONFIG
    )
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, text: None)
    sr = staticreports.StaticReports()

    sr._configure_unattended_upgrades()

    assert ["debconf-set-selections"] in run_calls
    assert ["dpkg-reconfigure", "-f", "noninteractive", "unattended-upgrades"] in run_calls


def test_configure_unattended_upgrades_enables_updates_pocket_in_allowed_origins(monkeypatch):
    monkeypatch.setattr(staticreports, "run", lambda *a, **k: Mock())
    monkeypatch.setattr(
        staticreports.Path, "read_text", lambda self: UNATTENDED_UPGRADES_SAMPLE_CONFIG
    )
    written = {}
    monkeypatch.setattr(
        staticreports.Path, "write_text", lambda self, text: written.__setitem__(str(self), text)
    )
    sr = staticreports.StaticReports()

    sr._configure_unattended_upgrades()

    content = written[str(staticreports.UNATTENDED_UPGRADES_CONFIG_PATH)]
    assert '    "${distro_id}:${distro_codename}-updates";' in content
    assert '//  "${distro_id}:${distro_codename}-proposed";' in content
    assert '//  "${distro_id}:${distro_codename}-backports";' in content


def test_configure_unattended_upgrades_raises_when_dpkg_reconfigure_fails(monkeypatch):
    def bad_run(cmd, **kwargs):
        if cmd[0] == "dpkg-reconfigure":
            raise CalledProcessError(1, cmd)
        return Mock()

    monkeypatch.setattr(staticreports, "run", bad_run)
    sr = staticreports.StaticReports()

    with pytest.raises(CalledProcessError):
        sr._configure_unattended_upgrades()


def test_install_creates_srv_directories_and_copies_scripts(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )

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

    assert ("copy", "src/script/update-bugpatterns", "/usr/bin") in ops
    assert ("copy", "src/script/update-sync-blocklist", "/usr/bin") in ops
    assert ("copy", "src/script/update-seeds", "/usr/bin") in ops
    assert ("copy", "src/script/update-archive-mirror", "/usr/bin") in ops
    assert ("copy", "src/script/germinate-ubuntu", "/usr/bin") in ops
    assert ("copy", "src/script/update-germinate", "/usr/bin") in ops
    assert ("copy", "src/script/update-mismatches", "/usr/bin") in ops
    assert (
        "copy",
        "src/nginx/staticreports.conf",
        staticreports.NGINX_SITE_CONFIG_PATH,
    ) in ops

    assert run_mock.called


def test_install_raises_when_script_copy_fails(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dir_path, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports, "run", lambda *a, **k: Mock())
    monkeypatch.setattr(
        staticreports.Path,
        "unlink",
        lambda self, missing_ok=True: None,
    )

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

    def fake_read_text(self, encoding=None):
        return "[Service]\nExecStart=/bin/true" if self.suffix == ".service" else "[Timer]"

    written = {}

    def fake_write_text(self, text, encoding=None):
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

    def fake_read_text(self, encoding=None):
        return "[Service]\nExecStart=/bin/true" if self.suffix == ".service" else "[Timer]"

    written = {}

    def fake_write_text(self, text, encoding=None):
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

    def fake_read_text(self, encoding=None):
        return "[Service]\nExecStart=/bin/true" if self.suffix == ".service" else "[Timer]"

    written = {}

    def fake_write_text(self, text, encoding=None):
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
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports.shutil, "copy", lambda src, dst: None)
    monkeypatch.setattr(
        staticreports.Path,
        "unlink",
        lambda self, missing_ok=True: None,
    )

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


def test_configure_mismatches_writes_archive_root_from_mirror_dir(monkeypatch):
    written = {}
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )
    monkeypatch.setattr(
        staticreports.Path,
        "write_text",
        lambda self, text, encoding=None: written.__setitem__(str(self), text),
    )
    sr = staticreports.StaticReports()

    sr.configure_mismatches("/srv/mirror")

    content = written[staticreports.MISMATCHES_ENV_PATH]
    assert content == "ARCHIVE_ROOT=/srv/mirror/germinate/current\n"


def test_configure_archive_mirror_writes_overrides(monkeypatch):
    written = {}
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )
    monkeypatch.setattr(
        staticreports.Path,
        "write_text",
        lambda self, text, encoding=None: written.__setitem__(str(self), text),
    )
    relinked = {}
    monkeypatch.setattr(
        staticreports, "_relink", lambda link, target: relinked.__setitem__(str(link), target)
    )
    monkeypatch.setattr(staticreports.os, "makedirs", lambda path, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    sr = staticreports.StaticReports()

    sr.configure_archive_mirror("rsync://host/dists/", "/var/cache/mirror")

    content = written[staticreports.ARCHIVE_MIRROR_ENV_PATH]
    assert "RSYNC_ARCHIVE_SOURCE=rsync://host/dists/" in content
    assert "MIRROR_DIR=/var/cache/mirror" in content
    assert relinked[str(staticreports.GERMINATE_WEB_PATH)] == "/var/cache/mirror/germinate/current"


def test_configure_archive_mirror_relinks_germinate_to_default_when_mirror_dir_empty(monkeypatch):
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, text, encoding=None: None)
    relinked = {}
    monkeypatch.setattr(
        staticreports, "_relink", lambda link, target: relinked.__setitem__(str(link), target)
    )
    monkeypatch.setattr(staticreports.os, "makedirs", lambda path, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    sr = staticreports.StaticReports()

    sr.configure_archive_mirror("", "")

    assert (
        relinked[str(staticreports.GERMINATE_WEB_PATH)]
        == f"{staticreports.DEFAULT_MIRROR_DIR}/germinate/current"
    )


def test_update_germinate_is_a_registered_report_service():
    assert "update-germinate" in staticreports.UBUNTU_STATIC_REPORT_SERVICES


def test_update_mismatches_is_a_registered_report_service():
    assert "update-mismatches" in staticreports.UBUNTU_STATIC_REPORT_SERVICES


def test_setup_systemd_unit_raises_when_service_enable_fails(monkeypatch):
    monkeypatch.setattr(staticreports.Path, "read_text", lambda self, encoding=None: "[Service]")
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, t, encoding=None: None)
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


def test_setup_systemd_unit_skips_enabling_when_existing_unit_manually_disabled(monkeypatch):
    """A unit already present on disk and disabled via systemctl should stay disabled."""
    monkeypatch.setattr(
        staticreports.Path, "read_text", lambda self, encoding=None: "[Service]\n[Timer]"
    )
    written = {}
    monkeypatch.setattr(
        staticreports.Path,
        "write_text",
        lambda self, text, encoding=None: written.__setitem__(str(self), text),
    )
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )

    def fake_exists(self):
        # Both the shipped timer definition and the already-installed unit exist.
        return (
            self.parent.name == "systemd"
            or str(self) == "/etc/systemd/system/update-seeds.service"
        )

    monkeypatch.setattr(staticreports.Path, "exists", fake_exists)

    enabled = []
    monkeypatch.setattr(
        staticreports.systemd, "service_enable", lambda *args, **kwargs: enabled.append(args)
    )

    fake_result = SimpleNamespace(stdout="disabled\n")
    monkeypatch.setattr(staticreports, "run", lambda *args, **kwargs: fake_result)

    sr = staticreports.StaticReports()
    sr.setup_systemd_unit("update-seeds")

    assert enabled == []
    assert "/etc/systemd/system/update-seeds.service" in written


def test_setup_systemd_unit_enables_existing_unit_when_still_enabled(monkeypatch):
    """A previously-installed unit that is still enabled continues to be (re-)enabled."""
    monkeypatch.setattr(
        staticreports.Path, "read_text", lambda self, encoding=None: "[Service]\n[Timer]"
    )
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, text, encoding=None: None)
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )

    def fake_exists(self):
        return (
            self.parent.name == "systemd"
            or str(self) == "/etc/systemd/system/update-seeds.service"
        )

    monkeypatch.setattr(staticreports.Path, "exists", fake_exists)

    enabled = []
    monkeypatch.setattr(
        staticreports.systemd, "service_enable", lambda *args, **kwargs: enabled.append(args)
    )

    fake_result = SimpleNamespace(stdout="enabled\n")
    monkeypatch.setattr(staticreports, "run", lambda *args, **kwargs: fake_result)

    sr = staticreports.StaticReports()
    sr.setup_systemd_unit("update-seeds")

    assert enabled and enabled[0][0] == "--now"


def test_setup_systemd_unit_enables_new_unit_without_checking_enabled_state(monkeypatch):
    """A unit not yet installed (new service) is enabled without querying systemctl."""
    monkeypatch.setattr(
        staticreports.Path, "read_text", lambda self, encoding=None: "[Service]\n[Timer]"
    )
    monkeypatch.setattr(staticreports.Path, "write_text", lambda self, text, encoding=None: None)
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=True, exist_ok=True: None
    )
    monkeypatch.setattr(staticreports.Path, "exists", lambda self: self.parent.name == "systemd")

    enabled = []
    monkeypatch.setattr(
        staticreports.systemd, "service_enable", lambda *args, **kwargs: enabled.append(args)
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("systemctl is-enabled should not be queried for a new unit")

    monkeypatch.setattr(staticreports, "run", fail_run)

    sr = staticreports.StaticReports()
    sr.setup_systemd_unit("update-seeds")

    assert enabled and enabled[0][0] == "--now"


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
    monkeypatch.setattr(
        staticreports.Path,
        "unlink",
        lambda self, missing_ok=True: ops.append(("unlink", str(self))),
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
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
    sr = staticreports.StaticReports()

    sr.install()

    assert any(
        isinstance(cmd, (list, tuple)) and "clone" in cmd and cmd[-1] == repo_target
        for cmd in run_calls
    ), f"expected clone to {repo_target}, got {run_calls}"


def test_install_raises_when_git_clone_fails(monkeypatch):
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dname, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports.shutil, "copy", lambda src, dst: None)
    monkeypatch.setattr(
        staticreports.Path,
        "unlink",
        lambda self, missing_ok=True: None,
    )

    def bad_run(cmd, **kwargs):
        raise CalledProcessError(2, "git")

    monkeypatch.setattr(staticreports, "run", bad_run)
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
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
def test_configure_lpoauthkey_returns_false_on_write_errors(localpathmock, monkeypatch):
    """Test configure_lpoauthkey handles write errors gracefully.

    configure_lpoauthkey should gracefully handle file write errors by returning False
    instead of raising exceptions, allowing the charm to detect and report the failure.
    """
    inst = localpathmock.return_value
    inst.parent = Path("/nonexistent/home/ubuntu")
    sr = StaticReports()
    monkeypatch.setattr(staticreports.os, "makedirs", lambda *a, **k: None)

    for error in (FileNotFoundError(), LookupError(), PermissionError()):
        inst.write_text.side_effect = error
        assert sr.configure_lpoauthkey("data") is False


def test_sru_report_is_a_managed_static_report_service():
    assert "sru-report" in staticreports.UBUNTU_STATIC_REPORT_SERVICES


def test_install_copies_sru_report_script_to_usr_bin(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
    monkeypatch.setattr(staticreports, "run", Mock())
    monkeypatch.setattr(staticreports.os, "makedirs", lambda dir_path, exist_ok=True: None)
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports.Path, "unlink", lambda self, missing_ok=True: None)

    copies = []
    monkeypatch.setattr(staticreports.shutil, "copy", lambda src, dst: copies.append((src, dst)))
    sr = staticreports.StaticReports()

    sr.install()

    assert ("src/script/sru-report", "/usr/bin") in copies


def test_install_creates_pending_sru_output_directory(monkeypatch):
    monkeypatch.setattr(staticreports.StaticReports, "_install_packages", lambda self: None)
    monkeypatch.setattr(
        staticreports.StaticReports, "_configure_unattended_upgrades", lambda self: None
    )
    monkeypatch.setattr(staticreports, "run", Mock())
    monkeypatch.setattr(staticreports.shutil, "chown", lambda path, u, g: None)
    monkeypatch.setattr(staticreports.shutil, "copy", lambda src, dst: None)
    monkeypatch.setattr(staticreports.Path, "unlink", lambda self, missing_ok=True: None)

    created = []
    monkeypatch.setattr(
        staticreports.os, "makedirs", lambda dir_path, exist_ok=True: created.append(dir_path)
    )
    sr = staticreports.StaticReports()

    sr.install()

    assert Path("/srv/staticreports/www/pending-sru") in created


def test_setup_systemd_unit_for_sru_report_uses_no_launchpad_credentials(monkeypatch):
    """The pending-SRU report logs into Launchpad anonymously.

    Its unit must therefore not reference the shared Launchpad OAuth
    credentials used by the other reports.
    """
    written = {}
    monkeypatch.setattr(
        staticreports.Path,
        "write_text",
        lambda self, text, encoding=None: written.__setitem__(str(self), text),
    )
    monkeypatch.setattr(
        staticreports.Path, "mkdir", lambda self, parents=False, exist_ok=False: None
    )
    monkeypatch.setattr(staticreports.systemd, "service_enable", lambda *args, **kwargs: None)
    sr = staticreports.StaticReports()

    sr.setup_systemd_unit("sru-report")

    svc = written["/etc/systemd/system/sru-report.service"]
    assert "OUTPUTDIR=/srv/staticreports/www/pending-sru" in svc
    assert "/usr/bin/sru-report" in svc
    assert "LP_CREDENTIALS_FILE" not in svc
    assert "lp-ubuntu-archive-unprivileged-bot.oauth" not in svc
