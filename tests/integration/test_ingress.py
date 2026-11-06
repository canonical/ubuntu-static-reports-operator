# Copyright 2025 Canonical
# See LICENSE file for licensing details.


import jubilant
from requests import Session

from . import (
    APPNAME,
    HAPROXY,
    SSC,
    DNSResolverHTTPSAdapter,
    address,
    configure_lp_secret,
    requires_secret,
    retry,
    wait_oneshot_finished,
)


def test_service_state_after_ha_deploy(
    juju: jubilant.Juju, ubuntu_static_reports_charm, lpuser_secret
):
    """Deploy the charm along haproxy and wait until it fully completed."""
    juju.deploy(
        ubuntu_static_reports_charm,
        app=APPNAME,
        # Keep the hourly archive-mirror sync - and the ~13G germinate
        # snapshot churn its OnSuccess= triggers - out of the test VM.
        config={"rsync_archive_source": "rsync://127.0.0.1/integration-tests"},
    )
    juju.deploy(
        HAPROXY, channel="2.8/edge", config={"external-hostname": "ubuntu-static-reports.internal"}
    )
    juju.deploy(SSC, channel="1/edge")

    juju.integrate(APPNAME, HAPROXY)
    juju.integrate(f"{HAPROXY}:certificates", f"{SSC}:certificates")

    configure_lp_secret(juju, lpuser_secret)

    juju.wait(jubilant.all_active, timeout=3600)
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="update-sync-blocklist.service"
    )
    wait_oneshot_finished(juju, unit="ubuntu-static-reports/0", service="update-seeds.service")


# These have to follow test_service_state_after_ha_deploy so content is ready
@retry(retry_num=48, retry_sleep_sec=10)
def check_content(juju: jubilant.Juju, path: str, startswith: str, contains: str):
    """Check if the response through haproxy matches the expected content."""
    model_name = juju.model
    assert model_name is not None

    status = juju.status()
    if HAPROXY not in status.apps:
        raise AssertionError(f"{HAPROXY} not found in deployed apps - HA test requires haproxy")

    haproxy_ip = address(juju, app=HAPROXY)
    external_hostname = "ubuntu-static-reports.internal"

    session = Session()
    session.mount("https://", DNSResolverHTTPSAdapter(external_hostname, haproxy_ip))
    response = session.get(
        f"https://{haproxy_ip}/{model_name}-{APPNAME}/{path}",
        headers={"Host": external_hostname},
        verify=False,
        timeout=60,
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
        path="package-team-mapping.json",
        startswith="{",
        contains="ubuntu-server",
    )


@requires_secret
def test_content_permissions_report(juju: jubilant.Juju):
    """Check the response of permissions-report through haproxy.

    The tool writes plain permission listings (=== user === sections
    with " - package" entries), one file per permission source.
    """
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="permissions-report.service"
    )
    check_content(
        juju,
        path="archive-permissions/teams",
        startswith="=== ",
        contains=" - ",
    )


@requires_secret
def test_content_packageset_report(juju: jubilant.Juju):
    """Check the response of packageset-report through haproxy.

    The tool writes one file per series and package set, ending with a
    'Generated at:' stamp; the file name is discovered from the unit.
    """
    wait_oneshot_finished(
        juju, unit="ubuntu-static-reports/0", service="packageset-report.service"
    )
    pkgset = juju.ssh(
        "ubuntu-static-reports/0",
        "ls /srv/staticreports/www/packagesets/noble/ | head -1",
    ).strip()
    assert pkgset, "no packageset files produced"

    check_content(
        juju,
        path=f"packagesets/noble/{pkgset}",
        startswith="Name: ",
        contains="Generated at:",
    )
