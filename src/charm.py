#!/usr/bin/env python3
# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Charmed Operator for Ubuntu Static Reports."""

import json
import logging
import shutil
import socket
from pathlib import Path
from subprocess import CalledProcessError, SubprocessError

import ops
from charmlibs.apt import PackageError, PackageNotFoundError
from charms.operator_libs_linux.v1.systemd import SystemdError
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer

import staticreports_health as health
from staticreports import ReportAlreadyRunningError, StaticReports

logger = logging.getLogger(__name__)

PORT = 80


class UbuntuStaticReportsCharm(ops.CharmBase):
    """Charmed Operator for Ubuntu Static Reports."""

    _stored = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._stored.set_default(
            health_candidate="",
            health_candidate_count=0,
            published_health_document=None,
            health_status_message="",
        )

        self.ingress = IngressRequirer(self, port=PORT, strip_prefix=True, relation_name="ingress")

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.upgrade_charm, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on["run-report"].action, self._on_run_report)
        self.framework.observe(
            self.on["static-reports-diagnose"].action,
            self._on_static_reports_diagnose,
        )
        self.framework.observe(self.on.staticreports_storage_attached, self._on_storage_attached)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

        framework.observe(self.ingress.on.ready, self._on_config_changed)
        framework.observe(self.ingress.on.revoked, self._on_config_changed)

        self._staticreports = StaticReports()

    @property
    def _lpuser_secret(self) -> ops.model.Secret | None:
        secret_id: str = ""

        try:
            secret_id = str(self.config["lpuser_secret_id"])
        except KeyError:
            logger.warning("lpuser_secret_id config not available, unable to extract keys.")
            return None

        try:
            return self.model.get_secret(id=secret_id)
        except (ops.SecretNotFoundError, ops.model.ModelError):
            logger.warning("Failed to get lpuser secret with id %s", secret_id)

        return None

    @property
    def _lpuser_lp_oauthkey(self) -> str | None:
        secret = self._lpuser_secret

        if secret is not None:
            logger.debug("config - got secret id %s, returning key lpoauthkey", secret)
            try:
                return secret.get_content(refresh=True)["lpoauthkey"]
            except KeyError:
                logger.warning("lpoauthkey not found in lpuser secret.")

        return None

    def _on_install(self, event: ops.EventBase):
        """Handle install, upgrade, config-changed, or ingress events."""
        self.unit.status = ops.MaintenanceStatus("Setting up environment")
        try:
            self._staticreports.install()
            self._staticreports.setup_systemd_units()
        except (
            CalledProcessError,
            SubprocessError,
            SystemdError,
            PackageError,
            PackageNotFoundError,
            IOError,
            OSError,
            shutil.Error,
        ) as e:
            logger.warning("Failed to set up the environment: %s", e)
            self.unit.status = ops.BlockedStatus(
                "Failed to set up the environment. Check `juju debug-log` for details."
            )
            return
        self.unit.status = ops.ActiveStatus()

    def _on_storage_attached(self, event: ops.StorageAttachedEvent):
        """(Re)create the report tree on the freshly attached storage volume."""
        self._staticreports.setup_storage()

    def _on_start(self, event: ops.StartEvent):
        """Start the services of the static reports."""
        self.unit.status = ops.MaintenanceStatus("Starting Static Reports")
        try:
            self._staticreports.start()
        except (CalledProcessError, SystemdError):
            self.unit.status = ops.BlockedStatus(
                "Failed to start services. Check `juju debug-log` for details."
            )
            return
        self.unit.set_ports(PORT)
        self.unit.status = ops.ActiveStatus()

    def _on_config_changed(self, event):
        """Update configuration."""
        logger.debug("config changed event")
        self.unit.status = ops.MaintenanceStatus("Updating configuration")
        try:
            self._staticreports.configure_url(self._get_external_url())
        except ValueError:
            self.unit.status = ops.BlockedStatus(
                "Invalid configuration. Check `juju debug-log` for details."
            )
            return False
        logger.debug("config change done - url set")

        self._staticreports.configure_archive_mirror(
            archive_rsync_source=str(self.config.get("rsync_archive_source", "")),
            ports_rsync_source=str(self.config.get("rsync_ports_source", "")),
            mirror_dir=str(self.config.get("mirror_dir", "")),
        )
        logger.debug("config change done - archive-sync overrides written")

        self._staticreports.configure_mismatches(
            mirror_dir=str(self.config.get("mirror_dir", "")),
        )
        logger.debug("config change done - mismatches overrides written")

        self._staticreports.configure_seeded_in_ubuntu_indexer(
            mirror_dir=str(self.config.get("mirror_dir", "")),
        )
        logger.debug("config change done - seeded-in-ubuntu-indexer overrides written")

        lp_key_data = self._lpuser_lp_oauthkey
        if lp_key_data is None:
            if not self._staticreports.clear_lpoauthkey():
                self.unit.status = ops.BlockedStatus(
                    "Failed to remove stale Launchpad oauth token."
                )
                return False
            secret_id = str(self.config.get("lpuser_secret_id", "") or "").strip()
            if secret_id:
                self.unit.status = ops.BlockedStatus(
                    "Launchpad oauth token is configured but unavailable or invalid."
                )
            else:
                self.unit.status = ops.ActiveStatus(
                    "Authenticated reports waiting for Launchpad credentials."
                )
            return False
        else:
            logger.debug("config - got lpoauthkey (length %d)", len(lp_key_data))
            if not self._staticreports.configure_lpoauthkey(lp_key_data):
                self.unit.status = ops.BlockedStatus("Failed to update Launchpad oauth token.")
                return False
        logger.debug("config change done - lp oauth key set")

        self.unit.status = ops.ActiveStatus()

    def _health_collector(self) -> health.HealthCollector:
        mirror_dir = Path(str(self.config.get("mirror_dir", "") or health.DEFAULT_MIRROR_DIR))
        return health.HealthCollector(mirror_dir=mirror_dir)

    def _on_static_reports_diagnose(self, event: ops.ActionEvent):
        """Return the shared local health document without changing workload state."""
        service = event.params.get("service") or None
        try:
            document = self._health_collector().collect(service)
        except (health.HealthCollectionError, OSError, ValueError) as error:
            event.fail(f"Could not collect health evidence: {error}")
            return
        event.set_results(
            {
                "schema-version": str(document["schema_version"]),
                "generated-at": document["generated_at"],
                "status": document["status"],
                "summary": document["summary"],
                "report": json.dumps(document, sort_keys=True, separators=(",", ":")),
            }
        )

    def _on_run_report(self, event: ops.ActionEvent):
        """Run one report, or explicitly interrupt and restart it."""
        service = str(event.params["service"])
        restart = bool(event.params.get("restart", False))
        self.unit.status = ops.MaintenanceStatus(f"Running {service}")
        try:
            event.log(
                f"Running {service}. This may take a while; use --background for long reports."
            )
            self._staticreports.run_report(service, restart=restart)
            document = self._health_collector().collect(service)
        except (ReportAlreadyRunningError, ValueError) as error:
            self.unit.status = ops.ActiveStatus()
            event.fail(str(error))
            return
        except (SubprocessError, SystemdError, IOError, health.HealthCollectionError) as error:
            self.unit.status = ops.ActiveStatus()
            event.fail(f"Failed to run {service}: {error}")
            return
        self.unit.status = ops.ActiveStatus()
        report = document["reports"][0]
        event.set_results(
            {
                "service": service,
                "status": report["status"],
                "outcome": report["outcome"],
                "reason": report["reason"],
                "report": json.dumps(document, sort_keys=True, separators=(",", ":")),
            }
        )
        if report["outcome"] == "condition_skipped":
            event.fail(f"{service} was skipped: {report['reason']}")
            return

    def _on_update_status(self, event: ops.UpdateStatusEvent):
        """Refresh the cheap shared health snapshot without running reports."""
        try:
            document = self._health_collector().collect()
        except (health.HealthCollectionError, OSError, ValueError) as error:
            logger.warning("Could not collect workload health: %s", error)
            document = {
                "schema_version": health.SCHEMA_VERSION,
                "generated_at": health.iso_time(),
                "status": "unknown",
                "summary": f"Could not collect health evidence: {error}",
                "reports": [],
                "serving": None,
            }
        self._record_health_document(document)

    def _record_health_document(self, document: dict):
        """Publish matching non-OK samples twice, but clear them on the first OK."""
        status = document["status"]
        if status == "ok":
            self._stored.health_candidate = ""
            self._stored.health_candidate_count = 0
            self._stored.published_health_document = document
            return
        if self._stored.health_candidate == status:
            self._stored.health_candidate_count += 1
        else:
            self._stored.health_candidate = status
            self._stored.health_candidate_count = 1
        if self._stored.health_candidate_count >= 2:
            self._stored.published_health_document = document

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent):
        """Reconcile lifecycle status with the latest published health summary."""
        lifecycle_status = self.unit.status
        health_status = self._published_health_status()
        health_generated = (
            lifecycle_status.name in {"active", "blocked"}
            and health_status is not None
            and lifecycle_status.message == self._stored.health_status_message
        )
        preserve_lifecycle = not health_generated and (
            lifecycle_status.name in {"blocked", "maintenance", "waiting"}
            or (
                lifecycle_status.name == "active"
                and self._stored.published_health_document is None
            )
            or (
                lifecycle_status.name == "active"
                and bool(lifecycle_status.message)
                and not health_generated
            )
        )
        if preserve_lifecycle:
            event.add_status(lifecycle_status)
        if health_status:
            event.add_status(health_status)
            self._stored.health_status_message = health_status.message
        else:
            self._stored.health_status_message = ""

    def _published_health_status(self):
        document = self._stored.published_health_document
        if document is None:
            return None
        status = document["status"]
        if status == "ok":
            return ops.ActiveStatus()
        if status == "waiting":
            return ops.ActiveStatus("Authenticated reports waiting for Launchpad credentials.")
        if status == "unknown":
            return ops.ActiveStatus("Report health unknown; run static-reports-diagnose.")
        attention = sum(report["status"] != "ok" for report in document["reports"])
        if serving := document.get("serving"):
            attention += serving["status"] != "ok"
        noun = "check" if attention == 1 else "checks"
        verb = "needs" if attention == 1 else "need"
        if status == "critical":
            return ops.BlockedStatus(
                f"Local workload critical: {attention} {noun} {verb} attention; "
                "run static-reports-diagnose."
            )
        return ops.ActiveStatus(
            f"Degraded: {attention} {noun} {verb} attention; run static-reports-diagnose."
        )

    def _get_external_url(self) -> str:
        """Report URL to access Ubuntu Static Reports."""
        external_url = f"http://{socket.getfqdn()}:{PORT}"
        if binding := self.model.get_binding("juju-info"):
            unit_ip = str(binding.network.bind_address)
            external_url = f"http://{unit_ip}:{PORT}"
        if self.ingress.url:
            external_url = self.ingress.url
        return external_url


if __name__ == "__main__":  # pragma: nocover
    ops.main(UbuntuStaticReportsCharm)
