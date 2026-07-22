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

requires_archive = pytest.mark.skipif(
    os.environ.get("MISMATCH_ARCHIVE_RSYNC_SOURCE") is None,
    reason="MISMATCH_ARCHIVE_RSYNC_SOURCE not set - archive-dependent mismatch test skipped",
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


def test_all_timers_are_active(juju: jubilant.Juju):
    """All report-service timers are installed and active after deploy."""
    timers = [
        "update-bugpatterns",
        "update-sync-blocklist",
        "update-seeds",
        "packageset-report",
        "package-subscribers",
        "permissions-report",
        "sru-report",
        "update-archive-mirror",
    ]
    for timer in timers:
        state = juju.ssh("ubuntu-static-reports/0", f"systemctl is-active {timer}.timer").strip()
        assert state == "active", f"{timer}.timer is not active"


def test_oncomplete_services_are_enabled(juju: jubilant.Juju):
    """Services triggered via OnSuccess= are installed and enabled after deploy."""
    services = ["update-germinate", "update-mismatches", "update-nbs"]
    for service in services:
        state = juju.ssh(
            "ubuntu-static-reports/0", f"systemctl is-enabled {service}.service"
        ).strip()
        assert state == "enabled", f"{service}.service is not enabled"


def test_mismatches_path_is_served(juju: jubilant.Juju):
    """The (initially empty) mismatches report directory is served by nginx."""
    response = requests.get(f"http://{address(juju)}:80/mismatches/", timeout=30)
    assert response.status_code == 200


@requires_secret
@requires_archive
def test_content_mismatches(juju: jubilant.Juju):
    """Generate and serve the mismatch reports against a reachable archive source.

    Skipped unless MISMATCH_ARCHIVE_RSYNC_SOURCE points at an rsync source the
    test runner can reach (the production default is the public archive
    mirror, which may not be reachable from all CI runners). update-archive-mirror
    builds the archive-index snapshot, update-germinate germinates it and
    publishes the combined snapshot, and the mismatch reports then consume its
    `current` pointer.
    """
    juju.config(
        APPNAME,
        {"rsync_archive_source": os.environ["MISMATCH_ARCHIVE_RSYNC_SOURCE"]},
    )
    juju.wait(jubilant.all_active, timeout=300)

    # Seed the germinate inputs, then build the archive-index snapshot.
    juju.ssh(
        "ubuntu-static-reports/0",
        "sudo systemctl start --no-block update-seeds.service",
    )
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-seeds.service")

    juju.ssh(
        "ubuntu-static-reports/0",
        "sudo systemctl start --no-block update-archive-mirror.service",
    )
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-archive-mirror.service"
    )

    # update-archive-mirror triggers update-germinate via OnSuccess=; wait for it
    # to publish the combined archive+germinate snapshot the mismatch reports read.
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-germinate.service")

    # update-germinate triggers update-mismatches via OnSuccess= in turn.
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-mismatches.service"
    )

    # /germinate/ is a symlink the charm maintains to update-germinate's `current`
    # snapshot, so it only resolves once a snapshot has actually been published.
    response = requests.get(f"http://{address(juju)}:80/germinate/", timeout=30)
    assert response.status_code == 200

    check_content(
        juju,
        path="mismatches/architecture-mismatches.html",
        startswith="<!DOCTYPE html",
        contains="Architecture mismatches",
    )
