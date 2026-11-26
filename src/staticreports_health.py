# Copyright 2026 Canonical
# See LICENSE file for licensing details.

"""Shared, read-only health evaluation for Ubuntu Static Reports.

The report wrappers optionally record normal outcomes here because systemctl
cannot distinguish a publication from a successful no-op. Systemd remains the
authority for running, skipped, failed, and timed-out invocations.
"""

import json
import logging
import os
import re
import stat
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
MAX_OUTCOME_RECORD_SIZE = 64 * 1024
DEFAULT_HEALTH_DIR = Path("/var/lib/staticreports/health")
ARCHIVE_MIRROR_ENV_PATH = Path("/etc/staticreports/archive-mirror.env")
DEFAULT_MIRROR_DIR = Path("/srv/staticreports")
DEFAULT_WEB_ROOT = Path("/srv/staticreports/www")
DEFAULT_HOME = Path("/home/ubuntu")
REPORT_DESCRIPTION_PREFIX = "Ubuntu Static Reports - "
STRUCTURED_OUTCOME_SERVICES = frozenset(
    {
        "update-archive-mirror",
        "update-germinate",
        "update-mismatches",
        "update-nbs",
        "seeded-in-ubuntu-indexer",
    }
)
OUTCOMES = frozenset(
    {
        "published",
        "unchanged",
        "already_processed",
        "locked",
        "missing_input",
        "failed",
    }
)
_STARTED_AT = datetime.now(timezone.utc)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportSpec:
    """Health policy derived from a report's systemd service and timer."""

    interval: int | None = None
    jitter: int = 0
    runtime_limit: int = 7200
    parent: str | None = None
    credential: bool = False
    structured: bool = False


def _duration_seconds(value: str | None) -> int | None:
    """Convert the systemd duration forms used by these units to seconds."""
    if not value:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zA-Z]*)", value.strip())
    if not match:
        raise ValueError(f"Unsupported systemd duration: {value!r}")
    amount, unit = match.groups()
    factors = {"": 1, "s": 1, "sec": 1, "min": 60, "h": 3600, "d": 86400}
    if unit not in factors:
        raise ValueError(f"Unsupported systemd duration unit: {value!r}")
    return int(float(amount) * factors[unit])


def _unit_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.lstrip().startswith(("#", ";")):
            values[key.strip()] = value.strip()
    return values


def _load_report_specs(unit_dir: Path) -> dict[str, ReportSpec]:
    """Load policy from deployed units, with source units as a development fallback."""
    if not (unit_dir / "update-bugpatterns.service").exists():
        unit_dir = Path(__file__).parent / "systemd"
    services = {
        path.stem: values
        for path in sorted(unit_dir.glob("*.service"))
        if (values := _unit_values(path))
        .get("Description", "")
        .startswith(REPORT_DESCRIPTION_PREFIX)
    }
    parents = {}
    for parent, service in services.items():
        for child in service.get("OnSuccess", "").split():
            parents[child.removesuffix(".service")] = parent
    specs = {}
    for service_name, service in services.items():
        timer_path = unit_dir / f"{service_name}.timer"
        timer = _unit_values(timer_path) if timer_path.exists() else {}
        limits = [
            value
            for key in ("TimeoutStartSec", "RuntimeMaxSec")
            if (value := _duration_seconds(service.get(key))) is not None
        ]
        specs[service_name] = ReportSpec(
            interval=_duration_seconds(timer.get("OnUnitActiveSec")),
            jitter=_duration_seconds(timer.get("RandomizedDelaySec")) or 0,
            runtime_limit=min(limits) if limits else 7200,
            parent=parents.get(service_name),
            credential="ConditionFileNotEmpty" in service,
            structured=service_name in STRUCTURED_OUTCOME_SERVICES,
        )
    return specs


def _parents_first(specs: dict[str, ReportSpec]) -> dict[str, ReportSpec]:
    ordered = []

    def add(name: str) -> None:
        if name in ordered:
            return
        parent = specs[name].parent
        if parent in specs:
            add(parent)
        ordered.append(name)

    for name in sorted(specs):
        add(name)
    return {name: specs[name] for name in ordered}


REPORT_SPECS = _parents_first(
    _load_report_specs(Path(os.environ.get("STATIC_REPORTS_UNIT_DIR", "/etc/systemd/system")))
)
SERVICE_NAMES = tuple(REPORT_SPECS)


