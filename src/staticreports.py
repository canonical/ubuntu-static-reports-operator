# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Representation of the collection of ubuntu static reports."""

import logging
import os
import shutil
from pathlib import Path
from subprocess import PIPE, STDOUT, CalledProcessError, SubprocessError, run
from urllib.parse import urlparse

import charms.operator_libs_linux.v1.systemd as systemd
from charmlibs import apt, pathops
from charmlibs.apt import PackageError, PackageNotFoundError

logger = logging.getLogger(__name__)

# Packages installed as part of the install/update process.
PACKAGES = [
    "git",
    "nginx-light",
    "procmail",
    "python3-keyring",
]

# Directories and ownership as needed for the services to run
SRV_DIRS = [
    (Path("/srv/staticreports/www"), "ubuntu", "ubuntu"),
    (Path("/srv/staticreports/www/seeds"), "ubuntu", "ubuntu"),
    (Path("/srv/staticreports/www/packagesets"), "ubuntu", "ubuntu"),
    (Path("/srv/staticreports/www/archive-permissions"), "ubuntu", "ubuntu"),
    (Path("/usr/local/src"), None, None),
]

# repo-url, branch and target directory
REPO_URLS = [
    (
        "https://git.launchpad.net/ubuntu-archive-tools",
        "main",
        Path("/usr/local/src/ubuntu-archive-tools"),
    ),
]

NGINX_SITE_CONFIG_PATH = Path("/etc/nginx/conf.d/staticreports.conf")

UBUNTU_STATIC_REPORT_SERVICES = [
    "update-sync-blocklist",
    "update-seeds",
    "packageset-report",
    "package-subscribers",
    "permissions-report",
]

LP_OAUTH_KEY_PATH = "/home/ubuntu/.config/lp-ubuntu-archive-unprivileged-bot.oauth"


