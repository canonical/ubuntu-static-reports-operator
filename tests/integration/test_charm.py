# Copyright 2025 Canonical
# See LICENSE file for licensing details.

import logging
import os

import jubilant
import pytest
import requests

from . import APPNAME, retry, wait_oneshot_finished

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


def address(juju: jubilant.Juju):
    """Report the IP address of the application."""
    return juju.status().apps[APPNAME].units[f"{APPNAME}/0"].public_address


def test_service_state_after_deploy(juju: jubilant.Juju, ubuntu_static_reports_charm):
    """Deploy the charm via jubilant and wait until it fully completed."""
    juju.deploy(ubuntu_static_reports_charm, app=APPNAME)
    juju.wait(deploy_wait_func, timeout=600)
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-sync-blocklist.service"
    )
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-seeds.service")


# These have to follow test_service_state_after_deploy so content is ready
@retry(retry_num=10, retry_sleep_sec=3)
def check_content(juju: jubilant.Juju, path, startswith, contains):
    """Check if the response matches the expected content."""
    response = requests.get(f"http://{address(juju)}:80/{path}", timeout=15)
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
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="package-subscribers.service")
    check_content(
        juju,
        path="package-team-mapping.json",
        startswith="{",
        contains="\"packages\"",
    )


@requires_secret
def test_content_permissions_report(juju: jubilant.Juju):
    """Check the response of permissions-report."""
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="permissions-report.service")
    check_content(
        juju,
        path="output/permissions-report.html",
        startswith="<!DOCTYPE html>",
        contains="Permission Report",
    )


@requires_secret
def test_content_packageset_report(juju: jubilant.Juju):
    """Check the response of packageset-report."""
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="packageset-report.service")
    check_content(
        juju,
        path="output/packageset-report.html",
        startswith="<!DOCTYPE html>",
        contains="Packageset Report",
    )