@dataclass
class Evidence:
    """Collected evidence for one report; pure evaluation consumes this."""

    service: dict[str, str] = field(default_factory=dict)
    timer: dict[str, str] | None = None
    record: dict[str, Any] | None = None
    record_error: str = ""
    outputs_present: bool = False
    current_snapshot: str = ""
    parent_snapshot: str = ""
    stamp_input_snapshot: str = ""


class HealthCollectionError(RuntimeError):
    """Raised when health evidence cannot be collected."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the local-only probe from following redirects to another address."""

    def redirect_request(self, *args, **kwargs):
        """Disable redirect following for the loopback-only probe."""
        return None


def utc_now() -> datetime:
    """Return the current UTC time; isolated for deterministic tests."""
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    """Format a UTC timestamp consistently in outcome and result documents."""
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO epoch or explicitly UTC systemctl realtime timestamp."""
    if not value or value in {"0", "n/a"}:
        return None
    if value.startswith("@"):
        try:
            return datetime.fromtimestamp(float(value[1:]), tz=timezone.utc)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        # Naive ISO timestamps are produced only by our tests and callers in UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snapshot_name(path: Path) -> str:
    """Return the resolved snapshot directory name, or an empty string if absent."""
    try:
        return path.resolve(strict=True).name if path.is_dir() else ""
    except (OSError, RuntimeError):
        return ""


def _previous_outcome(path: Path) -> dict[str, Any]:
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return previous if isinstance(previous, dict) else {}


def record_outcome(
    service: str,
    outcome: str,
    reason: str = "",
    *,
    input_snapshot: str = "",
    output_snapshot: str = "",
    details: dict[str, Any] | None = None,
    health_dir: Path | None = None,
) -> bool:
    """Atomically record a normal wrapper outcome without affecting the report."""
    if service not in STRUCTURED_OUTCOME_SERVICES or outcome not in OUTCOMES:
        logger.error("Refusing invalid health outcome %s/%s", service, outcome)
        return False
    if (
        health_dir is None
        and not os.environ.get("INVOCATION_ID")
        and not os.environ.get("STATIC_REPORTS_HEALTH_DIR")
    ):
        # Direct developer invocations must not try to write the system directory.
        return False
    directory = health_dir or Path(
        os.environ.get("STATIC_REPORTS_HEALTH_DIR", str(DEFAULT_HEALTH_DIR))
    )
    outcome_path = directory / f"{service}.json"
    previous = _previous_outcome(outcome_path)
    finished_at = iso_time()
    if (
        details is None
        and outcome == "already_processed"
        and previous.get("input_snapshot") == input_snapshot
    ):
        details = previous.get("details", {})
    previous_publication = previous.get("last_publication_at", "")
    if not previous_publication and previous.get("outcome") == "published":
        previous_publication = previous.get("finished_at", "")
    document = {
        "schema_version": SCHEMA_VERSION,
        "service": service,
        "invocation_id": os.environ.get("INVOCATION_ID", ""),
        "started_at": iso_time(_STARTED_AT),
        "finished_at": finished_at,
        "outcome": outcome,
        "reason": reason,
        "input_snapshot": input_snapshot,
        "output_snapshot": output_snapshot,
        "last_publication_at": (finished_at if outcome == "published" else previous_publication),
        "details": details if details is not None else {},
    }
    try:
        serialized = json.dumps(document, sort_keys=True)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{service}.{os.getpid()}.tmp"
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, outcome_path)
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Could not record %s health outcome: %s", service, error)
        return False
    return True


def _property_time(properties: dict[str, str], *names: str) -> datetime | None:
    for name in names:
        if parsed := parse_time(properties.get(name)):
            return parsed
    return None


def _latest_attempt(properties: dict[str, str]) -> datetime | None:
    return _property_time(
        properties,
        "ExecMainStartTimestamp",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
    )


def _completion_time(properties: dict[str, str]) -> datetime | None:
    return _property_time(
        properties,
        "ExecMainExitTimestamp",
        "ActiveExitTimestamp",
        "InactiveEnterTimestamp",
    )


def _failed(properties: dict[str, str]) -> bool:
    result = properties.get("Result", "")
    status = properties.get("ExecMainStatus", "0")
    return (
        properties.get("ActiveState") == "failed" or result not in {"", "success"} or status != "0"
    )


def _succeeded(properties: dict[str, str]) -> bool:
    return (
        not _failed(properties)
        and properties.get("Result") == "success"
        and _completion_time(properties) is not None
    )


def _missing_outputs_status(outputs_present: bool) -> str:
    return "warning" if outputs_present else "critical"


def _running_status(spec: ReportSpec, service: dict[str, str], now: datetime) -> dict | None:
    if service.get("ActiveState") not in {"active", "activating", "reloading"}:
        return None
    status = {"status": "ok", "outcome": "running", "reason": "Report is running."}
    attempt = _latest_attempt(service)
    if attempt:
        elapsed = max(0, int((now - attempt).total_seconds()))
        status["running_seconds"] = elapsed
        if elapsed > spec.runtime_limit:
            status.update(
                status="critical",
                reason=f"Report exceeded its {spec.runtime_limit}-second runtime limit.",
            )
    return status


def _terminal_status(
    spec: ReportSpec, service: dict[str, str], outputs_present: bool
) -> dict | None:
    if service.get("ConditionResult") == "no":
        return {
            "status": "waiting" if spec.credential else "warning",
            "outcome": "condition_skipped",
            "reason": (
                "Waiting for Launchpad credentials; this authenticated report is disabled."
                if spec.credential
                else "The systemd condition skipped this report."
            ),
        }
    if _failed(service):
        return {
            "status": _missing_outputs_status(outputs_present),
            "outcome": "failed",
            "reason": "The latest report invocation failed; inspect its journal.",
        }
    return None


def _timer_status(
    spec: ReportSpec,
    timer: dict[str, str] | None,
    now: datetime,
    outputs_present: bool,
) -> dict | None:
    if timer is None:
        return {
            "status": _missing_outputs_status(outputs_present),
            "outcome": "unknown",
            "reason": "No systemd timer evidence is available.",
        }
    if timer.get("LoadState", "loaded") != "loaded":
        return {
            "status": _missing_outputs_status(outputs_present),
            "outcome": "unknown",
            "reason": "The report timer is not loaded.",
        }
    if timer.get("UnitFileState") == "disabled" or timer.get("ActiveState") != "active":
        return {
            "status": _missing_outputs_status(outputs_present),
            "outcome": "disabled",
            "reason": "The report timer is disabled or inactive.",
        }
    anchor = _property_time(timer, "LastTriggerUSec", "ActiveEnterTimestamp")
    threshold = 2 * ((spec.interval or 0) + spec.jitter) + 60
    if anchor and (now - anchor).total_seconds() > threshold:
        return {
            "status": _missing_outputs_status(outputs_present),
            "outcome": "stale",
            "reason": f"No timer trigger for more than {threshold} seconds.",
        }
    return None


def _record_problem(name: str, evidence: Evidence) -> dict | None:
    record = evidence.record
    if record is None:
        return {
            "status": "unknown",
            "outcome": "unknown",
            "reason": evidence.record_error or "Awaiting the next recorded report run.",
        }
    invocation = evidence.service.get("InvocationID", "")
    recorded_invocation = str(record.get("invocation_id", ""))
    if invocation and recorded_invocation and invocation != recorded_invocation:
        return {
            "status": "unknown",
            "outcome": "unknown",
            "reason": "The outcome record is not from the latest systemd invocation.",
        }
    outcome = record["outcome"]
    reason = str(record.get("reason") or f"Latest outcome: {outcome}.")
    if outcome in {"locked", "missing_input", "failed"}:
        return {
            "status": _missing_outputs_status(evidence.outputs_present),
            "outcome": outcome,
            "reason": reason,
        }
    if outcome not in {"published", "unchanged", "already_processed"}:
        return {"status": "unknown", "outcome": "unknown", "reason": "Unknown outcome record."}
    return None


def _lineage_current(name: str, evidence: Evidence) -> bool:
    record = evidence.record or {}
    if name == "update-archive-mirror":
        return (
            bool(evidence.current_snapshot)
            and record.get("output_snapshot") == evidence.current_snapshot
        )
    if name == "update-germinate":
        return (
            bool(evidence.current_snapshot)
            and record.get("output_snapshot") == evidence.current_snapshot
            and record.get("input_snapshot") == evidence.parent_snapshot
            and evidence.stamp_input_snapshot == evidence.parent_snapshot
        )
    return (
        bool(evidence.parent_snapshot) and record.get("input_snapshot") == evidence.parent_snapshot
    )


def _structured_status(name: str, evidence: Evidence) -> dict:
    if problem := _record_problem(name, evidence):
        return problem
    record = evidence.record or {}
    result: dict[str, Any] = {
        "status": "ok",
        "outcome": record["outcome"],
        "reason": str(record.get("reason") or f"Latest outcome: {record['outcome']}."),
    }
    if finished := parse_time(record.get("finished_at")):
        result["last_success_at"] = iso_time(finished)
    published_at = parse_time(record.get("last_publication_at"))
    if published_at:
        result["last_publication_at"] = iso_time(published_at)
    elif record["outcome"] == "published" and finished:
        # Compatibility with the initial outcome schema.
        result["last_publication_at"] = iso_time(finished)
    if not evidence.outputs_present:
        result.update(status="critical", reason="Expected report output is missing or empty.")
        return result
    lineage_current = _lineage_current(name, evidence)
    result["lineage_current"] = lineage_current
    if not lineage_current:
        result.update(status="warning", reason="Report lineage is behind its current input.")
    elif record.get("details", {}).get("complete") is False:
        result.update(status="warning", reason="The latest run skipped expected outputs.")
    return result


def _initial_result(name: str, evidence: Evidence) -> dict[str, Any]:
    result: dict[str, Any] = {
        "service": name,
        "status": "unknown",
        "outcome": "unknown",
        "reason": "Health evidence is incomplete.",
    }
    if attempt := _latest_attempt(evidence.service):
        result["last_attempt_at"] = iso_time(attempt)
    completed = _completion_time(evidence.service)
    if completed and not _failed(evidence.service):
        result["last_success_at"] = iso_time(completed)
    if evidence.current_snapshot:
        result["output_snapshot"] = evidence.current_snapshot
    if evidence.parent_snapshot:
        result["input_snapshot"] = evidence.parent_snapshot
    return result


def _completed_report_status(
    name: str, evidence: Evidence, completed: datetime | None
) -> dict[str, Any]:
    if REPORT_SPECS[name].structured:
        return _structured_status(name, evidence)
    if not evidence.outputs_present:
        return {"status": "critical", "reason": "Expected report output is missing or empty."}
    result = {
        "status": "ok",
        "outcome": "published",
        "reason": "Latest run completed; output exists.",
    }
    if completed:
        result["last_publication_at"] = iso_time(completed)
    return result


def evaluate_report(
    name: str,
    evidence: Evidence,
    now: datetime,
    parent_status: str = "ok",
) -> dict[str, Any]:
    """Evaluate one report without performing I/O."""
    spec = REPORT_SPECS[name]
    result = _initial_result(name, evidence)
    if not evidence.service:
        result["reason"] = "No systemd service evidence is available."
        return result
    if evidence.service.get("LoadState", "loaded") != "loaded":
        result.update(status="critical", reason="The report service is not loaded.")
        return result

    for status in (
        _running_status(spec, evidence.service, now),
        _terminal_status(spec, evidence.service, evidence.outputs_present),
    ):
        if status:
            result.update(status)
            return result
    if spec.interval:
        if status := _timer_status(spec, evidence.timer, now, evidence.outputs_present):
            result.update(status)
            return result
    if not _succeeded(evidence.service):
        result["reason"] = "No successful completion is recorded for the latest invocation."
        return result

    result.update(_completed_report_status(name, evidence, _completion_time(evidence.service)))

    if parent_status in {"warning", "critical"} and result["status"] == "ok":
        result.update(
            status="warning",
            outcome="dependency_blocked",
            reason=(
                f"Upstream report {spec.parent} needs attention; existing output remains usable."
            ),
        )
    return result


def _selected_service_names(service: str | None) -> list[str]:
    if service is not None and service not in SERVICE_NAMES:
        raise ValueError(f"Unknown static report service: {service}")
    selected: list[str] = []

    def select_with_parents(name: str) -> None:
        parent = REPORT_SPECS[name].parent
        if parent:
            select_with_parents(parent)
        if name not in selected:
            selected.append(name)

    if service:
        select_with_parents(service)
    else:
        selected.extend(SERVICE_NAMES)
    return selected


def _overall(statuses: list[str]) -> tuple[str, str]:
    overall = "ok"
    for candidate in ("critical", "unknown", "warning", "waiting"):
        if candidate in statuses:
            overall = candidate
            break
    attention = sum(status != "ok" for status in statuses)
    summary = (
        "All checks passed."
        if overall == "ok"
        else f"{attention} of {len(statuses)} checks need attention."
    )
    return overall, summary


def _configured_mirror_dir(config_path: Path) -> Path:
    """Read the charm-managed mirror setting, falling back to its default."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return DEFAULT_MIRROR_DIR
    for line in lines:
        if line.startswith("MIRROR_DIR="):
            value = line.split("=", 1)[1].strip()
            return Path(value) if value else DEFAULT_MIRROR_DIR
    return DEFAULT_MIRROR_DIR


