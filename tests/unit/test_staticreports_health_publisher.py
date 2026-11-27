# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Tests for the atomic nginx-served periodic health publisher."""

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


def _current(data_dir):
    return (data_dir / "current").resolve()


def _history(data_dir):
    return [
        json.loads(line)
        for line in (_current(data_dir) / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]


@pytest.fixture(autouse=True)
def no_chown(monkeypatch):
    monkeypatch.setattr(publisher.shutil, "chown", lambda path, user, group: None)


def test_default_health_data_dir_is_inside_the_web_root():
    assert publisher.DEFAULT_HEALTH_DATA_DIR == health.DEFAULT_WEB_ROOT / "health"
    assert str(publisher.DEFAULT_HEALTH_DATA_DIR) == "/srv/staticreports/www/health"


def test_first_publication_writes_one_complete_snapshot(tmp_path):
    document = _document()

    assert publisher.publish_health_document(document, data_dir=tmp_path)

    current = tmp_path / "current"
    snapshot = _current(tmp_path)
    assert current.is_symlink()
    assert snapshot.parent == tmp_path / "snapshots"
    assert snapshot.is_dir()
    assert not (tmp_path / "health.json").exists()

    latest = json.loads((snapshot / "health.json").read_text(encoding="utf-8"))
    text = (snapshot / "health.txt").read_text(encoding="utf-8")
    history = _history(tmp_path)

    assert latest == document
    assert text.startswith("Overall: OK - test summary\n")
    assert text.endswith("\n")
    assert len(history) == 1
    assert history[0]["schema_version"] == health.SCHEMA_VERSION
    assert history[0]["first_seen_at"] == document["generated_at"]
    assert history[0]["last_seen_at"] == document["generated_at"]
    assert history[0]["sample_count"] == 1
    assert history[0]["document"] == document
    assert {path.name for path in tmp_path.iterdir()} == {"snapshots", "current"}


def test_published_snapshot_is_world_readable_and_staging_is_removed(tmp_path):
    document = _document()

    publisher.publish_health_document(document, data_dir=tmp_path)

    snapshot = _current(tmp_path)
    assert snapshot.stat().st_mode & 0o777 == 0o755
    assert (snapshot / "health.json").stat().st_mode & 0o777 == 0o644
    assert (snapshot / "health.txt").stat().st_mode & 0o777 == 0o644
    assert (snapshot / "history.jsonl").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".current.*"))
    assert not list((tmp_path / "snapshots").glob(".current.*"))


def test_unchanged_state_updates_the_newest_interval(tmp_path):
    first = _document(generated_at="2026-11-26T12:00:00+00:00", running_seconds=10)
    second = _document(generated_at="2026-11-26T12:05:00+00:00", running_seconds=20)

    publisher.publish_health_document(first, data_dir=tmp_path)
    publisher.publish_health_document(second, data_dir=tmp_path)

    history = _history(tmp_path)
    assert len(history) == 1
    assert history[0]["first_seen_at"] == first["generated_at"]
    assert history[0]["last_seen_at"] == second["generated_at"]
    assert history[0]["sample_count"] == 2
    assert history[0]["document"] == second


def test_changed_state_appends_a_new_interval(tmp_path):
    healthy = _document(status="ok")
    failed = _document(status="critical", reason="report failed")
    recovered = _document(status="ok", reason="report recovered")

    publisher.publish_health_document(healthy, data_dir=tmp_path)
    publisher.publish_health_document(failed, data_dir=tmp_path)
    publisher.publish_health_document(recovered, data_dir=tmp_path)

    history = _history(tmp_path)
    assert [entry["document"]["status"] for entry in history] == ["ok", "critical", "ok"]
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

    publisher.publish_health_document(document, data_dir=tmp_path)

    history = _history(tmp_path)
    assert [report["service"] for report in history[0]["document"]["reports"]] == [
        "update-nbs",
        "update-mismatches",
    ]


def test_history_cap_keeps_the_newest_entry(tmp_path):
    first = _document(status="ok", reason="first")
    second = _document(status="warning", reason="second")
    third = _document(status="critical", reason="third")

    publisher.publish_health_document(first, data_dir=tmp_path, max_history_bytes=1)
    publisher.publish_health_document(second, data_dir=tmp_path, max_history_bytes=1)
    publisher.publish_health_document(third, data_dir=tmp_path, max_history_bytes=1)

    history = _history(tmp_path)
    assert len(history) == 1
    assert history[0]["document"]["status"] == "critical"
    assert history[0]["document"]["reports"][0]["reason"] == "third"


def test_malformed_history_lines_are_ignored(tmp_path):
    publisher.publish_health_document(_document(), data_dir=tmp_path)
    (_current(tmp_path) / "history.jsonl").write_text("not-json\n", encoding="utf-8")
    document = _document(generated_at="2026-11-26T12:05:00+00:00")

    publisher.publish_health_document(document, data_dir=tmp_path)

    history = _history(tmp_path)
    assert len(history) == 1
    assert history[0]["document"] == document


def test_failed_switch_preserves_the_previous_complete_snapshot(tmp_path, monkeypatch):
    first = _document(status="ok", reason="first")
    publisher.publish_health_document(first, data_dir=tmp_path)
    previous_snapshot = _current(tmp_path)
    previous_json = (previous_snapshot / "health.json").read_text(encoding="utf-8")
    previous_history = (previous_snapshot / "history.jsonl").read_text(encoding="utf-8")

    def fail_switch(data_dir, snapshot):
        raise OSError("current link swap failed")

    monkeypatch.setattr(publisher, "_switch_current", fail_switch)
    second = _document(status="critical", reason="second")

    assert publisher.publish_health_document(second, data_dir=tmp_path) is False

    assert _current(tmp_path) == previous_snapshot
    assert (previous_snapshot / "health.json").read_text(encoding="utf-8") == previous_json
    assert (previous_snapshot / "history.jsonl").read_text(encoding="utf-8") == previous_history
    assert [path.name for path in (tmp_path / "snapshots").iterdir()] == [previous_snapshot.name]


def test_publication_failure_removes_an_incomplete_snapshot(tmp_path, monkeypatch):
    publisher.publish_health_document(_document(), data_dir=tmp_path)
    previous_snapshot = _current(tmp_path)
    previous_history = (previous_snapshot / "history.jsonl").read_text(encoding="utf-8")
    established = {path.name for path in (tmp_path / "snapshots").iterdir()}

    def fail_history_write(path, content):
        if path.name == "history.jsonl":
            raise OSError("disk full")
        path.write_text(content, encoding="utf-8")
        publisher.os.chmod(path, 0o644)

    monkeypatch.setattr(publisher, "_write_snapshot_file", fail_history_write)

    assert publisher.publish_health_document(_document(), data_dir=tmp_path) is False
    assert _current(tmp_path) == previous_snapshot
    assert (previous_snapshot / "history.jsonl").read_text(encoding="utf-8") == previous_history
    assert {path.name for path in (tmp_path / "snapshots").iterdir()} == established


def test_health_data_directory_must_not_be_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    data_dir = tmp_path / "health"
    data_dir.symlink_to(target, target_is_directory=True)

    assert publisher.publish_health_document(_document(), data_dir=data_dir) is False


def test_normalized_document_ignores_generated_at_and_running_seconds():
    first = _document(generated_at="2026-11-26T12:00:00+00:00", running_seconds=1)
    second = _document(generated_at="2026-11-26T12:05:00+00:00", running_seconds=2)

    assert publisher._same_health_state(first, second)

    second["reports"][0]["status"] = "critical"
    assert not publisher._same_health_state(first, second)
