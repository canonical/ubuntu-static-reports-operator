#!/usr/bin/env python3
# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Charmed Operator for Ubuntu Static Reports."""

import logging
import shutil
import socket
from subprocess import CalledProcessError

import ops
from charmlibs.apt import PackageError, PackageNotFoundError
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer

from staticreports import StaticReports

logger = logging.getLogger(__name__)

PORT = 80


class UbuntuStaticReportsCharm(ops.CharmBase):
    """Charmed Operator for Ubuntu Static Reports."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.ingress = IngressRequirer(self, port=PORT, strip_prefix=True, relation_name="ingress")

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.upgrade_charm, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.refresh_action, self._on_refresh_report)

        # Ingress URL changes require updating the configuration and also regenerating sitemaps,
        # therefore we can bind events for this relation to the config_changed event.
        framework.observe(self.ingress.on.ready, self._on_config_changed)
        framework.observe(self.ingress.on.revoked, self._on_config_changed)

        self._staticreports = StaticReports()

    def _on_install(self, event: ops.EventBase):
        """Handle install, upgrade, config-changed, or ingress events."""
        self.unit.status = ops.MaintenanceStatus("Setting up environment")
        try:
            self._staticreports.install()
            self._staticreports.setup_systemd_units()
        except (
            CalledProcessError,
            PackageError,
            PackageNotFoundError,
            IOError,
            OSError,
            shutil.Error,
        ):
            self.unit.status = ops.BlockedStatus(
                "Failed to set up the environment. Check `juju debug-log` for details."
            )
            return
        self.unit.status = ops.ActiveStatus()

    def _on_start(self, event: ops.StartEvent):
        """Start the services of the static reports."""
        self.unit.status = ops.MaintenanceStatus("Starting Static Reports")
        try:
            self._staticreports.start()
        except CalledProcessError:
            self.unit.status = ops.BlockedStatus(
                "Failed to start services. Check `juju debug-log` for details."
            )
            return
        self.unit.set_ports(PORT)
        self.unit.status = ops.ActiveStatus()

    def _on_config_changed(self, event):
        """Update configuration."""
        self.unit.status = ops.MaintenanceStatus("Updating configuration")
        try:
            self._staticreports.configure(self._get_external_url())
        except ValueError:
            self.unit.status = ops.BlockedStatus(
                "Invalid configuration. Check `juju debug-log` for details."
            )
            return
        self.unit.status = ops.ActiveStatus()

    def _on_refresh_report(self, event: ops.ActionEvent):
        """Refresh all reports."""
        self.unit.status = ops.MaintenanceStatus("Refreshing the report")

        try:
            event.log("Refreshing the report")
            self._staticreports.refresh_report()
        except (CalledProcessError, IOError):
            event.log("Report refresh failed")
            self.unit.status = ops.ActiveStatus(
                "Failed to refresh the report. Check `juju debug-log` for details."
            )
            return
        self.unit.status = ops.ActiveStatus()

    def _get_external_url(self) -> str:
        """Report URL to access Ubuntu Static Reports."""
        # Default: FQDN
        external_url = f"http://{socket.getfqdn()}:{PORT}"
        # If can connect to juju-info, get unit IP
        if binding := self.model.get_binding("juju-info"):
            unit_ip = str(binding.network.bind_address)
            external_url = f"http://{unit_ip}:{PORT}"
        # If ingress is set, get ingress url
        if self.ingress.url:
            external_url = self.ingress.url
        return external_url


if __name__ == "__main__":  # pragma: nocover
    ops.main(UbuntuStaticReportsCharm)
