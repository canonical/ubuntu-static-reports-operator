# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for the shared static report health model."""

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import staticreports_health as health

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _seconds(value: str) -> int:
    amount = int(re.match(r"\d+", value).group())
    if value.endswith("min"):
        return amount * 60
    if value.endswith("h"):
        return amount * 3600
    if value.isdigit() or value.endswith("s") or value.endswith("sec"):
        return amount
    raise AssertionError(f"Unsupported test duration: {value}")


def _unit(value: str) -> str:
    return (Path(__file__).parents[2] / "src" / "systemd" / value).read_text()


def _service(
    *,
    active: str = "inactive",
    result: str = "success",
    status: str = "0",
    condition: str = "yes",
    invocation: str = "invocation-1",
    started: datetime | None = None,
    finished: datetime | None = None,
) -> dict[str, str]:
    started = started or NOW - timedelta(minutes=2)
    finished = finished or NOW - timedelta(minutes=1)
    return {
        "ActiveState": active,
        "SubState": "running" if active in {"active", "activating"} else "dead",
        "Result": result,
        "ConditionResult": condition,
        "ExecMainStatus": status,
        "InvocationID": invocation,
        "ExecMainStartTimestamp": started.isoformat(),
        "ExecMainExitTimestamp": finished.isoformat(),
    }


def _timer(*, active: str = "active", enabled: str = "enabled", triggered=None) -> dict[str, str]:
    return {
        "ActiveState": active,
        "UnitFileState": enabled,
        "LastTriggerUSec": (triggered or NOW - timedelta(minutes=1)).isoformat(),
        "ActiveEnterTimestamp": (NOW - timedelta(days=1)).isoformat(),
    }


def _record(outcome: str = "published", **overrides) -> dict:
    record = {
        "schema_version": 1,
        "service": "update-archive-mirror",
        "invocation_id": "invocation-1",
        "started_at": (NOW - timedelta(minutes=2)).isoformat(),
        "finished_at": (NOW - timedelta(minutes=1)).isoformat(),
        "outcome": outcome,
        "reason": "test outcome",
        "input_snapshot": "",
        "output_snapshot": "snapshot.current",
        "details": {},
    }
    record.update(overrides)
    return record


def test_service_catalog_covers_every_report_and_matches_unit_timing():
    assert set(health.REPORT_SPECS) == set(health.SERVICE_NAMES)
    for name, spec in health.REPORT_SPECS.items():
        service = _unit(f"{name}.service")
        if spec.runtime_limit == 28800:
            assert "TimeoutStartSec=8h" in service
        else:
            assert "TimeoutStartSec=2h" in service
        if spec.interval:
            timer = _unit(f"{name}.timer")
            interval = re.search(r"OnUnitActiveSec=(\S+)", timer).group(1)
            jitter = re.search(r"RandomizedDelaySec=(\S+)", timer).group(1)
            assert spec.interval == _seconds(interval)
            assert spec.jitter == _seconds(jitter)


def test_report_specs_are_loaded_from_unit_files(tmp_path):
    (tmp_path / "update-bugpatterns.service").write_text(
        "[Unit]\nDescription=Ubuntu Static Reports - Bug Patterns Web view\n"
        "[Service]\nTimeoutStartSec=3h\n"
    )
    (tmp_path / "update-bugpatterns.timer").write_text(
        "[Timer]\nOnUnitActiveSec=20min\nRandomizedDelaySec=45\n"
    )
    (tmp_path / "update-archive-mirror.service").write_text(
        "[Unit]\nDescription=Ubuntu Static Reports - archive mirror sync\n"
        "[Service]\nTimeoutStartSec=2h\nConditionFileNotEmpty=/tmp/oauth\n"
        "OnSuccess=update-bugpatterns.service\n"
    )
    (tmp_path / "dbus-org.freedesktop.resolve1.service").write_text(
        "[Unit]\nDescription=Network Name Resolution\n"
    )
    (tmp_path / "jujud-machine-0.service").write_text("[Unit]\nDescription=juju unit agent\n")

    specs = health._load_report_specs(tmp_path)

    assert set(specs) == {"update-bugpatterns", "update-archive-mirror"}
    assert specs["update-bugpatterns"].interval == 1200
    assert specs["update-bugpatterns"].jitter == 45
    assert specs["update-bugpatterns"].runtime_limit == 10800
    assert specs["update-bugpatterns"].parent == "update-archive-mirror"
    assert specs["update-archive-mirror"].credential is True
    assert specs["update-archive-mirror"].structured is True


