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


def _snapshots(data_dir):
    snapshots = [
        path
        for path in (data_dir / "snapshots").iterdir()
        if path.name.startswith("snapshot.") and path.is_dir() and not path.is_symlink()
    ]
    return sorted(snapshots, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _snapshot_documents(data_dir):
    return [
        json.loads((snapshot / "health.json").read_text(encoding="utf-8"))
        for snapshot in _snapshots(data_dir)
    ]


def _snapshot_size(snapshot):
    return sum((snapshot / filename).stat().st_size for filename in ("health.json", "health.txt"))


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

    latest = json.loads((snapshot / "health.json").read_text(encoding="utf-8"))
    text = (snapshot / "health.txt").read_text(encoding="utf-8")
    assert latest == document
    assert text.startswith("Overall: OK - test summary\n")
    assert text.endswith("\n")
    assert not (snapshot / "history.jsonl").exists()

    assert (tmp_path / "health.json").is_symlink()
    assert (tmp_path / "health.txt").is_symlink()
    assert (tmp_path / "health.json").read_text(encoding="utf-8") == json.dumps(
        document, indent=2, sort_keys=True
    )
    assert (tmp_path / "health.txt").read_text(encoding="utf-8") == text
    assert {path.name for path in tmp_path.iterdir()} == {
        "snapshots",
        "current",
        "health.json",
        "health.txt",
    }


def test_published_snapshot_is_world_readable_and_staging_is_removed(tmp_path):
    document = _document()

    publisher.publish_health_document(document, data_dir=tmp_path)

    snapshot = _current(tmp_path)
    assert snapshot.stat().st_mode & 0o777 == 0o755
    assert (snapshot / "health.json").stat().st_mode & 0o777 == 0o644
    assert (snapshot / "health.txt").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".current.*"))
    assert not list((tmp_path / "snapshots").glob(".current.*"))


def test_unchanged_state_replaces_the_current_snapshot(tmp_path):
    first = _document(generated_at="2026-11-26T12:00:00+00:00", running_seconds=10)
    second = _document(generated_at="2026-11-26T12:05:00+00:00", running_seconds=20)

    publisher.publish_health_document(first, data_dir=tmp_path)
    publisher.publish_health_document(second, data_dir=tmp_path)

    snapshots = _snapshots(tmp_path)
    assert len(snapshots) == 1
    assert json.loads((snapshots[0] / "health.json").read_text(encoding="utf-8")) == second
    assert (
        (snapshots[0] / "health.txt")
        .read_text(encoding="utf-8")
        .startswith("Overall: OK - test summary\n")
    )
    assert not (snapshots[0] / "history.jsonl").exists()
    assert json.loads((tmp_path / "health.json").read_text(encoding="utf-8")) == second


def test_changed_state_retains_a_new_snapshot(tmp_path):
    healthy = _document(status="ok", reason="healthy")
    failed = _document(status="critical", reason="report failed")
    recovered = _document(status="ok", reason="report recovered")

    publisher.publish_health_document(healthy, data_dir=tmp_path)
    publisher.publish_health_document(failed, data_dir=tmp_path)
    publisher.publish_health_document(recovered, data_dir=tmp_path)

    documents = _snapshot_documents(tmp_path)
    assert [document["status"] for document in documents] == ["ok", "critical", "ok"]
    assert [document["reports"][0]["reason"] for document in documents] == [
        "healthy",
        "report failed",
        "report recovered",
    ]
    assert documents[-1] == recovered
    assert all(
        (snapshot / "health.txt").exists() and not (snapshot / "history.jsonl").exists()
        for snapshot in _snapshots(tmp_path)
    )
    assert json.loads((tmp_path / "health.json").read_text(encoding="utf-8")) == recovered


def test_snapshot_retains_the_full_reports_array(tmp_path):
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

    latest = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert [report["service"] for report in latest["reports"]] == [
        "update-nbs",
        "update-mismatches",
    ]


def test_size_limit_keeps_as_many_snapshots_as_fit(tmp_path):
    first = _document(status="ok", reason="first")
    second = _document(status="warning", reason="second")
    third = _document(status="critical", reason="third")

    publisher.publish_health_document(first, data_dir=tmp_path)
    publisher.publish_health_document(second, data_dir=tmp_path)
    budget = sum(_snapshot_size(snapshot) for snapshot in _snapshots(tmp_path)) + 100

    publisher.publish_health_document(third, data_dir=tmp_path, max_snapshot_bytes=budget)

    documents = _snapshot_documents(tmp_path)
    assert len(documents) == 2
    assert [document["status"] for document in documents] == ["warning", "critical"]
    assert documents[-1] == third


def test_size_limit_always_keeps_the_newest_snapshot(tmp_path):
    first = _document(status="ok", reason="first")
    second = _document(status="warning", reason="second")
    third = _document(status="critical", reason="third")

    publisher.publish_health_document(first, data_dir=tmp_path, max_snapshot_bytes=1)
    publisher.publish_health_document(second, data_dir=tmp_path, max_snapshot_bytes=1)
    publisher.publish_health_document(third, data_dir=tmp_path, max_snapshot_bytes=1)

    documents = _snapshot_documents(tmp_path)
    assert len(documents) == 1
    assert documents[0] == third


def test_failed_switch_preserves_the_previous_complete_snapshot(tmp_path, monkeypatch):
    first = _document(status="ok", reason="first")
    publisher.publish_health_document(first, data_dir=tmp_path)
    previous_snapshot = _current(tmp_path)
    previous_json = (previous_snapshot / "health.json").read_text(encoding="utf-8")
    previous_text = (previous_snapshot / "health.txt").read_text(encoding="utf-8")

    def fail_switch(data_dir, snapshot):
        raise OSError("current link swap failed")

    monkeypatch.setattr(publisher, "_switch_current", fail_switch)
    second = _document(status="critical", reason="second")

    assert publisher.publish_health_document(second, data_dir=tmp_path) is False

    assert _current(tmp_path) == previous_snapshot
    assert (previous_snapshot / "health.json").read_text(encoding="utf-8") == previous_json
    assert (previous_snapshot / "health.txt").read_text(encoding="utf-8") == previous_text
    assert [path.name for path in _snapshots(tmp_path)] == [previous_snapshot.name]


def test_publication_failure_removes_an_incomplete_snapshot(tmp_path, monkeypatch):
    publisher.publish_health_document(_document(), data_dir=tmp_path)
    previous_snapshot = _current(tmp_path)
    previous_text = (previous_snapshot / "health.txt").read_text(encoding="utf-8")
    established = {path.name for path in _snapshots(tmp_path)}

    def fail_text_write(path, content):
        if path.name == "health.txt":
            raise OSError("disk full")
        path.write_text(content, encoding="utf-8")
        publisher.os.chmod(path, 0o644)

    monkeypatch.setattr(publisher, "_write_snapshot_file", fail_text_write)

    assert publisher.publish_health_document(_document(), data_dir=tmp_path) is False
    assert _current(tmp_path) == previous_snapshot
    assert (previous_snapshot / "health.txt").read_text(encoding="utf-8") == previous_text
    assert {path.name for path in _snapshots(tmp_path)} == established


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
