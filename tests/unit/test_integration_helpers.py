# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for integration oneshot completion verification."""

from typing import cast

import jubilant
import pytest

from tests.integration import (
    _oneshot_failure,
    oneshot_invocation_id,
    wait_oneshot_finished,
)


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
    output = (
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "InvocationID=0123456789abcdef"
    )
    juju = fake_juju([output])

    wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)

    assert "-p InvocationID" in juju.commands[0]


def test_wait_oneshot_finished_parses_reordered_escaped_properties():
    output = (
        "\x1b[?1h\x1b=\n"
        "Result=success\x1b[m\n"
        "ExecMainStatus=0\x1b[m\n"
        "InvocationID=0123456789abcdef\x1b[m\n"
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
    output = (
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=failed\n"
        "ExecMainStatus=1\n"
        "InvocationID=0123456789abcdef"
    )
    juju = fake_juju([output])

    with pytest.raises(AssertionError, match="completed unsuccessfully"):
        wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1)


def test_wait_oneshot_finished_waits_for_a_real_invocation():
    never_ran = (
        "ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID="
    )
    completed = (
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "InvocationID=0123456789abcdef"
    )
    juju = fake_juju([never_ran, completed])

    wait_oneshot_finished(juju, "app/0", "update-seeds.service", timeout_sec=1, poll_sec=0)

    assert len(juju.commands) == 2


def test_wait_oneshot_finished_with_baseline_requires_new_invocation():
    previous = (
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "InvocationID=old-invocation"
    )
    current = (
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "InvocationID=new-invocation"
    )
    juju = fake_juju([previous, current])

    wait_oneshot_finished(
        juju,
        "app/0",
        "update-seeds.service",
        timeout_sec=1,
        poll_sec=0,
        baseline_invocation_id="old-invocation",
    )

    assert len(juju.commands) == 2


def test_oneshot_invocation_id_strips_escape_sequences():
    juju = fake_juju(["\x1b[?1h\x1b=0123456789abcdef\x1b[m\n"])

    invocation = oneshot_invocation_id(juju, "app/0", "update-seeds.service")

    assert invocation == "0123456789abcdef"
    assert "-p InvocationID --value" in juju.commands[0]


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
