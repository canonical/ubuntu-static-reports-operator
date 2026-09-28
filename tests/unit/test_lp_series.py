# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Unit tests for the Launchpad-backed lp_series series helper."""

import email.message
import importlib.util
import io
import json
import socket
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path

import lp_series
import pytest

# Newest first, as the Launchpad collection API returns it: the unreleased
# devel series, maintained releases (bionic ESM and trusty/xenial Legacy
# included via `supported`), and long-EOL questing.
ENTRIES = [
    {"name": "stonking", "supported": False, "datereleased": None},
    {"name": "resolute", "supported": True, "datereleased": "2026-04-23T17:07:00+00:00"},
    {"name": "questing", "supported": False, "datereleased": "2025-10-09T09:26:00+00:00"},
    {"name": "noble", "supported": True, "datereleased": "2024-04-25T15:11:00+00:00"},
    {"name": "jammy", "supported": True, "datereleased": "2022-04-21T17:16:00+00:00"},
    {"name": "focal", "supported": True, "datereleased": "2020-04-23T17:34:00+00:00"},
    {"name": "bionic", "supported": True, "datereleased": "2018-04-26T23:26:00+00:00"},
    {"name": "xenial", "supported": True, "datereleased": "2016-04-21T23:26:00+00:00"},
    {"name": "trusty", "supported": True, "datereleased": "2015-04-17T11:44:00+00:00"},
]
ACTIVE = ["stonking", "resolute", "noble", "jammy", "focal", "bionic", "xenial", "trusty"]


def _page(entries, next_link=None):
    """Serialize one collection page, optionally with a follow-up link."""
    page = {"entries": entries}
    if next_link:
        page["next_collection_link"] = next_link
    return io.BytesIO(json.dumps(page).encode())


def _fake_urlopen(monkeypatch, pages):
    """Serve the given pages in order; return the list of requested URLs."""
    requested = []
    remaining = list(pages)

    def urlopen(url, timeout=None):
        requested.append(url)
        return remaining.pop(0)

    monkeypatch.setattr(lp_series.urllib.request, "urlopen", urlopen)
    return requested


def _load_script(name: str):
    """Import an extensionless script from src/script as a module."""
    module_name = name.replace("-", "_")
    path = Path(__file__).parents[2] / "src" / "script" / name
    spec = importlib.util.spec_from_loader(module_name, SourceFileLoader(module_name, str(path)))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_entries_cache():
    lp_series._entries.cache_clear()
    yield
    lp_series._entries.cache_clear()


def test_active_series_includes_legacy_and_devel(monkeypatch):
    _fake_urlopen(monkeypatch, [_page(ENTRIES)])
    assert lp_series.active_series() == ACTIVE


def test_all_series_newest_first(monkeypatch):
    _fake_urlopen(monkeypatch, [_page(ENTRIES)])
    assert lp_series.all_series() == [entry["name"] for entry in ENTRIES]


def test_devel_series(monkeypatch):
    _fake_urlopen(monkeypatch, [_page(ENTRIES)])
    assert lp_series.devel_series() == "stonking"


def test_entries_are_fetched_once_per_process(monkeypatch):
    requested = _fake_urlopen(monkeypatch, [_page(ENTRIES)])
    lp_series.active_series()
    lp_series.all_series()
    lp_series.devel_series()
    assert len(requested) == 1


def test_pagination_follows_next_collection_link(monkeypatch):
    requested = _fake_urlopen(
        monkeypatch,
        [
            _page(ENTRIES[:4], next_link="https://api.launchpad.net/1.0/ubuntu/series?start=4"),
            _page(ENTRIES[4:]),
        ],
    )
    assert lp_series.all_series() == [entry["name"] for entry in ENTRIES]
    assert requested == [lp_series.API, "https://api.launchpad.net/1.0/ubuntu/series?start=4"]


