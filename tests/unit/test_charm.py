# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for the charm.

These tests only cover those methods that do not require internet access,
and do not attempt to manipulate the underlying machine.
"""

from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import ops
import pytest
from charmlibs.apt import PackageError, PackageNotFoundError
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

from charm import UbuntuStaticReportsCharm


@pytest.fixture
def ctx():
    return Context(UbuntuStaticReportsCharm)


@pytest.fixture
def base_state(ctx):
    return State(leader=True)


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
@pytest.mark.parametrize("exception", [CalledProcessError(1, "foo")])
def test_start_event_blocks_charm_when_service_start_fails(start_mock, exception, ctx, base_state):
    start_mock.side_effect = exception

    out = ctx.run(ctx.on.start(), base_state)

    assert out.unit_status == BlockedStatus(
        "Failed to start services. Check `juju debug-log` for details."
    )
    assert out.opened_ports == frozenset()


@patch("charm.StaticReports.refresh_report")
def test_refresh_action_triggers_report_refresh_and_logs_message(
    refresh_report_mock, ctx, base_state
):
    out = ctx.run(ctx.on.action("refresh"), base_state)

    assert ctx.action_logs == ["Refreshing the report"]
    assert out.unit_status == ActiveStatus()
    assert refresh_report_mock.called


@patch("charm.StaticReports.refresh_report")
def test_refresh_action_sets_status_message_when_refresh_fails(
    refresh_report_mock, ctx, base_state
):
    refresh_report_mock.side_effect = CalledProcessError(1, "refresh")

    out = ctx.run(ctx.on.action("refresh"), base_state)

    assert out.unit_status == ActiveStatus(
        "Failed to refresh the report. Check `juju debug-log` for details."
    )


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


def test_config_changed_event_blocks_charm_when_lp_secret_not_configured(ctx, base_state):
    out = ctx.run(ctx.on.config_changed(), base_state)

    assert out.unit_status == BlockedStatus("Launchpad oauth token config missing.")


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

    result = UbuntuStaticReportsCharm._lpuser_secret.fget(dummy)

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

    result = UbuntuStaticReportsCharm._lpuser_lp_oauthkey.fget(dummy)

    assert result is None
