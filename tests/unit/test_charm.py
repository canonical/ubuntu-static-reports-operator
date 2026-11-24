# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for the charm.

These tests only cover those methods that do not require internet access,
and do not attempt to manipulate the underlying machine.
"""

import json
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

import ops
import ops.testing
import pytest
from charmlibs.apt import PackageError, PackageNotFoundError
from charms.operator_libs_linux.v1.systemd import SystemdError
from ops.testing import (
    ActiveStatus,
    Address,
    BindAddress,
    BlockedStatus,
    Context,
    Network,
    Relation,
    State,
    TCPPort,
)

import staticreports_health as health
from charm import UbuntuStaticReportsCharm
from staticreports import ReportAlreadyRunningError


@pytest.fixture
def ctx():
    return Context(UbuntuStaticReportsCharm)


@pytest.fixture
def base_state(ctx):
    return State(leader=True)


@pytest.fixture(autouse=True)
def mock_configure_archive_mirror():
    """Stub configure_archive_mirror so unit tests never write to the host."""
    with patch("charm.StaticReports.configure_archive_mirror") as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_configure_mismatches():
    """Stub configure_mismatches so unit tests never write to the host."""
    with patch("charm.StaticReports.configure_mismatches") as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_configure_seeded_in_ubuntu_index():
    """Stub configure_seeded_in_ubuntu_indexer so unit tests never write to the host."""
    with patch("charm.StaticReports.configure_seeded_in_ubuntu_indexer") as mock:
        yield mock


@patch("charm.StaticReports.install")
@patch("charm.StaticReports.setup_systemd_units")
def test_install_event_sets_active_status_on_success(systemd_mock, install_mock, ctx, base_state):
    install_mock.return_value = True
    systemd_mock.return_value = True

    out = ctx.run(ctx.on.install(), base_state)

    assert out.unit_status == ActiveStatus()
    assert install_mock.called


@patch("charm.StaticReports.install")
@pytest.mark.parametrize(
    "exception",
    [
        PackageError,
        PackageNotFoundError,
        CalledProcessError(1, "foo"),
    ],
)
def test_install_event_blocks_charm_on_environment_setup_failure(
    install_mock, exception, ctx, base_state
):
    install_mock.side_effect = exception

    out = ctx.run(ctx.on.install(), base_state)

    assert out.unit_status == BlockedStatus(
        "Failed to set up the environment. Check `juju debug-log` for details."
    )


@patch("charm.StaticReports.install")
@patch("charm.StaticReports.setup_systemd_units")
def test_upgrade_charm_event_sets_active_status_on_success(
    systemd_mock, install_mock, ctx, base_state
):
    install_mock.return_value = True
    systemd_mock.return_value = True

    out = ctx.run(ctx.on.upgrade_charm(), base_state)

    assert out.unit_status == ActiveStatus()
    assert install_mock.called


@patch("charm.StaticReports.install")
@patch("charm.StaticReports.setup_systemd_units")
def test_upgrade_charm_event_blocks_when_systemd_reload_fails(
    systemd_mock, install_mock, ctx, base_state
):
    systemd_mock.side_effect = SystemdError("systemctl daemon-reload failed")

    out = ctx.run(ctx.on.upgrade_charm(), base_state)

    install_mock.assert_called_once()
    systemd_mock.assert_called_once()
    assert out.unit_status == BlockedStatus(
        "Failed to set up the environment. Check `juju debug-log` for details."
    )


@patch("charm.StaticReports.install")
@pytest.mark.parametrize(
    "exception",
    [
        PackageError,
        PackageNotFoundError,
        CalledProcessError(1, "foo"),
    ],
)
def test_upgrade_charm_event_blocks_charm_on_environment_setup_failure(
    install_mock, exception, ctx, base_state
):
    install_mock.side_effect = exception

    out = ctx.run(ctx.on.upgrade_charm(), base_state)

    assert out.unit_status == BlockedStatus(
        "Failed to set up the environment. Check `juju debug-log` for details."
    )


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_config_changed_event_configures_url_and_oauth_on_success(
    configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx, base_state
):
    lp_oauth_prop_mock.return_value = "fake-token"
    lpoauth_mock.return_value = True

    out = ctx.run(ctx.on.config_changed(), base_state)

    assert out.unit_status == ActiveStatus()
    configure_mock.assert_called()
    lpoauth_mock.assert_called()


def test_config_changed_event_writes_archive_mirror_overrides_from_config(
    mock_configure_archive_mirror, ctx
):
    """The archive rsync configs and mirror_dir options are passed to configure_archive_mirror."""
    state = State(
        leader=True,
        config={
            "rsync_archive_source": "rsync://host/dists/",
            "rsync_ports_source": "rsync://ports-host/ubuntu-ports/dists/",
            "mirror_dir": "/srv/mirror",
        },
    )

    ctx.run(ctx.on.config_changed(), state)

    mock_configure_archive_mirror.assert_called_once_with(
        archive_rsync_source="rsync://host/dists/",
        ports_rsync_source="rsync://ports-host/ubuntu-ports/dists/",
        mirror_dir="/srv/mirror",
    )


def test_config_changed_event_writes_mismatches_overrides_from_config(
    mock_configure_mismatches, ctx
):
    """The mirror_dir config option is forwarded to configure_mismatches."""
    state = State(leader=True, config={"mirror_dir": "/srv/mirror"})

    ctx.run(ctx.on.config_changed(), state)

    mock_configure_mismatches.assert_called_once_with(mirror_dir="/srv/mirror")


@patch("charm.StaticReports.setup_storage")
def test_storage_attached_event_creates_report_directories(setup_storage_mock, ctx):
    """storage-attached (re)creates the report tree on the mounted volume."""
    storage = ops.testing.Storage("staticreports")
    state = State(leader=True, storages={storage})

    ctx.run(ctx.on.storage_attached(storage), state)

    setup_storage_mock.assert_called_once()


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_config_changed_event_blocks_charm_on_invalid_configuration(
    configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx, base_state
):
    configure_mock.side_effect = ValueError

    out = ctx.run(ctx.on.config_changed(), base_state)

    assert out.unit_status == BlockedStatus(
        "Invalid configuration. Check `juju debug-log` for details."
    )
    assert not lpoauth_mock.called


@patch("charm.StaticReports.start")
def test_start_event_opens_port_80_and_sets_active_status(start_mock, ctx, base_state):
    out = ctx.run(ctx.on.start(), base_state)

    assert out.unit_status == ActiveStatus()
    assert start_mock.called
    assert out.opened_ports == {TCPPort(port=80, protocol="tcp")}


@patch("charm.StaticReports.start")
@pytest.mark.parametrize(
    "exception",
    [CalledProcessError(1, "foo"), SystemdError("systemctl failed")],
)
def test_start_event_blocks_charm_when_service_start_fails(start_mock, exception, ctx, base_state):
    start_mock.side_effect = exception

    out = ctx.run(ctx.on.start(), base_state)

    assert out.unit_status == BlockedStatus(
        "Failed to start services. Check `juju debug-log` for details."
    )
    assert out.opened_ports == frozenset()


def _diagnostic_document(status="ok"):
    return {
        "schema_version": 1,
        "generated_at": "2026-11-24T12:00:00+00:00",
        "status": status,
        "summary": "test summary",
        "reports": [
            {
                "service": "update-nbs",
                "status": status,
                "outcome": "published" if status == "ok" else "failed",
                "reason": "test reason",
            }
        ],
        "serving": {"status": "ok", "reason": "probe passed"},
    }


@patch("charm.health.HealthCollector")
def test_diagnose_action_returns_shared_document_for_all_services(
    collector_class, ctx, base_state
):
    collector_class.return_value.collect.return_value = _diagnostic_document()
    out = ctx.run(ctx.on.action("static-reports-diagnose"), base_state)
    collector_class.return_value.collect.assert_called_once_with(None)
    assert ctx.action_results["schema-version"] == "1"
    assert ctx.action_results["status"] == "ok"
    assert json.loads(ctx.action_results["report"])["reports"][0]["service"] == "update-nbs"
    assert out.unit_status == base_state.unit_status


@patch("charm.health.HealthCollector")
def test_diagnose_action_forwards_service_and_returns_unhealthy_result(
    collector_class, ctx, base_state
):
    collector_class.return_value.collect.return_value = _diagnostic_document("critical")
    ctx.run(
        ctx.on.action("static-reports-diagnose", params={"service": "update-nbs"}),
        base_state,
    )
    collector_class.return_value.collect.assert_called_once_with("update-nbs")
    assert ctx.action_results["status"] == "critical"


@patch("charm.health.HealthCollector")
def test_diagnose_action_fails_only_when_collection_fails(collector_class, ctx, base_state):
    collector_class.return_value.collect.side_effect = health.HealthCollectionError("no systemctl")
    with pytest.raises(ops.testing.ActionFailed) as failure:
        ctx.run(ctx.on.action("static-reports-diagnose"), base_state)
    assert "no systemctl" in failure.value.message


@patch("charm.health.HealthCollector")
@patch("charm.StaticReports.run_report")
def test_run_report_action_runs_one_service_and_returns_health(
    run_report_mock, collector_class, ctx, base_state
):
    collector_class.return_value.collect.return_value = _diagnostic_document()
    out = ctx.run(
        ctx.on.action("run-report", params={"service": "update-nbs"}),
        base_state,
    )
    run_report_mock.assert_called_once_with("update-nbs", restart=False)
    collector_class.return_value.collect.assert_called_once_with("update-nbs")
    assert ctx.action_logs == [
        "Running update-nbs. This may take a while; use --background for long reports."
    ]
    assert ctx.action_results["outcome"] == "published"
    assert out.unit_status == ActiveStatus()


@patch("charm.StaticReports.run_report")
def test_run_report_action_refuses_unintended_interruption(run_report_mock, ctx, base_state):
    run_report_mock.side_effect = ReportAlreadyRunningError(
        "already running; restart=true required"
    )
    with pytest.raises(ops.testing.ActionFailed) as failure:
        ctx.run(
            ctx.on.action("run-report", params={"service": "update-nbs"}),
            base_state,
        )
    assert "restart=true" in failure.value.message


@patch("charm.StaticReports.run_report")
@pytest.mark.parametrize(
    "exception",
    [SystemdError("start failed"), TimeoutExpired("systemctl", 10)],
)
def test_run_report_action_fails_on_service_operation_error(
    run_report_mock, exception, ctx, base_state
):
    run_report_mock.side_effect = exception
    with pytest.raises(ops.testing.ActionFailed) as failure:
        ctx.run(
            ctx.on.action("run-report", params={"service": "update-nbs"}),
            base_state,
        )
    assert "Failed to run update-nbs" in failure.value.message


@patch("charm.health.HealthCollector")
@patch("charm.StaticReports.run_report")
def test_run_report_action_fails_when_service_is_condition_skipped(
    run_report_mock, collector_class, ctx, base_state
):
    document = _diagnostic_document("waiting")
    document["reports"][0]["outcome"] = "condition_skipped"
    collector_class.return_value.collect.return_value = document
    with pytest.raises(ops.testing.ActionFailed) as failure:
        ctx.run(
            ctx.on.action(
                "run-report",
                params={"service": "permissions-report", "restart": True},
            ),
            base_state,
        )
    run_report_mock.assert_called_once_with("permissions-report", restart=True)
    assert "skipped" in failure.value.message
    assert ctx.action_results["outcome"] == "condition_skipped"


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
@patch("charm.socket.getfqdn")
@patch("ops.model.Model.get_binding")
def test_get_external_url_uses_fqdn_when_no_network_binding_or_ingress(
    get_binding_mock, getfqdn_mock, configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx
):
    """Test FQDN fallback when bindings unavailable.

    When neither juju-info binding nor ingress relation is available, the charm
    falls back to using the unit's FQDN to construct the external URL for Launchpad
    callback registration. This ensures the service remains functional in basic deployments.
    """
    get_binding_mock.return_value = None
    getfqdn_mock.return_value = "test-host.example.com"
    state = State(leader=True)
    lp_oauth_prop_mock.return_value = "fake-token"
    lpoauth_mock.return_value = True

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == ActiveStatus()
    configure_mock.assert_called_once_with("http://test-host.example.com:80")
    lpoauth_mock.assert_called()


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_get_external_url_uses_juju_info_binding_ip_when_available(
    configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx
):
    state = State(
        leader=True,
        networks={
            Network(
                "juju-info",
                bind_addresses=[BindAddress(addresses=[Address("192.168.1.10")])],
            ),
        },
    )
    lp_oauth_prop_mock.return_value = "fake-token"
    lpoauth_mock.return_value = True

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == ActiveStatus()
    configure_mock.assert_called_once_with("http://192.168.1.10:80")
    lpoauth_mock.assert_called()


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_get_external_url_prioritizes_ingress_url_over_binding(
    configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx
):
    """Test ingress URL priority.

    Ingress URL takes priority over network bindings when determining the external URL.
    This ensures proper routing through ingress controllers in Kubernetes deployments.
    """
    ingress_relation = Relation(
        endpoint="ingress",
        interface="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": '{"url": "https://ingress.example.com/"}'},
    )
    state = State(
        leader=True,
        networks={
            Network(
                "juju-info",
                bind_addresses=[BindAddress(addresses=[Address("192.168.1.10")])],
            ),
        },
        relations={ingress_relation},
    )
    lp_oauth_prop_mock.return_value = "fake-token"
    lpoauth_mock.return_value = True

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == ActiveStatus()
    configure_mock.assert_called_once_with("https://ingress.example.com/")
    lpoauth_mock.assert_called()


def test_config_changed_sets_active_message_when_lp_secret_not_configured(ctx, base_state):
    """Without the optional LP token the charm stays active with a hint."""
    out = ctx.run(ctx.on.config_changed(), base_state)

    assert out.unit_status == ActiveStatus(
        "Launchpad oauth token not configured - authenticated reports are disabled."
    )


@patch("charm.StaticReports.configure_lpoauthkey")
@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=PropertyMock,
)
def test_config_changed_event_blocks_charm_on_oauth_key_config_failure(
    lp_oauth_prop_mock, configure_lpoauth_mock, ctx
):
    lp_oauth_prop_mock.return_value = "fake-token"
    configure_lpoauth_mock.return_value = False

    out = ctx.run(ctx.on.config_changed(), State(leader=True))

    assert out.unit_status == BlockedStatus("Failed to update Launchpad oauth token.")


def test_lpuser_secret_property_returns_none_when_secret_not_found():
    """Test _lpuser_secret handles missing secrets gracefully.

    The _lpuser_secret property gracefully handles SecretNotFoundError by returning None
    instead of raising, allowing the charm to detect and report missing configuration.
    """
    dummy = SimpleNamespace()
    dummy.config = {"lpuser_secret_id": "missing"}
    dummy.model = MagicMock()
    dummy.model.get_secret.side_effect = ops.SecretNotFoundError
    charm = cast(UbuntuStaticReportsCharm, dummy)

    result = UbuntuStaticReportsCharm._lpuser_secret.fget(charm)

    assert result is None


def test_lpuser_lp_oauthkey_property_returns_none_when_key_missing_from_secret():
    """Test _lpuser_lp_oauthkey handles missing key gracefully.

    The _lpuser_lp_oauthkey property gracefully handles missing 'lpoauthkey' in secret
    content by returning None instead of raising KeyError.
    """
    dummy = SimpleNamespace()
    fake_secret = MagicMock()
    fake_secret.get_content.return_value = {}
    dummy._lpuser_secret = fake_secret
    charm = cast(UbuntuStaticReportsCharm, dummy)

    result = UbuntuStaticReportsCharm._lpuser_lp_oauthkey.fget(charm)

    assert result is None
