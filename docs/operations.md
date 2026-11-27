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

Juju renders action results as YAML. The nested `report` value is still the
versioned JSON document; parse that string with a JSON parser to consume the
same document produced by `static-reports-diagnose --format json`.

## Published health snapshots

Every `update-status` hook publishes the latest health snapshot. The Juju
action and installed CLI remain read-only and do not change these files.

The stable URLs for the current snapshot are:

```text
/health/health.json
/health/health.txt
```

`health.json` contains the current versioned JSON document. `health.txt`
contains the same human-readable rendering as the CLI's default output.

Older state changes remain browsable as complete snapshot directories:

```text
/health/snapshots/<snapshot>/health.json
/health/snapshots/<snapshot>/health.txt
```

Each snapshot is prepared in full in a root-owned directory under
`/srv/staticreports/www/health/snapshots`. One atomic `current` link switch
makes its JSON and text files visible together. A failed publication leaves
the previous complete snapshot selected, so consumers do not see a partially
updated set.

Snapshot deduplication compares the full document while ignoring `generated_at`
and per-report `running_seconds`. An unchanged semantic state replaces the
current snapshot with the latest sample instead of retaining another duplicate.
A changed state creates a new snapshot directory.

The retained snapshot files are capped at 5 MiB in total. When the cap is
exceeded, the oldest snapshots are removed first; the newest complete snapshot
is always retained. There is no fixed time-based retention. These human-readable
snapshots are a lightweight pre-COS interface and do not replace time-series
storage or alerting.

## Automatic health in `juju status`

The charm also handles Juju's periodic `update-status` event. Juju normally emits
it every five minutes; the model-wide `update-status-hook-interval` setting can
change that cadence. The charm refreshes only the cheap shared health snapshot
and then uses Ops' `collect_unit_status` event to reconcile it with setup,
configuration, and maintenance state. This is independent of COS and remains
available through the same APIs in Juju 3.6 and Juju 4.

Two matching non-OK samples are required before publishing a new degraded,
unknown, or critical health state. The first healthy sample clears a previous
health message immediately. Existing setup and configuration messages retain
their normal priority.

- Healthy reports produce ordinary `active` status with no health message.
- Degraded reports produce an `active` message directing the operator to
  `static-reports-diagnose`.
- Missing optional Launchpad credentials leave authenticated reports waiting
  with an explanatory `active` message.
- Persistent critical local workload health produces `blocked` status with the
  diagnostic action as the next step.

Periodic checks never run reports, perform recovery, search journals, contact
Launchpad, probe external addresses, or inspect network routes. They use bounded
systemd properties, small outcome/state files, fixed output checks, and the
loopback nginx probe. Full history and future alerting belong to COS, not Juju
status messages.

## Targeted recovery with `run-report`

`run-report` synchronously runs exactly one report service:

```bash
juju run ubuntu-static-reports/0 run-report service=update-nbs
```

The `service` parameter is required and accepts one of the twelve report service
names listed by `static-reports-diagnose --help`. There is no all-report mode and
no `force` option. Normal unchanged-input and already-processed checks still
apply. Declared systemd `OnSuccess=` dependencies may trigger downstream reports
after an upstream service succeeds.

The action refuses to interrupt an active service. Only use the following form
when intentionally interrupting and restarting that service, for example after
diagnosing a stuck invocation:

```bash
juju run ubuntu-static-reports/0 run-report service=update-mismatches restart=true
```

Some reports can take hours. Run long jobs in the background:

```bash
juju run --background ubuntu-static-reports/0 run-report service=sru-report
```

The command returns a Juju task ID. Inspect it with `juju show-task <task-id>`.
After completion, use `static-reports-diagnose` to inspect the selected report's
health. Action results distinguish the execution outcome from the resulting
report health. Actual start/restart errors and credential-condition skips fail
the action; a valid no-change result does not.

The former all-report `refresh` action was removed. It did not actually bypass
unchanged-input checks and could hide failures. Migrate old commands as follows:

```bash
# Old, removed:
# juju run ubuntu-static-reports/0 refresh

# New: select one report explicitly, optionally in the background:
juju run ubuntu-static-reports/0 run-report service=update-seeds
```

Do not run the new action periodically from monitoring. It is an explicitly
operator-requested workload mutation, separate from all diagnostic interfaces.

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

The document is shared by the CLI, the Juju diagnostic action, and the published
health files. It is also the intended input to the cached exporter described in
[the COS integration handover](../handover-and-plan-for-cos.md).

## Follow-up commands

For a failed report, inspect its workload journal, for example:

```bash
journalctl -eu update-nbs.service --no-pager
```

Use `juju debug-log` for charm hook and action implementation errors, not as a
replacement for the report services' journald output.

## Launchpad credentials

Only `permissions-report` and `packageset-report` require the OAuth credential
from `lpuser_secret_id`. `update-mismatches` uses anonymous Launchpad access and
does not consume or propagate that credential. No other report becomes
credential-dependent through this lifecycle.

When the secret configuration is unset or no usable credential is available,
the charm removes its managed OAuth file. This prevents stale credentials from
keeping the two authenticated reports enabled. With no secret configured, those
reports remain in `waiting` and other reports continue normally. An explicitly
configured but unavailable or malformed secret is invalid configuration and
puts the charm into `blocked` state until an operator corrects it.

Credential contents are never included in diagnostic output or logs.
