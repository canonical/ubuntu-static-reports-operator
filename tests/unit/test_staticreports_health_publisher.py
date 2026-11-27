# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for the nginx-served periodic health publisher."""

import json

import pytest

import staticreports_health as health
import staticreports_health_publisher as publisher


def _document(
    status="ok",
    generated_at="2026-11-26T12:00:00+00:00",
    running_seconds=None,
    reason="test reason",
):
    report = {
        "service": "update-nbs",
        "status": status,
        "outcome": "published" if status == "ok" else "failed",
        "reason": reason,
    }
    if running_seconds is not None:
        report["running_seconds"] = running_seconds
    return {
        "schema_version": health.SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "summary": "test summary",
        "reports": [report],
        "serving": {"status": "ok", "reason": "probe passed"},
    }


def _history(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def no_chown(monkeypatch):
    monkeypatch.setattr(publisher.shutil, "chown", lambda path, user, group: None)


def test_first_publication_writes_stable_files_and_one_history_interval(tmp_path):
    document = _document()

    assert publisher.publish_health_document(document, health_dir=tmp_path)

    latest = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    text = (tmp_path / "health.txt").read_text(encoding="utf-8")
    history = _history(tmp_path / "history.jsonl")

    assert latest == document
    assert text.startswith("Overall: OK - test summary\n")
    assert text.endswith("\n")
    assert len(history) == 1
    assert history[0]["schema_version"] == health.SCHEMA_VERSION
    assert history[0]["first_seen_at"] == document["generated_at"]
    assert history[0]["last_seen_at"] == document["generated_at"]
    assert history[0]["sample_count"] == 1
    assert history[0]["document"] == document


def test_published_files_are_world_readable(tmp_path):
    document = _document()

    publisher.publish_health_document(document, health_dir=tmp_path)

    assert (tmp_path / "health.json").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "health.txt").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "history.jsonl").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".*.tmp"))


def test_unchanged_state_updates_the_newest_interval(tmp_path):
    first = _document(generated_at="2026-11-26T12:00:00+00:00", running_seconds=10)
    second = _document(generated_at="2026-11-26T12:05:00+00:00", running_seconds=20)

    publisher.publish_health_document(first, health_dir=tmp_path)
    publisher.publish_health_document(second, health_dir=tmp_path)

    history = _history(tmp_path / "history.jsonl")
    assert len(history) == 1
    assert history[0]["first_seen_at"] == first["generated_at"]
    assert history[0]["last_seen_at"] == second["generated_at"]
    assert history[0]["sample_count"] == 2
    assert history[0]["document"] == second


def test_changed_state_appends_a_new_interval(tmp_path):
    healthy = _document(status="ok")
    failed = _document(status="critical", reason="report failed")
    recovered = _document(status="ok", reason="report recovered")

    publisher.publish_health_document(healthy, health_dir=tmp_path)
    publisher.publish_health_document(failed, health_dir=tmp_path)
    publisher.publish_health_document(recovered, health_dir=tmp_path)

    history = _history(tmp_path / "history.jsonl")
    assert [entry["document"]["status"] for entry in history] == [
        "ok",
        "critical",
        "ok",
    ]
    assert [entry["sample_count"] for entry in history] == [1, 1, 1]
    assert history[1]["document"]["reports"][0]["reason"] == "report failed"
    assert history[2]["first_seen_at"] == recovered["generated_at"]


def test_history_retains_the_full_reports_array(tmp_path):
    document = _document()
    document["reports"].append(
        {
            "service": "update-mismatches",
            "status": "warning",
            "outcome": "missing_input",
            "reason": "missing input",
        }
    )

    publisher.publish_health_document(document, health_dir=tmp_path)

    history = _history(tmp_path / "history.jsonl")
    assert [report["service"] for report in history[0]["document"]["reports"]] == [
        "update-nbs",
        "update-mismatches",
    ]


def test_history_cap_keeps_the_newest_entry(tmp_path):
    first = _document(status="ok", reason="first")
    second = _document(status="warning", reason="second")
    third = _document(status="critical", reason="third")

    publisher.publish_health_document(first, health_dir=tmp_path, max_history_bytes=1)
    publisher.publish_health_document(second, health_dir=tmp_path, max_history_bytes=1)
    publisher.publish_health_document(third, health_dir=tmp_path, max_history_bytes=1)

    history = _history(tmp_path / "history.jsonl")
    assert len(history) == 1
    assert history[0]["document"]["status"] == "critical"
    assert history[0]["document"]["reports"][0]["reason"] == "third"


def test_malformed_history_lines_are_ignored(tmp_path):
    history_path = tmp_path / "history.jsonl"
    invalid_entry = {
        "schema_version": 1,
        "first_seen_at": "2026-11-26T12:00:00+00:00",
        "last_seen_at": "2026-11-26T12:00:00+00:00",
        "sample_count": "invalid",
        "document": {"status": "ok"},
    }
    history_path.write_text(
        "not-json\n" + json.dumps(invalid_entry) + "\n",
        encoding="utf-8",
    )
    document = _document()

    publisher.publish_health_document(document, health_dir=tmp_path)

    history = _history(history_path)
    assert len(history) == 1
    assert history[0]["document"] == document


def test_publication_failure_is_reported_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "_atomic_write", lambda path, content: False)

    assert publisher.publish_health_document(_document(), health_dir=tmp_path) is False


def test_normalized_document_ignores_generated_at_and_running_seconds():
    first = _document(generated_at="2026-11-26T12:00:00+00:00", running_seconds=1)
    second = _document(generated_at="2026-11-26T12:05:00+00:00", running_seconds=2)

    assert publisher._same_health_state(first, second)

    second["reports"][0]["status"] = "critical"
    assert not publisher._same_health_state(first, second)