class StaticReports:
    """Represent an instance running and presenting all Ubuntu static reports."""

    def __init__(self):
        logger.debug("StaticReports class init")
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
        """Install the required Debian packages needed."""
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
        """Set up the environment required for the static reports."""
        logger.info("Install required deb packages")
        self._install_packages()

        logger.info("Create the required directories")
        for dname, duser, dgroup in SRV_DIRS:
            try:
                os.makedirs(dname, exist_ok=True)
                logger.debug("Directory %s created", dname)
                if duser is not None:
                    logger.debug("Ownership of directory %s set", dname)
                    shutil.chown(dname, duser, dgroup)
            except OSError as e:
                logger.warning("Creating directory %s failed: %s", dname, e)
                raise

        logger.info("Updating repositories")
        for rurl, rbranch, rtarget in REPO_URLS:
            logger.debug(f"Handle repository {rurl}")
            try:
                if not rtarget.is_dir():
                    run(
                        [
                            "git",
                            "clone",
                            "-b",
                            rbranch,
                            rurl,
                            rtarget,
                        ],
                        check=True,
                        env=self.env,
                        stdout=PIPE,
                        stderr=STDOUT,
                        text=True,
                        timeout=300,
                    )
                else:
                    run(
                        [
                            "git",
                            "pull",
                            rbranch,
                        ],
                        cwd=rtarget,
                        check=True,
                        env=self.env,
                        stdout=PIPE,
                        stderr=STDOUT,
                        text=True,
                        timeout=300,
                    )
            except (CalledProcessError, SubprocessError, FileNotFoundError) as e:
                logger.warning("Git handling {rurl} failed: %s", e.stdout)
                raise

        logger.info("Installing App and Config files")
        try:
            shutil.copy("src/script/update-sync-blocklist", "/usr/bin")
            shutil.copy("src/script/update-seeds", "/usr/bin")
            shutil.copy("src/nginx/staticreports.conf", NGINX_SITE_CONFIG_PATH)
            logger.debug("App and Config files copied")
        except (OSError, shutil.Error) as e:
            logger.warning("Error copying files: %s", str(e))
            raise

        logger.info("Removing default Nginx configuration")
        Path("/etc/nginx/sites-enabled/default").unlink(missing_ok=True)

    def start(self):
        """Start all services of the Ubuntu static reports, but do not wait."""
        try:
            systemd.service_restart("nginx")
            logger.debug("Nginx service restarted")
            for service in UBUNTU_STATIC_REPORT_SERVICES:
                systemd.service_start(service + ".service", "--no-block")
                logger.debug(f"{service} service started")
        except CalledProcessError as e:
            logger.error("Failed to start systemd services: %s", e)
            raise

    def configure_url(self, url: str):
        """URL is defined externally - this is a no-op for now."""
        logger.debug("configure_url: The url in use is %s", url)

    def configure_lpoauthkey(self, lp_key_data: str):
        """Create or refresh the credentials file for launchpad access.

        Args:
            user: The git-ubuntu user.
            home_dir: The home directory for the user.
            lp_key_data: The private credential data.

        Returns:
            True if directory and file creation succeeded, False otherwise.
        """
        lp_key_file = pathops.LocalPath(LP_OAUTH_KEY_PATH)
        parent_dir = lp_key_file.parent
        os.makedirs(parent_dir, exist_ok=True)

        key_success = False
        try:
            lp_key_file.write_text(
                lp_key_data,
                mode=0o600,
                user="ubuntu",
                group="ubuntu",
            )
            key_success = True
        except (FileNotFoundError, NotADirectoryError) as e:
            logger.error(
                "Failed to create lp credentials entry due to directory issues: %s", str(e)
            )
        except LookupError as e:
            logger.error(
                "Failed to create lp credentials entry due to issues with root user: %s", str(e)
            )
        except PermissionError as e:
            logger.error(
                "Failed to create lp credentials entry due to permission issues: %s", str(e)
            )
        logger.debug(
            "configure_lpoauthkey: written lp oauth key (length %d) to %s",
            len(lp_key_data),
            lp_key_file,
        )
        return key_success

    def refresh_report(self):
        """Refresh all the reports - wait for completion."""
        try:
            for service in UBUNTU_STATIC_REPORT_SERVICES:
                systemd.service_start(service + ".service")
        except CalledProcessError as e:
            logger.debug("Refreshing of the tracker failed: %s", e.stdout)
            raise

    def setup_systemd_unit(self, service):
        """Set up the requested service and timer with proxy configuration."""
        systemd_unit_location = Path("/etc/systemd/system")
        systemd_unit_location.mkdir(parents=True, exist_ok=True)

        systemd_service = Path(f"src/systemd/{service}.service")
        service_txt = systemd_service.read_text()

        systemd_timer = Path(f"src/systemd/{service}.timer")
        timer_txt = systemd_timer.read_text()

        systemd_proxy = ""
        if "http" in self.proxies:
            systemd_proxy += "\nEnvironment=HTTP_PROXY=" + self.proxies["http"]
        if "https" in self.proxies:
            systemd_proxy += "\nEnvironment=HTTPS_PROXY=" + self.proxies["https"]
        if "rsync" in self.proxies:
            systemd_proxy += "\nEnvironment=RSYNC_PROXY=" + self.proxies["rsync"]

        service_txt += systemd_proxy
        (systemd_unit_location / f"{service}.service").write_text(service_txt)
        (systemd_unit_location / f"{service}.timer").write_text(timer_txt)
        logger.debug(f"Systemd units for {service} created")

        try:
            systemd.service_enable("--now", f"{service}.timer")
        except CalledProcessError as e:
            logger.error(f"Failed to enable {service}.timer: %s", e)
            raise

    def setup_systemd_units(self):
        """Set up all needed systemd services and timers."""
        for service in UBUNTU_STATIC_REPORT_SERVICES:
            self.setup_systemd_unit(service)