def test_transient_errors_are_retried_with_backoff(monkeypatch):
    monkeypatch.setattr(lp_series, "INITIAL_DELAY", 3.0)
    sleeps = []
    attempts = []
    monkeypatch.setattr(lp_series.time, "sleep", sleeps.append)

    def urlopen(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise urllib.error.URLError("flaky connection")
        return _page(ENTRIES)

    monkeypatch.setattr(lp_series.urllib.request, "urlopen", urlopen)
    assert lp_series.devel_series() == "stonking"
    assert len(attempts) == 3
    assert sleeps == [3.0, 6.0]


def test_permanent_client_errors_raise_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr(lp_series.time, "sleep", sleeps.append)

    def urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)

    monkeypatch.setattr(lp_series.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="rejected"):
        lp_series.all_series()
    assert sleeps == []


def test_exhausted_attempts_raise(monkeypatch):
    monkeypatch.setattr(lp_series, "ATTEMPTS", 3)
    monkeypatch.setattr(lp_series, "INITIAL_DELAY", 1.0)
    sleeps = []
    monkeypatch.setattr(lp_series.time, "sleep", sleeps.append)

    def urlopen(url, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(lp_series.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="unreachable after 3 attempts"):
        lp_series.all_series()
    assert sleeps == [1.0, 2.0]


def test_update_seeds_series_from_keeps_release_order(monkeypatch):
    seeds = _load_script("update-seeds")
    monkeypatch.setattr(lp_series, "all_series", lambda: ["noble", "jammy", "focal"])
    assert seeds._series_from("jammy", ["focal", "jammy", "noble"]) == ["jammy", "noble"]


def test_update_mismatches_detect_devel(monkeypatch):
    mismatches = _load_script("update-mismatches")
    monkeypatch.setattr(lp_series, "devel_series", lambda: "stonking")
    assert mismatches._detect_devel_series() == "stonking"


def test_update_mismatches_devel_error_is_wrapped(monkeypatch):
    mismatches = _load_script("update-mismatches")

    def devel_series():
        raise RuntimeError("Launchpad API unreachable after 4 attempts: boom")

    monkeypatch.setattr(lp_series, "devel_series", devel_series)
    with pytest.raises(RuntimeError, match="development series"):
        mismatches._detect_devel_series()


def test_archive_mirror_series_filters_are_a_whitelist(monkeypatch):
    mirror = _load_script("update-archive-mirror")
    monkeypatch.setattr(lp_series, "active_series", lambda: ["noble", "jammy", "trusty"])
    assert mirror._series_filters() == [
        "--include",
        "/noble/",
        "--include",
        "/noble-*/",
        "--include",
        "/jammy/",
        "--include",
        "/jammy-*/",
        "--include",
        "/trusty/",
        "--include",
        "/trusty-*/",
        "--include",
        "/devel/",
        "--include",
        "/devel-*/",
        "--exclude",
        "/*/",
    ]


def test_archive_mirror_rsync_uses_series_filters(monkeypatch):
    mirror = _load_script("update-archive-mirror")
    commands = []
    monkeypatch.setattr(lp_series, "active_series", lambda: ["jammy"])
    monkeypatch.setattr(mirror, "_run", lambda cmd, **kwargs: commands.append(cmd))
    mirror._rsync_archive(Path("/mirror"), "rsync://src/dists/")
    cmd = commands[0]
    assert cmd[:5] == ["rsync", "-aq", "--timeout=1200", "--include", "/jammy/"]
    assert "--delete" in cmd
    assert "--delete-excluded" in cmd
    assert "--prune-empty-dirs" in cmd
    # the whitelist root exclude must come before the file-level includes
    assert cmd.index("--exclude") < cmd.index("Packages*")


def _bound_notify_socket(monkeypatch, tmp_path):
    """Bind a unix datagram socket and point NOTIFY_SOCKET at it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path = tmp_path / "notify.sock"
    sock.bind(str(path))
    sock.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    return sock


def test_archive_mirror_sd_notify_sends_datagram(monkeypatch, tmp_path):
    mirror = _load_script("update-archive-mirror")
    sock = _bound_notify_socket(monkeypatch, tmp_path)
    mirror._sd_notify("READY=1")
    assert sock.recv(4096) == b"READY=1"


def test_archive_mirror_sd_notify_is_noop_without_socket(monkeypatch):
    mirror = _load_script("update-archive-mirror")
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    mirror._sd_notify("READY=1")  # must not raise
