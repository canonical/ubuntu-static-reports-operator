# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for periodic Juju workload-health status reconciliation."""

from typing import cast
from unittest.mock import patch

import ops
import ops.testing
import pytest
from charms.operator_libs_linux.v1.systemd import SystemdError
from ops.testing import ActiveStatus, BlockedStatus, Context, State

import staticreports_health as health
from charm import UbuntuStaticReportsCharm


@pytest.fixture
def ctx():
    return Context(UbuntuStaticReportsCharm)


def _document(status):
    return {
        "schema_version": 1,
        "generated_at": "2026-11-24T12:00:00+00:00",
        "status": status,
        "summary": "test summary",
        "reports": [
            {
                "service": "update-nbs",
                "status": status,
                "outcome": "published" if status == "ok" else "failed",
                "reason": "test reason",
            }
        ],
        "serving": {"status": "ok", "reason": "probe passed"},
    }


@patch("charm.health.HealthCollector")
def test_update_status_publishes_degradation_after_two_matching_samples(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("warning")
    state = State(leader=True, unit_status=ActiveStatus())

    state = ctx.run(ctx.on.update_status(), state)
    assert state.unit_status == ActiveStatus()
    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == ActiveStatus(
        "Degraded: 1 check needs attention; run static-reports-diagnose."
    )


@patch("charm.health.HealthCollector")
def test_update_status_clears_degradation_on_first_healthy_sample(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("warning")
    state = State(leader=True)
    state = ctx.run(ctx.on.update_status(), state)
    state = ctx.run(ctx.on.update_status(), state)
    collector_class.return_value.collect.return_value = _document("ok")

    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == ActiveStatus()


@patch("charm.health.HealthCollector")
def test_update_status_blocks_only_after_persistent_critical_health(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("critical")
    state = State(leader=True, unit_status=ActiveStatus())

    state = ctx.run(ctx.on.update_status(), state)
    assert state.unit_status == ActiveStatus()
    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == BlockedStatus(
        "Local workload critical: 1 check needs attention; run static-reports-diagnose."
    )


@patch("charm.health.HealthCollector")
def test_update_status_recovers_from_health_generated_block(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("critical")
    state = State(leader=True)
    state = ctx.run(ctx.on.update_status(), state)
    state = ctx.run(ctx.on.update_status(), state)
    collector_class.return_value.collect.return_value = _document("ok")

    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == ActiveStatus()


@patch("charm.health.HealthCollector")
def test_update_status_turns_collection_failure_into_unknown_not_hook_error(collector_class, ctx):
    collector_class.return_value.collect.side_effect = health.HealthCollectionError(
        "systemctl unavailable"
    )
    state = State(leader=True)

    state = ctx.run(ctx.on.update_status(), state)
    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == ActiveStatus("Report health unknown; run static-reports-diagnose.")


@patch("charm.health.HealthCollector")
def test_update_status_works_with_juju4(collector_class):
    collector_class.return_value.collect.return_value = _document("ok")
    ctx = Context(UbuntuStaticReportsCharm, juju_version="4.0.0")
    state = State(leader=True)

    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == ActiveStatus()


@patch("charm.health.HealthCollector")
def test_collect_status_preserves_existing_lifecycle_block(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("ok")
    state = State(leader=True, unit_status=BlockedStatus("Invalid configuration"))

    state = ctx.run(ctx.on.update_status(), state)

    assert state.unit_status == BlockedStatus("Invalid configuration")


@patch("charm.health.HealthCollector")
def test_credential_waiting_message_survives_published_health(collector_class, ctx):
    collector_class.return_value.collect.return_value = _document("ok")
    state = ctx.run(ctx.on.update_status(), State(leader=True))

    with (
        patch("charm.StaticReports.configure_archive_mirror"),
        patch("charm.StaticReports.configure_mismatches"),
        patch("charm.StaticReports.configure_seeded_in_ubuntu_indexer"),
        patch("charm.StaticReports.clear_lpoauthkey", return_value=True),
    ):
        state = ctx.run(ctx.on.config_changed(), state)

    assert state.unit_status == ActiveStatus(
        "Authenticated reports waiting for Launchpad credentials."
    )


@patch("charm.StaticReports.run_report")
@patch("charm.health.HealthCollector")
def test_action_failure_does_not_become_persistent_health_status(
    collector_class, run_report_mock, ctx
):
    collector_class.return_value.collect.return_value = _document("ok")
    state = ctx.run(ctx.on.update_status(), State(leader=True))
    run_report_mock.side_effect = SystemdError("start failed")

    with pytest.raises(ops.testing.ActionFailed) as failure:
        ctx.run(
            ctx.on.action("run-report", params={"service": "update-nbs"}),
            state,
        )

    assert failure.value.state is not None
    failed_state = cast(State, failure.value.state)
    assert failed_state.unit_status == ActiveStatus()
    state = ctx.run(ctx.on.update_status(), failed_state)
    assert state.unit_status == ActiveStatus()
