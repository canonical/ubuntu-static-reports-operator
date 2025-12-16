# Copyright 2025 Canonical
# See LICENSE file for licensing details.

import logging

import jubilant
from requests import Session

from . import APPNAME, HAPROXY, SSC, DNSResolverHTTPSAdapter, retry, wait_oneshot_finished


def deploy_ha_wait_func(status):
    """Wait on juju status until deployed and started."""
    app_maintenance = status.apps[APPNAME].is_maintenance
    started = status.apps[APPNAME].app_status.message == "Starting Static Reports"
    haproxy_active = status.apps[HAPROXY].is_active
    ssc_active = status.apps[SSC].is_active
    logging.debug(f"app_maintenance: {app_maintenance}")
    logging.debug(f"started: {started}")
    logging.debug(f"haproxy_active: {haproxy_active}")
    logging.debug(f"ssc_active: {ssc_active}")
    return app_maintenance and started and haproxy_active and ssc_active


def test_service_state_after_ha_deploy(juju: jubilant.Juju, ubuntu_static_reports_charm):
    """Deploy the charm along haproxy and wait until it fully completed."""
    juju.deploy(ubuntu_static_reports_charm, app=APPNAME)
    juju.deploy(
        HAPROXY, channel="2.8/edge", config={"external-hostname": "ubuntu-static-reports.internal"}
    )
    juju.deploy(SSC, channel="1/edge")

    juju.integrate(APPNAME, HAPROXY)
    juju.integrate(f"{HAPROXY}:certificates", f"{SSC}:certificates")

    juju.wait(deploy_ha_wait_func, timeout=1800)
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-sync-blocklist.service"
    )
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-seeds.service")


# These have to follow test_service_state_after_deploy so content is ready
@retry(retry_num=24, retry_sleep_sec=5)
def check_content(juju: jubilant.Juju, path: str, startswith: str, contains: str):
    """Check if the response through haproxy matches the expected content."""
    model_name = juju.model
    assert model_name is not None

    haproxy_ip = juju.status().apps[HAPROXY].units[f"{HAPROXY}/0"].public_address
    external_hostname = "ubuntu-static-reports.internal"

    session = Session()
    session.mount("https://", DNSResolverHTTPSAdapter(external_hostname, haproxy_ip))
    response = session.get(
        f"https://{haproxy_ip}/{model_name}-{APPNAME}/{path}",
        headers={"Host": external_hostname},
        verify=False,
        timeout=30,
    )

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
    check_content(
        juju,
        path="TODO-web-path",
        startswith="TODO content at the beginning",
        contains="TODO-something-later",
    )


def test_content_permissions_report(juju: jubilant.Juju):
    """Check the response of permissions-report."""
    check_content(
        juju,
        path="TODO-web-path",
        startswith="TODO content at the beginning",
        contains="TODO-something-later",
    )


def test_content_packageset_report(juju: jubilant.Juju):
    """Check the response of packageset-report."""
    check_content(
        juju,
        path="TODO-web-path",
        startswith="TODO content at the beginning",
        contains="TODO-something-later",
    )
