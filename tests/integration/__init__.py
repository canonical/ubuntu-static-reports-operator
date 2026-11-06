# Copyright 2025 Canonical
# See LICENSE file for licensing details.
import functools
import logging
import os
import time
from urllib.parse import urlparse

import jubilant
import pytest
from requests.adapters import DEFAULT_POOLBLOCK, DEFAULT_POOLSIZE, DEFAULT_RETRIES, HTTPAdapter

APPNAME = "ubuntu-static-reports"
HAPROXY = "haproxy"
SSC = "self-signed-certificates"

requires_secret = pytest.mark.skipif(
    not os.path.isfile(os.environ.get("LPUSER_OAUTH_FILE", "")),
    reason="LPUSER_OAUTH_FILE not set or not a file - secret-dependent tests skipped",
)

requires_archive = pytest.mark.skipif(
    os.environ.get("MISMATCH_ARCHIVE_RSYNC_SOURCE") is None,
    reason="MISMATCH_ARCHIVE_RSYNC_SOURCE not set - archive-dependent mismatch test skipped",
)


def retry(retry_num, retry_sleep_sec):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(retry_num):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if i >= retry_num - 1:
                        raise Exception(f"Exceeded {retry_num} retries") from exc
                    logging.error(
                        "func %s failure %d/%d: %s", func.__name__, i + 1, retry_num, exc
                    )
                    time.sleep(retry_sleep_sec)

        return wrapper

    return decorator


class DNSResolverHTTPSAdapter(HTTPAdapter):
    """A simple mounted DNS resolver for HTTP requests."""

    def __init__(
        self,
        hostname,
        ip,
    ):
        """Initialize the dns resolver.

        Args:
            hostname: DNS entry to resolve.
            ip: Target IP address.
        """
        self.hostname = hostname
        self.ip = ip
        super().__init__(
            pool_connections=DEFAULT_POOLSIZE,
            pool_maxsize=DEFAULT_POOLSIZE,
            max_retries=DEFAULT_RETRIES,
            pool_block=DEFAULT_POOLBLOCK,
        )

    # Ignore pylint rule as this is the parent method signature
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # pylint: disable=too-many-arguments, too-many-positional-arguments
        """Wrap HTTPAdapter send to modify the outbound request.

        Args:
            request: Outbound HTTP request.
            stream: argument used by parent method.
            timeout: argument used by parent method.
            verify: argument used by parent method.
            cert: argument used by parent method.
            proxies: argument used by parent method.

        Returns:
            Response: HTTP response after modification.
        """
        connection_pool_kwargs = self.poolmanager.connection_pool_kw

        result = urlparse(request.url)
        if result.hostname == self.hostname:
            ip = self.ip
            if result.scheme == "https" and ip:
                request.url = request.url.replace(
                    "https://" + result.hostname,
                    "https://" + ip,
                )
                connection_pool_kwargs["server_hostname"] = result.hostname
                connection_pool_kwargs["assert_hostname"] = result.hostname
                request.headers["Host"] = result.hostname
            else:
                connection_pool_kwargs.pop("server_hostname", None)
                connection_pool_kwargs.pop("assert_hostname", None)

        return super().send(request, stream, timeout, verify, cert, proxies)


def address(juju: jubilant.Juju, app: str = APPNAME):
    """Report the IP address of the application."""
    return juju.status().apps[app].units[f"{app}/0"].public_address


def configure_lp_secret(juju: jubilant.Juju, secret_uri):
    """Grant the LP secret to the deployed app and point the config at it.

    Must run after juju.deploy(): Juju cannot grant a secret to an
    application that does not exist yet.
    """
    if secret_uri:
        juju.grant_secret(secret_uri, APPNAME)
        juju.config(APPNAME, {"lpuser_secret_id": secret_uri})


def wait_oneshot_finished(
    juju: jubilant.Juju, unit: str, service: str, retries: int = 80, sleep_sec: int = 120
):
    """Wait on service to complete after it has started.

    A failed service is restarted up to two times: its own timer retries
    only after hours, which no test budget can wait out.
    """
    restarts = 0
    state = ""
    for _ in range(retries):
        state = juju.ssh(unit, "systemctl show -p ActiveState -p SubState --value " + service)
        if state == "inactive\ndead\n":
            return
        if "failed" in state and restarts < 2:
            restarts += 1
            logging.warning("%s failed, restarting it (attempt %d)", service, restarts)
            juju.ssh(unit, f"sudo systemctl start --no-block {service}")
        time.sleep(sleep_sec)
    raise AssertionError(
        f"{service} did not finish within {retries * sleep_sec}s, state: {state!r}"
    )
