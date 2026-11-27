# Copyright 2025 Canonical
# See LICENSE file for licensing details.

import json
import os

import jubilant
import pytest
import requests

from . import (
    APPNAME,
    address,
    configure_lp_secret,
    oneshot_invocation_id,
    requires_archive,
    requires_secret,
    retry,
    wait_oneshot_finished,
)


def test_service_state_after_deploy(
    juju: jubilant.Juju, ubuntu_static_reports_charm, lpuser_secret
):
    """Deploy the charm via jubilant and wait until it fully completed."""
    juju.deploy(
        ubuntu_static_reports_charm,
        app=APPNAME,
        # Keep the hourly archive-mirror sync - and the ~13G germinate
        # snapshot churn its OnSuccess= triggers - out of the test VM.
        # The mismatch test overrides this with a real source when
        # MISMATCH_ARCHIVE_RSYNC_SOURCE is set.
        config={"rsync_archive_source": "rsync://127.0.0.1/integration-tests"},
    )
    configure_lp_secret(juju, lpuser_secret)

    juju.wait(jubilant.all_active, timeout=1200)
    wait_oneshot_finished(
        juju,
        unit="ubuntu-static-reports/0",
        service="update-sync-blocklist.service",
        timeout_sec=600,
    )
    # update-seeds performs a Launchpad-bound cold clone on its first run and
    # publishes individual seed files incrementally. The content tests below
    # wait for the specific seed content they need instead of requiring the
    # entire background clone to finish.


# These have to follow test_service_state_after_deploy so content is ready
@retry(retry_num=150, retry_sleep_sec=6)
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
    """Check the response of package-subscribers.

    A timer may start another long invocation after the first successful run.
    The published JSON is the integration contract, so waiting for every
    overlapping invocation would turn a known Launchpad hang into a two-hour
    test delay.
    """
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
            "sudo systemctl stop sru-report.timer sru-report.service; "
            f"sudo rm -f {report_new}; "
            f"sudo install -d {override_dir} && "
            f"printf '[Service]\\nEnvironment=DEBUG=1\\n' | "
            f"sudo tee {override_dir}/debug.conf >/dev/null && "
            "sudo systemctl daemon-reload",
        )

        assert not file_test(f"-e {report_new}")

        juju.ssh(unit, "sudo systemctl start --no-block sru-report.service")

        @retry(retry_num=6, retry_sleep_sec=10)
        def progress_started():
            journal = juju.ssh(unit, "sudo journalctl -u sru-report.service --no-pager")
            assert "Initializing LP Credentials" in journal, (
                f"sru-report did not start querying Launchpad yet, journal:\n{journal}"
            )
            assert file_test(f"-e {report_new}")
            assert not file_test(f"-s {report_new}")

        progress_started()
    finally:
        juju.ssh(
            unit,
            "sudo systemctl stop sru-report.service; "
            f"sudo rm -f {override_dir}/debug.conf {report_new}; "
            "sudo systemctl daemon-reload; "
            "sudo systemctl start sru-report.timer",
        )


@requires_secret
def test_content_permissions_report(juju: jubilant.Juju):
    """Check the response of permissions-report.

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
    """Check the response of packageset-report.

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


def test_onsuccess_services_are_enabled(juju: jubilant.Juju):
    """Services triggered via OnSuccess= are installed but stay static (not enabled)."""
    services = ["update-germinate", "update-mismatches", "update-nbs", "seeded-in-ubuntu-indexer"]
    for service in services:
        state = juju.ssh(
            "ubuntu-static-reports/0", f"systemctl is-enabled {service}.service"
        ).strip()
        assert state == "static", f"{service}.service is not static"


def test_static_reports_diagnose_matches_cli_and_action(juju: jubilant.Juju):
    """The installed CLI and Juju action expose the same health document.

    The integration model intentionally points the archive source at an
    unreachable local address, so an overall critical result is expected. The
    CLI's non-zero diagnostic exit code must not be mistaken for an SSH
    command failure.
    """
    unit = "ubuntu-static-reports/0"
    cli_document = json.loads(juju.ssh(unit, "static-reports-diagnose --format json || true"))
    task = juju.run(unit, "static-reports-diagnose", wait=60)
    action_document = json.loads(task.results["report"])

    assert cli_document["schema_version"] == action_document["schema_version"] == 1
    assert cli_document["status"] == action_document["status"] == "critical"
    assert len(cli_document["reports"]) == len(action_document["reports"]) == 12
    assert {report["service"] for report in cli_document["reports"]} == {
        report["service"] for report in action_document["reports"]
    }
    assert cli_document["serving"] is not None
    assert action_document["serving"] is not None

    selected = juju.run(
        unit,
        "static-reports-diagnose",
        {"service": "update-sync-blocklist"},
        wait=60,
    )
    selected_document = json.loads(selected.results["report"])
    assert len(selected_document["reports"]) == 1
    assert selected_document["reports"][0]["service"] == "update-sync-blocklist"
    assert selected_document["serving"] is None