def test_record_outcome_writes_versioned_json_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")
    assert health.record_outcome(
        "update-archive-mirror",
        "unchanged",
        "No archive changes.",
        output_snapshot="snapshot.one",
        health_dir=tmp_path,
    )
    document = json.loads((tmp_path / "update-archive-mirror.json").read_text())
    assert document["schema_version"] == 1
    assert document["invocation_id"] == "systemd-invocation"
    assert document["outcome"] == "unchanged"
    assert document["output_snapshot"] == "snapshot.one"
    assert not list(tmp_path.glob("*.tmp"))


def test_record_outcome_rejects_unknown_service(tmp_path):
    assert not health.record_outcome("unknown", "published", health_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_running_report_uses_its_actual_runtime_limit():
    evidence = health.Evidence(
        service=_service(active="activating", started=NOW - timedelta(hours=3)),
        outputs_present=True,
    )
    report = health.evaluate_report("sru-report", evidence, NOW)
    assert report["status"] == "ok"
    assert report["outcome"] == "running"
    report = health.evaluate_report("update-seeds", evidence, NOW)
    assert report["status"] == "critical"
    assert "runtime limit" in report["reason"]


def test_failed_report_is_warning_with_old_output_and_critical_without_output():
    failed = _service(result="failed", status="1", active="failed")
    warning = health.evaluate_report(
        "update-seeds", health.Evidence(service=failed, outputs_present=True), NOW
    )
    critical = health.evaluate_report(
        "update-seeds", health.Evidence(service=failed, outputs_present=False), NOW
    )
    assert warning["status"] == "warning"
    assert critical["status"] == "critical"


def test_credential_condition_is_waiting_not_a_successful_run():
    evidence = health.Evidence(
        service=_service(condition="no"),
        timer=_timer(),
        outputs_present=True,
    )
    report = health.evaluate_report("permissions-report", evidence, NOW)
    assert report["status"] == "waiting"
    assert report["outcome"] == "condition_skipped"


def test_timer_staleness_uses_two_complete_cycles_with_slack():
    too_old = NOW - timedelta(seconds=2 * (300 + 60) + 61)
    evidence = health.Evidence(
        service=_service(finished=too_old),
        timer=_timer(triggered=too_old),
        outputs_present=True,
    )
    report = health.evaluate_report("update-bugpatterns", evidence, NOW)
    assert report["status"] == "warning"
    assert report["outcome"] == "stale"


def test_simple_success_requires_output_but_not_an_outcome_record():
    evidence = health.Evidence(service=_service(), timer=_timer(), outputs_present=True)
    report = health.evaluate_report("package-subscribers", evidence, NOW)
    assert report["status"] == "ok"
    assert report["last_publication_at"]


def test_structured_report_without_record_is_unknown_during_upgrade():
    evidence = health.Evidence(service=_service(), timer=_timer(), outputs_present=True)
    report = health.evaluate_report("update-archive-mirror", evidence, NOW)
    assert report["status"] == "unknown"
    assert "Awaiting" in report["reason"]


def test_structured_unchanged_outcome_is_healthy_with_current_snapshot():
    evidence = health.Evidence(
        service=_service(),
        timer=_timer(),
        record=_record("unchanged"),
        outputs_present=True,
        current_snapshot="snapshot.current",
    )
    report = health.evaluate_report("update-archive-mirror", evidence, NOW)
    assert report["status"] == "ok"
    assert report["outcome"] == "unchanged"
    assert report["lineage_current"] is True


def test_newer_systemd_failure_overrides_an_old_success_record():
    evidence = health.Evidence(
        service=_service(result="failed", status="1", active="failed", invocation="new"),
        record=_record(),
        outputs_present=True,
    )
    report = health.evaluate_report("update-archive-mirror", evidence, NOW)
    assert report["status"] == "warning"
    assert report["outcome"] == "failed"


def test_locked_outcome_does_not_claim_publication():
    evidence = health.Evidence(
        service=_service(),
        timer=_timer(),
        record=_record("locked"),
        outputs_present=True,
    )
    report = health.evaluate_report("update-archive-mirror", evidence, NOW)
    assert report["status"] == "warning"
    assert report["outcome"] == "locked"
    assert "last_publication_at" not in report


def test_germinate_checks_archive_and_output_lineage():
    record = _record(
        service="update-germinate",
        input_snapshot="snapshot.archive",
        output_snapshot="snapshot.germinate",
    )
    evidence = health.Evidence(
        service=_service(),
        record=record,
        outputs_present=True,
        current_snapshot="snapshot.germinate",
        parent_snapshot="snapshot.archive",
        stamp_input_snapshot="snapshot.archive",
    )
    assert health.evaluate_report("update-germinate", evidence, NOW)["status"] == "ok"
    evidence.parent_snapshot = "snapshot.newer"
    report = health.evaluate_report("update-germinate", evidence, NOW)
    assert report["status"] == "warning"
    assert report["lineage_current"] is False


def test_partial_index_publication_is_warning():
    record = _record(
        service="seeded-in-ubuntu-indexer",
        input_snapshot="snapshot.germinate",
        output_snapshot="",
        details={"complete": False, "written": [], "skipped": [{"cell": "resolute/amd64"}]},
    )
    evidence = health.Evidence(
        service=_service(),
        record=record,
        outputs_present=True,
        parent_snapshot="snapshot.germinate",
    )
    report = health.evaluate_report("seeded-in-ubuntu-indexer", evidence, NOW)
    assert report["status"] == "warning"
    assert "skipped" in report["reason"]


def test_parent_failure_marks_otherwise_current_downstream_dependency_blocked():
    record = _record(
        service="update-nbs",
        input_snapshot="snapshot.archive",
        output_snapshot="",
    )
    evidence = health.Evidence(
        service=_service(),
        record=record,
        outputs_present=True,
        parent_snapshot="snapshot.archive",
    )
    report = health.evaluate_report("update-nbs", evidence, NOW, parent_status="failed")
    assert report["status"] == "ok"
    report = health.evaluate_report("update-nbs", evidence, NOW, parent_status="critical")
    assert report["status"] == "warning"
    assert report["outcome"] == "dependency_blocked"


def test_collector_defaults_to_all_reports_and_local_serving(monkeypatch):
    collector = health.HealthCollector(now=lambda: NOW, http_probe=lambda: (True, "ok"))
    units = {}
    for name in health.SERVICE_NAMES:
        units[f"{name}.service"] = _service()
        if health.REPORT_SPECS[name].interval:
            units[f"{name}.timer"] = _timer()
    units["nginx.service"] = _service(active="active")
    monkeypatch.setattr(collector, "_systemd", lambda names: units)
    monkeypatch.setattr(collector, "_outputs_present", lambda name: True)

    def snapshots(name):
        if name == "update-archive-mirror":
            return "snapshot.archive", "", ""
        if name == "update-germinate":
            return "snapshot.germinate", "snapshot.archive", "snapshot.archive"
        if name in {"update-mismatches", "seeded-in-ubuntu-indexer"}:
            return "", "snapshot.germinate", ""
        if name == "update-nbs":
            return "", "snapshot.archive", ""
        return "", "", ""

    def outcome(name):
        inputs = {
            "update-germinate": "snapshot.archive",
            "update-mismatches": "snapshot.germinate",
            "update-nbs": "snapshot.archive",
            "seeded-in-ubuntu-indexer": "snapshot.germinate",
        }
        outputs = {
            "update-archive-mirror": "snapshot.archive",
            "update-germinate": "snapshot.germinate",
        }
        return _record(
            service=name,
            input_snapshot=inputs.get(name, ""),
            output_snapshot=outputs.get(name, ""),
            details={},
        ), ""

    monkeypatch.setattr(collector, "_snapshots", snapshots)
    monkeypatch.setattr(collector, "_record", outcome)
    document = collector.collect()
    assert len(document["reports"]) == len(health.SERVICE_NAMES)
    assert document["serving"]["status"] == "ok"
    assert document["status"] == "ok"
    json.dumps(document)


def test_systemd_collection_is_bounded_and_parses_multiple_units(tmp_path):
    output = "Id=one.service\nActiveState=inactive\n\nId=one.timer\nActiveState=active\n"
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    collector = health.HealthCollector(runner=runner)
    units = collector._systemd(["one"])
    assert units["one.service"]["ActiveState"] == "inactive"
    assert units["one.timer"]["ActiveState"] == "active"
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["env"]["TZ"] == "UTC"
    assert "--timestamp=utc" in calls[0][0]


def test_missing_service_or_completion_evidence_is_not_healthy():
    assert (
        health.evaluate_report("update-seeds", health.Evidence(outputs_present=True), NOW)[
            "status"
        ]
        == "unknown"
    )
    incomplete = _service()
    del incomplete["ExecMainExitTimestamp"]
    incomplete["ActiveExitTimestamp"] = ""
    incomplete["InactiveEnterTimestamp"] = ""
    report = health.evaluate_report(
        "update-seeds",
        health.Evidence(service=incomplete, timer=_timer(), outputs_present=True),
        NOW,
    )
    assert report["status"] == "unknown"
    assert "No successful completion" in report["reason"]


def test_not_loaded_unit_and_disabled_timer_without_output_are_critical():
    service = _service()
    service["LoadState"] = "not-found"
    assert (
        health.evaluate_report(
            "update-seeds", health.Evidence(service=service, outputs_present=True), NOW
        )["status"]
        == "critical"
    )
    report = health.evaluate_report(
        "update-seeds",
        health.Evidence(
            service=_service(),
            timer=_timer(active="inactive", enabled="disabled"),
            outputs_present=False,
        ),
        NOW,
    )
    assert report["status"] == "critical"
    assert report["outcome"] == "disabled"


def test_systemctl_time_parser_accepts_only_explicit_zone_or_epoch():
    expected = datetime(2026, 11, 24, 11, 37, 33, tzinfo=timezone.utc)
    assert health.parse_time("Tue 2026-11-24 11:37:33 UTC") == expected
    assert health.parse_time("Tue 2026-11-24 07:37:33 EST") is None
    assert health.parse_time("@1795519316") is not None


def test_outcome_recording_contains_serialization_failures(tmp_path):
    assert not health.record_outcome(
        "update-nbs",
        "published",
        details={"not-json": object()},
        health_dir=tmp_path,
    )
    assert not (tmp_path / "update-nbs.json").exists()


def test_malformed_outcome_document_becomes_evidence_error(tmp_path):
    (tmp_path / "update-nbs.json").write_text("null")
    collector = health.HealthCollector(health_dir=tmp_path)
    record, error = collector._record("update-nbs")
    assert record is None
    assert "unsupported schema" in error


def test_seeded_output_must_be_a_nonempty_file(tmp_path):
    output = tmp_path / "seeded-in-ubuntu"
    output.mkdir()
    seeded = output / "seeded-resolute-amd64.json.gz"
    seeded.touch()
    collector = health.HealthCollector(web_root=tmp_path)
    assert not collector._outputs_present("seeded-in-ubuntu-indexer")
    seeded.write_text("not-empty")
    assert collector._outputs_present("seeded-in-ubuntu-indexer")


def test_nginx_probe_disables_environment_proxies_and_redirects(monkeypatch):
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    opener = Mock()
    opener.open.return_value = response
    build_opener = Mock(return_value=opener)
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    collector = health.HealthCollector()
    assert collector._nginx_probe() == (True, "nginx serves the local robots.txt probe.")
    handlers = build_opener.call_args.args
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], health._NoRedirect)
    assert opener.open.call_args.kwargs["timeout"] == 2


