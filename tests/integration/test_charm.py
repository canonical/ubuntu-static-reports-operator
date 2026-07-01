# Copyright 2025 Canonical
# See LICENSE file for licensing details.

import logging
import os
import time

import jubilant
import pytest
import requests

from . import APPNAME, address, retry, wait_oneshot_finished

requires_secret = pytest.mark.skipif(
    os.environ.get("LPUSER_OAUTH_FILE") is None,
    reason="LPUSER_OAUTH_FILE not set - secret-dependent tests skipped",
)


def deploy_wait_func(status):
    """Wait on juju status until deployed and started."""
    all_maint = jubilant.all_maintenance(status)
    started = status.apps[APPNAME].app_status.message == "Starting Static Reports"
    ready = all_maint and started
    logging.debug(f"all_maint: {all_maint}")
    logging.debug(f"started: {started}")
    return ready


def test_service_state_after_deploy(
    juju: jubilant.Juju, ubuntu_static_reports_charm, lpuser_secret
):
    """Deploy the charm via jubilant and wait until it fully completed."""
    juju.deploy(ubuntu_static_reports_charm, app=APPNAME)

    if lpuser_secret:
        juju.config(APPNAME, {"lpuser_secret_id": lpuser_secret})

    juju.wait(deploy_wait_func, timeout=1200)
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-sync-blocklist.service"
    )
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-seeds.service")


# These have to follow test_service_state_after_deploy so content is ready
@retry(retry_num=20, retry_sleep_sec=6)
def check_content(juju: jubilant.Juju, path, startswith, contains):
    """Check if the response matches the expected content."""
    response = requests.get(f"http://{address(juju)}:80/{path}", timeout=30)
    assert response.status_code == 200
    assert response.text.startswith(startswith)
    assert contains in response.text


def test_content_sync_blocklist(juju: jubilant.Juju):
    """Check the response of sync blocklist."""
    check_content(
        juju,
        path="sync-blocklist.txt",
        startswith="# THIS IS A GIT MAINTAINED FILE",
        contains="linux",
    )


def test_content_update_seeds(juju: jubilant.Juju):
    """Check the response of update seeds."""
    check_content(
        juju,
        path="seeds/ubuntu.resolute/server",
        startswith="Task-Section: server",
        contains="ubuntu-server",
    )


def test_content_package_subscribers(juju: jubilant.Juju):
    """Check the response of package-subscribers."""
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="package-subscribers.service"
    )
    check_content(
        juju,
        path="package-team-mapping.json",
        startswith="{",
        contains="ubuntu-server",
    )


def test_sru_report_logs_progress_in_debug_mode(juju: jubilant.Juju):
    """Confirm sru-report starts working without waiting for the full run.

    A full sru-report run takes hours, so instead of waiting for completion we
    enable DEBUG via a systemd drop-in, start the service non-blocking, and
    confirm from the journal that it began querying Launchpad. We also check the
    empty .new report file is freshly created by the run (and absent before it).
    """
    unit = "ubuntu-static-reports/0"
    override_dir = "/etc/systemd/system/sru-report.service.d"
    report_new = "/srv/staticreports/www/pending-sru/pending-sru.html.new"

    def file_test(expr):
        return juju.ssh(unit, f"[ {expr} ] && echo yes || echo no").strip() == "yes"

    try:
        juju.ssh(
            unit,
            f"sudo systemctl stop sru-report.service; "
            f"sudo rm -f {report_new}; "
            f"sudo install -d {override_dir} && "
            f"printf '[Service]\\nEnvironment=DEBUG=1\\n' | "
            f"sudo tee {override_dir}/debug.conf >/dev/null && "
            "sudo systemctl daemon-reload",
        )

        assert not file_test(f"-e {report_new}")

        juju.ssh(unit, "sudo systemctl start --no-block sru-report.service")
        time.sleep(15)

        journal = juju.ssh(unit, "sudo journalctl -u sru-report.service --no-pager")
        assert "Initializing LP Credentials" in journal

        assert file_test(f"-e {report_new}")
        assert not file_test(f"-s {report_new}")
    finally:
        juju.ssh(
            unit,
            "sudo systemctl stop sru-report.service; "
            f"sudo rm -f {override_dir}/debug.conf {report_new}; "
            "sudo systemctl daemon-reload",
        )


@requires_secret
def test_content_permissions_report(juju: jubilant.Juju):
    """Check the response of permissions-report."""
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="permissions-report.service"
    )
    check_content(
        juju,
        path="output/permissions-report.html",
        startswith="<!DOCTYPE html>",
        contains="Permission Report",
    )


@requires_secret
def test_content_packageset_report(juju: jubilant.Juju):
    """Check the response of packageset-report."""
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="packageset-report.service"
    )
    check_content(
        juju,
        path="output/packageset-report.html",
        startswith="<!DOCTYPE html>",
        contains="Packageset Report",
    )


def test_archive_mirror_sync_populates_local_mirror(juju: jubilant.Juju):
    """Check that the archive mirror sync populates /var/cache/mirror for the devel release."""
    unit = "ubuntu-static-reports/0"
    wait_oneshot_finished(juju, unit=unit, service="update-archive-mirror.service")

    devel = juju.ssh(unit, "distro-info --devel").strip()
    assert devel, "distro-info --devel returned nothing on the unit"

    dist_dir = f"/var/cache/mirror/ubuntu/dists/{devel}"

    # The release tree exists and carries its top-level Release index.
    juju.ssh(unit, f"test -f {dist_dir}/Release")

    # Check Sources and Packages info downloaded
    juju.ssh(
        unit,
        f"ls {dist_dir}/*/source/Sources.gz >/dev/null 2>&1 || "
        f"{{ echo 'no Sources.gz under {dist_dir}'; ls -R {dist_dir}; exit 1; }}",
    )
    juju.ssh(
        unit,
        f"ls {dist_dir}/*/binary-*/Packages.gz >/dev/null 2>&1 || "
        f"{{ echo 'no Packages.gz under {dist_dir}'; ls -R {dist_dir}; exit 1; }}",
    )