def test_published_health_snapshots_are_served(juju: jubilant.Juju):
    """The periodic hook publishes stable JSON and text snapshot views."""
    unit = "ubuntu-static-reports/0"
    base_url = f"http://{address(juju)}:80/health"

    @retry(retry_num=60, retry_sleep_sec=6)
    def fetch(path):
        response = requests.get(f"{base_url}/{path}", timeout=30)
        assert response.status_code == 200, (
            f"{path} is not served yet (HTTP {response.status_code})"
        )
        return response

    latest_response = fetch("health.json")
    text_response = fetch("health.txt")
    snapshots_response = fetch("snapshots/")

    document = json.loads(latest_response.content)
    assert document["schema_version"] == 1
    assert document["status"] in {"ok", "warning", "critical", "unknown", "waiting"}
    assert len(document["reports"]) == 12
    assert document["serving"] is not None
    assert text_response.text.startswith("Overall: ")
    assert "snapshot." in snapshots_response.text

    history_response = requests.get(f"{base_url}/history.jsonl", timeout=30)
    assert history_response.status_code == 404

    snapshot_path = juju.ssh(
        unit,
        "find /srv/staticreports/www/health/snapshots "
        "-mindepth 1 -maxdepth 1 -type d -name 'snapshot.*' | head -1",
    ).strip()
    assert snapshot_path
    snapshot_name = os.path.basename(snapshot_path)
    snapshot_response = fetch(f"snapshots/{snapshot_name}/health.json")
    snapshot_document = json.loads(snapshot_response.content)
    assert snapshot_document["schema_version"] == 1
    assert snapshot_document["reports"]


def test_run_report_runs_only_selected_service(juju: jubilant.Juju):
    """A targeted recovery completes and returns the selected report's health."""
    task = juju.run(
        "ubuntu-static-reports/0",
        "run-report",
        {"service": "update-sync-blocklist"},
        wait=600,
    )

    assert task.results["service"] == "update-sync-blocklist"
    assert task.results["status"] == "ok"
    assert task.results["outcome"] == "published"
    document = json.loads(task.results["report"])
    assert len(document["reports"]) == 1
    assert document["reports"][0]["service"] == "update-sync-blocklist"


def test_swapfile_is_active_and_persistent(juju: jubilant.Juju):
    """The charm-ensured swap file is active and referenced in /etc/fstab.

    Skipped in containers: swapon is impossible there and the charm
    deliberately continues without the swap safety net.
    """
    unit = "ubuntu-static-reports/0"
    virt = juju.ssh(unit, "systemd-detect-virt --container || true").strip()
    if virt != "none":
        pytest.skip(f"unit runs in a container ({virt}) - swap is not possible")

    swaps = juju.ssh(unit, "cat /proc/swaps")
    assert "/swapfile.swp" in swaps, f"/swapfile.swp not active in /proc/swaps: {swaps}"

    fstab = juju.ssh(unit, "cat /etc/fstab")
    assert "/swapfile.swp" in fstab, f"/swapfile.swp not present in /etc/fstab: {fstab}"


def test_mismatches_path_is_served(juju: jubilant.Juju):
    """The (initially empty) mismatches report directory is served by nginx."""
    response = requests.get(f"http://{address(juju)}:80/mismatches/", timeout=30)
    assert response.status_code == 200


def test_nbs_path_is_served(juju: jubilant.Juju):
    """The (initially empty) NBS report directory is served by nginx."""
    response = requests.get(f"http://{address(juju)}:80/nbs/", timeout=30)
    assert response.status_code == 200


def test_seeded_in_ubuntu_path_is_served(juju: jubilant.Juju):
    """The (initially empty) seeded-in-ubuntu directory is served by nginx."""
    response = requests.get(f"http://{address(juju)}:80/seeded-in-ubuntu/", timeout=30)
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
    unit = "ubuntu-static-reports/0"
    baseline = oneshot_invocation_id(juju, unit, "update-seeds.service")
    juju.ssh(unit, "sudo systemctl restart --no-block update-seeds.service")
    wait_oneshot_finished(
        juju,
        unit=unit,
        service="update-seeds.service",
        timeout_sec=7200,
        baseline_invocation_id=baseline,
    )

    archive_baseline = oneshot_invocation_id(juju, unit, "update-archive-mirror.service")
    germinate_baseline = oneshot_invocation_id(juju, unit, "update-germinate.service")
    mismatches_baseline = oneshot_invocation_id(juju, unit, "update-mismatches.service")
    indexer_baseline = oneshot_invocation_id(juju, unit, "seeded-in-ubuntu-indexer.service")
    juju.ssh(unit, "sudo systemctl restart --no-block update-archive-mirror.service")
    wait_oneshot_finished(
        juju,
        unit=unit,
        service="update-archive-mirror.service",
        baseline_invocation_id=archive_baseline,
    )

    # update-archive-mirror triggers update-germinate via OnSuccess=; wait for it
    # to publish the combined archive+germinate snapshot the mismatch reports read.
    wait_oneshot_finished(
        juju,
        unit=unit,
        service="update-germinate.service",
        timeout_sec=7200,
        baseline_invocation_id=germinate_baseline,
    )

    # update-germinate triggers update-mismatches via OnSuccess= in turn.
    wait_oneshot_finished(
        juju,
        unit=unit,
        service="update-mismatches.service",
        baseline_invocation_id=mismatches_baseline,
    )

    # update-germinate also triggers seeded-in-ubuntu-indexer via OnSuccess=.
    wait_oneshot_finished(
        juju,
        unit=unit,
        service="seeded-in-ubuntu-indexer.service",
        baseline_invocation_id=indexer_baseline,
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

    # seeded-in-ubuntu-indexer should have produced at least one index file.
    ls = juju.ssh(
        "ubuntu-static-reports/0",
        "ls /srv/staticreports/www/seeded-in-ubuntu/seeded-*.json.gz 2>/dev/null | head -1",
    )
    assert ls.strip().startswith("/srv/staticreports/www/seeded-in-ubuntu/seeded-"), (
        f"No seeded index files produced (got: {ls.strip()!r})"
    )