def test_single_service_collection_retains_parent_failure(monkeypatch):
    collector = health.HealthCollector(now=lambda: NOW)
    units = {f"{name}.service": _service() for name in health.SERVICE_NAMES}
    units.update(
        {f"{name}.timer": _timer() for name, spec in health.REPORT_SPECS.items() if spec.interval}
    )
    units["update-archive-mirror.service"] = _service(active="failed", result="failed", status="1")
    monkeypatch.setattr(collector, "_systemd", lambda names: units)
    monkeypatch.setattr(collector, "_outputs_present", lambda name: True)

    def snapshots(name):
        if name == "update-archive-mirror":
            return "snapshot.archive", "", ""
        return "", "snapshot.archive", ""

    def outcome(name):
        return _record(
            service=name,
            input_snapshot="snapshot.archive" if name == "update-nbs" else "",
            output_snapshot="snapshot.archive" if name == "update-archive-mirror" else "",
        ), ""

    monkeypatch.setattr(collector, "_snapshots", snapshots)
    monkeypatch.setattr(collector, "_record", outcome)
    document = collector.collect("update-nbs")
    assert len(document["reports"]) == 1
    assert document["reports"][0]["service"] == "update-nbs"
    assert document["reports"][0]["outcome"] == "dependency_blocked"
    assert document["serving"] is None