class HealthCollector:
    """Collect bounded local evidence and produce a versioned health document."""

    def __init__(
        self,
        *,
        mirror_dir: Path | None = None,
        config_path: Path = ARCHIVE_MIRROR_ENV_PATH,
        web_root: Path = DEFAULT_WEB_ROOT,
        home: Path = DEFAULT_HOME,
        health_dir: Path = DEFAULT_HEALTH_DIR,
        now: Callable[[], datetime] = utc_now,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        http_probe: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.mirror_dir = mirror_dir or _configured_mirror_dir(config_path)
        self.web_root = web_root
        self.home = home
        self.health_dir = health_dir
        self.now = now
        self.runner = runner
        self.http_probe = http_probe or self._nginx_probe

    def _systemd(self) -> dict[str, dict[str, str]]:
        properties = (
            "Id,LoadState,ActiveState,SubState,Result,ConditionResult,ExecMainStatus,"
            "InvocationID,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime,"
            "ActiveEnterTimestamp,ActiveExitTimestamp,InactiveEnterTimestamp,"
            "ExecMainStartTimestamp,ExecMainExitTimestamp"
        )
        units = [f"{name}.service" for name in SERVICE_NAMES]
        units += [f"{name}.timer" for name, spec in REPORT_SPECS.items() if spec.interval]
        units.append("nginx.service")
        try:
            completed = self.runner(
                [
                    "systemctl",
                    "show",
                    *units,
                    f"--property={properties}",
                    "--timestamp=utc",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                env={**os.environ, "TZ": "UTC"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise HealthCollectionError(f"Could not inspect systemd units: {error}") from error
        documents: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if not line.strip():
                if current.get("Id"):
                    documents[current["Id"]] = current
                current = {}
                continue
            key, separator, value = line.partition("=")
            if separator:
                current[key] = value
        if current.get("Id"):
            documents[current["Id"]] = current
        if not documents:
            raise HealthCollectionError("systemctl returned no unit evidence.")
        return documents

    def _record(self, name: str) -> tuple[dict[str, Any] | None, str]:
        path = self.health_dir / f"{name}.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError("outcome record is not a regular file")
                if metadata.st_size > MAX_OUTCOME_RECORD_SIZE:
                    raise OSError("outcome record exceeds the size limit")
                content = os.read(descriptor, MAX_OUTCOME_RECORD_SIZE + 1)
                if len(content) > MAX_OUTCOME_RECORD_SIZE:
                    raise OSError("outcome record exceeds the size limit")
            finally:
                os.close(descriptor)
            record = json.loads(content.decode("utf-8"))
        except FileNotFoundError:
            return None, ""
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return None, f"Could not read outcome record: {error}"
        if not isinstance(record, dict) or (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("service") != name
            or record.get("outcome") not in OUTCOMES
        ):
            return None, "Outcome record has an unsupported schema."
        if (
            not isinstance(record.get("finished_at"), str)
            or parse_time(record["finished_at"]) is None
            or not isinstance(record.get("details", {}), dict)
            or not isinstance(record.get("invocation_id", ""), str)
            or not isinstance(record.get("reason", ""), str)
            or not isinstance(record.get("input_snapshot", ""), str)
            or not isinstance(record.get("output_snapshot", ""), str)
            or not isinstance(record.get("last_publication_at", ""), str)
        ):
            return None, "Outcome record has invalid field types."
        return record, ""

    @staticmethod
    def _file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _directory(path: Path, contents: bool = False) -> bool:
        try:
            return path.is_dir() and (not contents or next(path.iterdir(), None) is not None)
        except OSError:
            return False

    def _outputs_present(self, name: str) -> bool:  # noqa: C901
        www = self.web_root
        fixed_files = {
            "update-bugpatterns": (www / "bugpatterns" / "bugpatterns.xml",),
            "update-sync-blocklist": (www / "sync-blocklist.txt",),
            "package-subscribers": (www / "package-team-mapping.json",),
            "permissions-report": (www / "archive-permissions" / "teams",),
            "sru-report": (www / "pending-sru" / "pending-sru.html",),
            "update-nbs": (www / "nbs" / "nbs.html", www / "nbs" / "nbs.csv"),
        }
        if name in fixed_files:
            return all(self._file(path) for path in fixed_files[name])
        if name == "update-seeds":
            return self._directory(www / "seeds", contents=True)
        if name == "packageset-report":
            return self._directory(www / "packagesets", contents=True)
        if name == "update-archive-mirror":
            current = self.mirror_dir / "archive" / "current"
            return self._file(current / "STAMP") and self._directory(current / "ubuntu" / "dists")
        if name == "update-germinate":
            current = self.mirror_dir / "germinate" / "current"
            return (
                self._file(current / "STAMP")
                and self._directory(current / "flavours")
                and self._directory(current / "flat" / "release")
            )
        if name == "update-mismatches":
            names = []
            for base in ("component-mismatches", "component-mismatches-proposed"):
                names.extend(f"{base}.{suffix}" for suffix in ("txt", "html", "csv", "dot", "svg"))
            for base in ("architecture-mismatches", "priority-mismatches"):
                names.extend(f"{base}.{suffix}" for suffix in ("txt", "html", "csv"))
            names.extend(("pocket-mismatches.txt", "pocket-mismatches.html"))
            return all(self._file(www / "mismatches" / filename) for filename in names)
        if name == "seeded-in-ubuntu-indexer":
            try:
                return any(
                    self._file(path)
                    for path in (www / "seeded-in-ubuntu").glob("seeded-*.json.gz")
                )
            except OSError:
                return False
        return False

    @staticmethod
    def _snapshot(path: Path) -> str:
        return snapshot_name(path)

    @staticmethod
    def _stamp_input(snapshot_path: Path) -> str:
        try:
            lines = (snapshot_path / "STAMP").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in lines:
            if line.startswith("Archive-snapshot:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _snapshots(self, name: str) -> tuple[str, str, str]:
        archive = self.mirror_dir / "archive" / "current"
        germinate = self.mirror_dir / "germinate" / "current"
        if name == "update-archive-mirror":
            return self._snapshot(archive), "", ""
        if name == "update-germinate":
            return self._snapshot(germinate), self._snapshot(archive), self._stamp_input(germinate)
        if name == "update-mismatches" or name == "seeded-in-ubuntu-indexer":
            return "", self._snapshot(germinate), ""
        if name == "update-nbs":
            return "", self._snapshot(archive), ""
        return "", "", ""

    def _nginx_probe(self) -> tuple[bool, str]:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )
        try:
            with opener.open("http://127.0.0.1/robots.txt", timeout=2) as response:
                if response.status == 200:
                    return True, "nginx serves the local robots.txt probe."
                return False, f"Local nginx probe returned HTTP {response.status}."
        except OSError as error:
            return False, f"Local nginx probe failed: {error}"

    def _serving(self, units: dict[str, dict[str, str]], service: str | None) -> dict | None:
        if service is not None:
            return None
        nginx = units.get("nginx.service", {})
        probe_ok, probe_reason = self.http_probe()
        serving_ok = nginx.get("ActiveState") == "active" and probe_ok
        return {
            "status": "ok" if serving_ok else "critical",
            "reason": (
                probe_reason if nginx.get("ActiveState") == "active" else "nginx is not active."
            ),
        }

    def collect(self, service: str | None = None) -> dict[str, Any]:
        """Return health for one report or all reports and local serving."""
        selected = _selected_service_names(service)
        now = self.now().astimezone(timezone.utc)
        units = self._systemd()
        statuses: dict[str, str] = {}
        reports = []
        for name in selected:
            spec = REPORT_SPECS[name]
            record, record_error = self._record(name) if spec.structured else (None, "")
            current, parent, stamp_input = self._snapshots(name)
            evidence = Evidence(
                service=units.get(f"{name}.service", {}),
                timer=units.get(f"{name}.timer") if spec.interval else None,
                record=record,
                record_error=record_error,
                outputs_present=self._outputs_present(name),
                current_snapshot=current,
                parent_snapshot=parent,
                stamp_input_snapshot=stamp_input,
            )
            report = evaluate_report(name, evidence, now, statuses.get(spec.parent or "", "ok"))
            statuses[name] = report["status"]
            if service is None or name == service:
                reports.append(report)

        serving = self._serving(units, service)
        statuses_to_reduce = [report["status"] for report in reports]
        if serving:
            statuses_to_reduce.append(serving["status"])
        overall, summary = _overall(statuses_to_reduce)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_time(now),
            "status": overall,
            "summary": summary,
            "reports": reports,
            "serving": serving,
        }


def exit_code(document: dict[str, Any]) -> int:
    """Map a health document to its documented process exit code."""
    return {"ok": 0, "waiting": 1, "warning": 1, "critical": 2, "unknown": 3}[document["status"]]
