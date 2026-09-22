"""Ubuntu series information from the Launchpad API.

Launchpad's ``supported`` flag covers the full maintenance lifecycle of a
series (standard support, ESM, and Legacy), and the one series without a
release date is the current development series. Unlike the local
distro-info data, the API is always current, including freshly-opened
development series. Furthermore distro-info does not yet support probing
legacy via api or cli.
"""

import json
import os
import time
import typing
import urllib.error
import urllib.request
from functools import lru_cache

API = "https://api.launchpad.net/1.0/ubuntu/series?ws.size=100"
ATTEMPTS = int(os.environ.get("LP_ATTEMPTS", "4"))
INITIAL_DELAY = float(os.environ.get("LP_INITIAL_DELAY", "3"))
# Worst case (4 attempts x 15s timeout + 3+6+12s backoff = 81s) stays under
# the 90s systemd default timeout the calling services run with.
REQUEST_TIMEOUT = 15


def _fetch_json(url: str) -> dict[str, typing.Any]:
    """GET one Launchpad API collection page, retrying transient errors with backoff."""
    delay = INITIAL_DELAY
    error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except (OSError, json.JSONDecodeError) as caught:
            error = caught
            if (
                isinstance(caught, urllib.error.HTTPError)
                and 400 <= caught.code < 500
                and caught.code != 429
            ):
                raise RuntimeError(f"Launchpad API rejected {url}: {caught}") from caught
            if attempt < ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Launchpad API unreachable after {ATTEMPTS} attempts: {error}") from error


@lru_cache(maxsize=1)
def _entries() -> tuple[dict[str, typing.Any], ...]:
    """All Ubuntu distro series entries from Launchpad, newest first."""
    entries: list[dict[str, typing.Any]] = []
    url: str | None = API
    while url:
        page = _fetch_json(url)
        entries += page["entries"]
        url = page.get("next_collection_link")
    return tuple(entries)


def all_series() -> list[str]:
    """All known series names, newest first."""
    return [entry["name"] for entry in _entries()]


def active_series() -> list[str]:
    """Maintained series names, newest first: standard support, ESM, Legacy, plus devel."""
    return [
        entry["name"]
        for entry in _entries()
        if entry["supported"] or entry["datereleased"] is None
    ]


def devel_series() -> str:
    """Return the current development series (the one series without a release date)."""
    for entry in _entries():
        if entry["datereleased"] is None:
            return entry["name"]
    raise RuntimeError("Launchpad reports no unreleased (development) series.")
