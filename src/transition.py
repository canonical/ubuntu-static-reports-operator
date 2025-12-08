# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Representation of the transition service."""

import logging
import os
import shutil
from pathlib import Path
from subprocess import PIPE, STDOUT, CalledProcessError, run
from urllib.parse import urlparse

import charms.operator_libs_linux.v1.systemd as systemd
from charmlibs import apt
from charmlibs.apt import PackageError, PackageNotFoundError

logger = logging.getLogger(__name__)

# Packages installed as part of the update process.
PACKAGES = [
    "ben",
    "dctrl-tools",
    "git",
    "nginx-light",
    "rsync",
]

SRVDIR = Path("/srv/transitions/transition-tracker")
REPO_URL = (
    "https://git.launchpad.net/~ubuntu-transition-trackers/ubuntu-transition-tracker/+git/configs"
)
MIRROR_DIR = Path("/srv/mirrors")
MIRROR_DISTS = MIRROR_DIR / "ubuntu" / "dists"

NGINX_SITE_CONFIG_PATH = Path("/etc/nginx/conf.d/transition.conf")


class Transition:
    """Represent a transition instance in the workload."""

    def __init__(self):
        logger.debug("Transition class init")
        self.env = os.environ.copy()
        self.proxies = {}
        juju_http_proxy = self.env.get("JUJU_CHARM_HTTP_PROXY")
        juju_https_proxy = self.env.get("JUJU_CHARM_HTTPS_PROXY")
        if juju_http_proxy:
            logger.debug("Setting HTTP_PROXY env to %s", juju_http_proxy)
            self.env["HTTP_PROXY"] = juju_http_proxy
            rsync_proxy = urlparse(juju_http_proxy).netloc
            logger.debug("Setting RSYNC_PROXY env to %s", rsync_proxy)
            self.env["RSYNC_PROXY"] = rsync_proxy
            self.proxies["http"] = juju_http_proxy
            self.proxies["rsync"] = rsync_proxy
        if juju_https_proxy:
            logger.debug("Setting HTTPS_PROXY env to %s", juju_https_proxy)
            self.env["HTTPS_PROXY"] = juju_https_proxy
            self.proxies["https"] = juju_https_proxy

    def _install_packages(self):
        """Install the transition Debian packages needed."""
        try:
            apt.update()
            logger.debug("Apt index refreshed.")
        except CalledProcessError as e:
            logger.error("Failed to update package cache: %s", e)
            raise

        for p in PACKAGES:
            try:
                apt.add_package(p)
                logger.debug("Package %s installed", p)
            except PackageNotFoundError:
                logger.error("Failed to find package %s in package cache", p)
                raise
            except PackageError as e:
                logger.error("Failed to install %s: %s", p, e)
                raise

    def install(self):
        """Install the transition tracker environment."""
        # Install the deb packages needed for the service
        self._install_packages()

        # do the following steps only once and not again on upgrades
        if SRVDIR.is_dir():
            return

        # Create the build and log directories
        for dname in (SRVDIR, MIRROR_DISTS):
            try:
                os.makedirs(dname, exist_ok=True)
                logger.debug("Directory %s created", dname)
            except OSError as e:
                logger.warning("Creating directory %s failed: %s", dname, e)
                raise

        # Clone the config repository
        try:
            run(
                [
                    "git",
                    "clone",
                    "-b",
                    "main",
                    REPO_URL,
                    SRVDIR / "config",
                ],
                check=True,
                env=self.env,
                stdout=PIPE,
                stderr=STDOUT,
                text=True,
                timeout=300,
            )
            logger.debug("Transition config vcs cloned.")
        except CalledProcessError as e:
            logger.warning("Git clone of the code failed: %s", e.stdout)
            raise

        # create that needed directory but it should be in the repository?
        finisheddir = SRVDIR / "config" / "monitor" / "finished"
        try:
            os.makedirs(finisheddir, exist_ok=True)
            logger.debug("Directory %s created", finisheddir)
        except OSError as e:
            logger.warning("Creating directory %s failed: %s", finisheddir, e)
            raise

        try:
            shutil.copy("src/script/syncmirror", "/usr/bin")
            shutil.copy("src/nginx/transition.conf", NGINX_SITE_CONFIG_PATH)
            logger.debug("App files copied")
        except (OSError, shutil.Error) as e:
            logger.warning("Error copying files: %s", str(e))
            raise

        # Remove default nginx configuration
        Path("/etc/nginx/sites-enabled/default").unlink(missing_ok=True)
        logger.debug("Nginx default configuration removed")

    def start(self):
        """Restart the transition services."""
        try:
            systemd.service_restart("nginx")
            logger.debug("Nginx service restarted")
            systemd.service_start("ubuntu-transition-tracker.service")
            logger.debug("Ubuntu-transition-tracker service started")
        except CalledProcessError as e:
            logger.error("Failed to start systemd service: %s", e)
            raise

    def configure(self, url: str):
        """Configure the charm."""
        logger.debug("The url in use is %s", url)

    def refresh_report(self):
        """Refresh the tracker."""
        try:
            systemd.service_start("ubuntu-transition-tracker.service")
        except CalledProcessError as e:
            logger.debug("Refreshing of the tracker failed: %s", e.stdout)
            raise

    def setup_systemd_units(self):
        """Set up the systemd service and timer."""
        systemd_unit_location = Path("/etc/systemd/system")
        systemd_unit_location.mkdir(parents=True, exist_ok=True)

        systemd_service = Path("src/systemd/ubuntu-transition-tracker.service")
        service_txt = systemd_service.read_text()

        systemd_timer = Path("src/systemd/ubuntu-transition-tracker.timer")
        timer_txt = systemd_timer.read_text()

        systemd_proxy = ""
        if "http" in self.proxies:
            systemd_proxy += "\nEnvironment=HTTP_PROXY=" + self.proxies["http"]
        if "https" in self.proxies:
            systemd_proxy += "\nEnvironment=HTTPS_PROXY=" + self.proxies["https"]
        if "rsync" in self.proxies:
            systemd_proxy += "\nEnvironment=RSYNC_PROXY=" + self.proxies["rsync"]

        service_txt += systemd_proxy
        (systemd_unit_location / "ubuntu-transition-tracker.service").write_text(service_txt)
        (systemd_unit_location / "ubuntu-transition-tracker.timer").write_text(timer_txt)
        logger.debug("Systemd units created")

        try:
            systemd.service_enable("--now", "ubuntu-transition-tracker.timer")
        except CalledProcessError as e:
            logger.error("Failed to enable the ubuntu-transition-tracker timer: %s", e)
            raise
