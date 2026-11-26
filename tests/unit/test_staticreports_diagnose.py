# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for the static-reports-diagnose command-line interface."""

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import Mock

import pytest

import staticreports_health as health


@pytest.fixture
def cli():
    path = Path(__file__).parents[2] / "src" / "script" / "static-reports-diagnose"
    spec = importlib.util.spec_from_loader(
        "static_reports_diagnose", SourceFileLoader("static_reports_diagnose", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _document(status="ok", service="update-seeds"):
    return {
        "schema_version": 1,
        "generated_at": "2026-11-24T12:00:00+00:00",
        "status": status,
        "summary": "test summary",
        "reports": [
            {
                "service": service,
                "status": status,
                "outcome": "published" if status == "ok" else "failed",
                "reason": "test reason",
                "last_success_at": "2026-11-24T11:00:00+00:00",
            }
        ],
        "serving": {"status": "ok", "reason": "local probe passed"},
    }


def test_default_checks_all_services_and_emits_versioned_json(cli, capsys):
    collector = Mock()
    collector.collect.return_value = _document()
    assert cli.main(["--format", "json"], collector=collector) == 0
    collector.collect.assert_called_once_with(None)
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == health.SCHEMA_VERSION
    assert len(document["reports"]) == 1
    assert document["serving"]["status"] == "ok"


def test_service_selection_and_warning_text(cli, capsys):
    collector = Mock()
    collector.collect.return_value = _document(status="warning", service="update-nbs")
    assert cli.main(["--service", "update-nbs"], collector=collector) == 1
    collector.collect.assert_called_once_with("update-nbs")
    output = capsys.readouterr().out
    assert "update-nbs: WARNING (failed) - test reason" in output
    assert "Last successful check:" in output


def test_help_documents_defaults_and_does_not_collect(cli, capsys):
    collector = Mock()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"], collector=collector)
    assert exit_info.value.code == 0
    collector.collect.assert_not_called()
    output = " ".join(capsys.readouterr().out.split())
    assert "default: check all reports and local nginx" in output
    assert "--service" in output
    assert "--format" in output
    assert "Exit codes: 0=OK" in output
    assert cli.DOCUMENTATION in output


def test_collection_failure_is_unknown_json(cli, capsys):
    collector = Mock()
    collector.collect.side_effect = health.HealthCollectionError("systemctl unavailable")
    assert cli.main(["--format", "json"], collector=collector) == 3
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "unknown"
    assert "systemctl unavailable" in document["summary"]
