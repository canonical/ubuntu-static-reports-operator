# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for the charm.

These tests only cover those methods that do not require internet access,
and do not attempt to manipulate the underlying machine.
"""

from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
def test_install_success(systemd_mock, install_mock, ctx, base_state):
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
def test_install_failure(mock, exception, ctx, base_state):
    mock.side_effect = exception
    out = ctx.run(ctx.on.install(), base_state)
    assert out.unit_status == BlockedStatus(
        "Failed to set up the environment. Check `juju debug-log` for details."
    )


@patch("charm.StaticReports.install")
@patch("charm.StaticReports.setup_systemd_units")
def test_upgrade_success(systemd_mock, install_mock, ctx, base_state):
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
def test_upgrade_failure(install_mock, exception, ctx, base_state):
    install_mock.side_effect = exception
    out = ctx.run(ctx.on.upgrade_charm(), base_state)
    assert out.unit_status == BlockedStatus(
        "Failed to set up the environment. Check `juju debug-log` for details."
    )


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_config_changed(configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx, base_state):
    lp_oauth_prop_mock.return_value = "fake-token"
    lpoauth_mock.return_value = True
    out = ctx.run(ctx.on.config_changed(), base_state)
    assert out.unit_status == ActiveStatus()
    configure_mock.assert_called()
    lpoauth_mock.assert_called()


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_config_changed_failed_bad_config(
    configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx, base_state
):
    configure_mock.side_effect = ValueError
    out = ctx.run(ctx.on.config_changed(), base_state)
    assert out.unit_status == BlockedStatus(
        "Invalid configuration. Check `juju debug-log` for details."
    )
    assert not lpoauth_mock.called


@patch("charm.StaticReports.start")
def test_start_success(start_mock, ctx, base_state):
    out = ctx.run(ctx.on.start(), base_state)
    assert out.unit_status == ActiveStatus()
    assert start_mock.called
    assert out.opened_ports == {TCPPort(port=80, protocol="tcp")}


@patch("charm.StaticReports.start")
@pytest.mark.parametrize("exception", [CalledProcessError(1, "foo")])
def test_start_failure(start_mock, exception, ctx, base_state):
    start_mock.side_effect = exception
    out = ctx.run(ctx.on.start(), base_state)
    assert out.unit_status == BlockedStatus(
        "Failed to start services. Check `juju debug-log` for details."
    )
    assert out.opened_ports == frozenset()


@patch("charm.StaticReports.refresh_report")
def test_staticreports_refresh_success(refresh_report_mock, ctx, base_state):
    out = ctx.run(ctx.on.action("refresh"), base_state)
    assert ctx.action_logs == ["Refreshing the report"]
    assert out.unit_status == ActiveStatus()
    assert refresh_report_mock.called


@patch("charm.StaticReports.refresh_report")
def test_staticreports_refresh_failure(refresh_report_mock, ctx, base_state):
    refresh_report_mock.side_effect = CalledProcessError(1, "refresh")
    out = ctx.run(ctx.on.action("refresh"), base_state)
    assert out.unit_status == ActiveStatus(
        "Failed to refresh the report. Check `juju debug-log` for details."
    )


@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
@patch("charm.socket.getfqdn")
@patch("ops.model.Model.get_binding")
def test_get_external_url_fqdn_fallback(
    get_binding_mock, getfqdn_mock, configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx
):
    """Test that FQDN is used when no juju-info binding and no ingress."""
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
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_get_external_url_juju_info_binding(configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx):
    """Test that unit IP is used when juju-info binding exists."""
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
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
@patch("charm.StaticReports.configure_lpoauthkey")
@patch("charm.StaticReports.configure_url")
def test_get_external_url_ingress_url(configure_mock, lpoauth_mock, lp_oauth_prop_mock, ctx):
    """Test that ingress URL takes priority when available."""
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


def test_config_changed_lp_secret_not_found(ctx, base_state):
    """If the lp secret is not configured the unit should be blocked."""
    out = ctx.run(ctx.on.config_changed(), base_state)
    assert out.unit_status == BlockedStatus("Launchpad oauth token config missing.")


@patch("charm.StaticReports.configure_lpoauthkey")
@patch(
    "charm.UbuntuStaticReportsCharm._lpuser_lp_oauthkey",
    new_callable=lambda: __import__("unittest.mock").mock.PropertyMock,
)
def test_config_changed_lpoauthkey_failure(lp_oauth_prop_mock, configure_lpoauth_mock, ctx):
    """If configure_lpoauthkey fails the unit should be blocked."""
    lp_oauth_prop_mock.return_value = "fake-token"
    configure_lpoauth_mock.return_value = False
    out = ctx.run(ctx.on.config_changed(), State(leader=True))
    assert out.unit_status == BlockedStatus("Failed to update Launchpad oauth token.")


def test_lpuser_secret_get_secret_raises():
    """When Model.get_secret raises, _lpuser_secret should handle it."""
    dummy = SimpleNamespace()
    dummy.config = {"lpuser_secret_id": "missing"}
    dummy.model = MagicMock()
    dummy.model.get_secret.side_effect = ops.SecretNotFoundError
    result = UbuntuStaticReportsCharm._lpuser_secret.fget(dummy)
    assert result is None


def test_lpuser_lp_oauthkey_keyerror():
    """When secret.get_content is missing lpoauthkey, property handles KeyError."""
    dummy = SimpleNamespace()
    fake_secret = MagicMock()
    fake_secret.get_content.return_value = {}
    dummy._lpuser_secret = fake_secret
    result = UbuntuStaticReportsCharm._lpuser_lp_oauthkey.fget(dummy)
    assert result is None
