# Operating Ubuntu Static Reports

This document describes the generic operational interfaces provided by the
charm. Environment-specific models, ingress configuration, escalation paths,
and network incidents belong in the Ubuntu Engineering operations playbook.

## Diagnostic scope

`static-reports-diagnose` checks local workload health only:

- report timers and invocation state;
- normal report outcomes and output presence;
- archive and germinate snapshot lineage;
- local nginx state and a loopback request for `robots.txt`.

It does not start, stop, restart, or regenerate anything. It does not inspect
Launchpad credentials, contact Launchpad, scan Git repositories, follow external
redirects, or diagnose ingress, SAAS authorization, routing, or asymmetric
network paths.

## Installed CLI

Check every report and local nginx:

```bash
static-reports-diagnose
```

Check one report:

```bash
static-reports-diagnose --service update-nbs
```

Return the versioned JSON document instead of human-readable text:

```bash
static-reports-diagnose --format json
static-reports-diagnose --service update-germinate --format json
```

The command is installed at `/usr/bin/static-reports-diagnose` and can be used
through SSH or `juju exec`. With no `--service` argument it checks all twelve
report services and local nginx. A service selection checks that report and its
upstream dependency state, but omits the nginx result.

Use `static-reports-diagnose --help` for a short usage summary, exit codes, and a
link to this document. Help does not perform health checks.

## Juju diagnostic action

The Juju action has the same name, default, service selection, and health
semantics as the installed CLI:

```bash
# All reports and local nginx:
juju run ubuntu-static-reports/0 static-reports-diagnose

# One report and its upstream dependency state:
juju run ubuntu-static-reports/0 static-reports-diagnose service=update-nbs
```

The action returns `schema-version`, `generated-at`, `status`, `summary`, and a
`report` string containing the same JSON document as `--format json`. An
unhealthy result is a completed diagnostic action, not an action failure.
Invalid parameters or an inability to collect evidence fail the action. The
action is read-only and does not start, stop, restart, or regenerate reports.

## CLI exit codes

| Code | Meaning |
|---|---|
| `0` | All requested checks are OK. |
| `1` | At least one report is degraded or waiting, but existing output may remain usable. |
| `2` | A critical local workload check failed, such as missing output or unavailable nginx. |
| `3` | Health is unknown or evidence collection failed. |

A collected warning or critical result is not a command execution error. It is a
valid diagnostic result and is rendered in the requested format.

## Health and outcome meanings

Health is separate from the latest normal outcome:

- **OK**: the requested checks passed.
- **Warning**: a report needs attention, but previous output may remain usable.
- **Critical**: required local output or serving is unavailable.
- **Waiting**: an authenticated report is intentionally waiting for credentials.
- **Unknown**: evidence is missing, stale, malformed, or awaiting the next run
  after an upgrade.

Normal outcomes include:

- `published`: new output was published;
- `unchanged`: inputs did not change, so existing output remains current;
- `already_processed`: the current input snapshot was already processed;
- `locked`: another invocation held the report lock;
- `missing_input`: required input was absent or incomplete;
- `condition_skipped`: a systemd condition prevented execution;
- `failed`: the latest invocation failed;
- `running`: execution is still within its configured runtime limit.

A successful systemd exit alone does not prove health. In particular,
`unchanged` and `already_processed` can be healthy, while `locked` and
`missing_input` must not be mistaken for new publications. Output mtimes are not
used as universal freshness indicators: some reports preserve source mtimes,
and snapshot-backed reports publish only when their inputs change.

Timer freshness uses two complete schedule-plus-jitter cycles with additional
accuracy slack. These conservative thresholds are diagnostic heuristics, not
per-report service-level objectives. Runtime checks use the actual two-hour or
eight-hour systemd limits. No recovery is triggered by these checks.

## JSON document

The top-level document contains `schema_version`, `generated_at`, `status`,
`summary`, `reports`, and `serving`. Each report contains its service name,
health status, outcome, reason, and applicable attempt, success, publication,
runtime, and lineage fields. Consumers must tolerate additive fields within the
same schema version.

The document is shared by the CLI and the Juju diagnostic action. It is also the
intended input to the cached exporter described in
`handover-and-plan-for-cos.md`.

## Follow-up commands

For a failed report, inspect its workload journal, for example:

```bash
journalctl -eu update-nbs.service --no-pager
```

Use `juju debug-log` for charm hook and action implementation errors, not as a
replacement for the report services' journald output.
