# Copyright 2025 Canonical
# See LICENSE file for licensing details.
import functools
import logging
import sys
import time
from urllib.parse import urlparse

import jubilant
from requests.adapters import DEFAULT_POOLBLOCK, DEFAULT_POOLSIZE, DEFAULT_RETRIES, HTTPAdapter

APPNAME = "ubuntu-static-reports"
HAPROXY = "haproxy"
SSC = "self-signed-certificates"


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


@retry(retry_num=20, retry_sleep_sec=30)
def wait_oneshot_finished(juju: jubilant.Juju, unit: str, service: str):
    """Wait on service to complete after it has started."""
    state = juju.ssh(unit, "systemctl show -p ActiveState -p SubState --value " + service)
    ready = state == "inactive\ndead\n"
    logging.debug(f"{sys._getframe().f_code.co_name} - state: {state}")
    logging.debug(f"{sys._getframe().f_code.co_name} - ready: {ready}")
    assert ready, f"State is {state}, expected finish is 'inactive\ndead\n'"
