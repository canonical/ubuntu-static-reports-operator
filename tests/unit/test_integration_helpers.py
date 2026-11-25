# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for integration oneshot completion verification."""

from typing import cast

import jubilant
import pytest

from tests.integration import _oneshot_failure, wait_oneshot_finished


class FakeJuju:
    """Return scripted systemd properties and a bounded journal."""

    def __init__(self, properties):
        self.properties = iter(properties)
        self.commands = []

    def ssh(self, unit, command):
        self.commands.append(command)
        if "journalctl" in command:
            return "latest failure details"
        return next(self.properties)


def fake_juju(properties):
    return cast(jubilant.Juju, FakeJuju(properties))


def test_wait_oneshot_finished_accepts_only_successful_completion():
    juju = fake_juju(["ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0"])

    wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)

    assert "sudo timeout 20 systemctl show" in juju.commands[0]


def test_wait_oneshot_finished_parses_reordered_escaped_properties():
    output = (
        "\x1b[?1h\x1b=\n"
        "Result=success\x1b[m\n"
        "ExecMainStatus=0\x1b[m\n"
        "ActiveState=inactive\x1b[m\n"
        "SubState=dead\x1b[m\n"
        "\n\x1b[K\x1b[?1l\x1b>"
    )
    juju = fake_juju([output])

    wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)


def test_wait_oneshot_finished_fails_with_bounded_journal_on_failed_state():
    juju = fake_juju(["ActiveState=failed\nSubState=dead\nResult=failed\nExecMainStatus=1"])

    with pytest.raises(AssertionError, match="latest failure details"):
        wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)

    assert any("journalctl -u update-seeds.service -n 80" in cmd for cmd in juju.commands)


def test_wait_oneshot_finished_rejects_inactive_unsuccessful_result():
    juju = fake_juju(["ActiveState=inactive\nSubState=dead\nResult=failed\nExecMainStatus=1"])

    with pytest.raises(AssertionError, match="completed unsuccessfully"):
        wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)


def test_wait_oneshot_finished_enforces_wall_clock_deadline(monkeypatch):
    running = "ActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0"
    juju = fake_juju([running, running])
    times = iter([0, 31])
    monkeypatch.setattr("tests.integration.time.monotonic", lambda: next(times))

    with pytest.raises(AssertionError, match="did not complete within 30s"):
        wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=30)


def test_oneshot_failure_includes_properties_in_deadline_message():
    running = "ActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0"
    juju = fake_juju([running])
    error = _oneshot_failure(juju, "app/0", "update-seeds.service", "deadline")

    assert "deadline" in str(error)
    assert "latest failure details" in str(error)
