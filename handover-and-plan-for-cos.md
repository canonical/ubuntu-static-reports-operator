# Handover and plan for COS integration

## Status and purpose

The charm now provides one local health implementation used by:

- `/usr/bin/static-reports-diagnose` for SSH and `juju exec`;
- the `static-reports-diagnose` Juju action;
- concise, periodic workload-health summaries in `juju status`.

The shared result is a versioned JSON document containing overall health,
per-report health and outcomes, timestamps, snapshot lineage, and local nginx
health. This document is the contract to reuse for Canonical Observability Stack
(COS) integration. No COS environment, relation, exporter, dashboard, or alert
rule is required by the current implementation.

This document is the handover for a later, separately reviewed COS
implementation. It is not a commitment to deploy COS or to change model
automation.

## Scope and non-goals

The future integration should report local workload health and workload logs.
It should not:

- run reports, restart services, or mutate routes from monitoring;
- diagnose SAAS authorization, ingress configuration, asymmetric routing, or
  other infrastructure network paths;
- put detailed diagnostics into Juju status messages;
- create a second implementation of the report-health rules;
- use Grafana Agent, which has been superseded by OpenTelemetry Collector for
  the documented machine-charm architecture;
- depend on a collector-private node-exporter textfile directory.

External HTTP/blackbox monitoring of the public site is a separate deployment
decision. It is not a responsibility of the local health helper.

## Reuse contract

The shared health document has `schema_version`, `generated_at`, `status`,
`summary`, `reports`, and `serving`. Each report has a fixed service name,
health status, outcome, reason, and applicable attempt, success, publication,
runtime, and lineage fields. Consumers must tolerate additive fields within one
schema version.

The current normal outcomes are `published`, `unchanged`, `already_processed`,
`locked`, `missing_input`, `condition_skipped`, `failed`, `running`, and
`unknown`. Health values are `ok`, `warning`, `critical`, `waiting`, and
`unknown`.

Do not derive COS health independently from journal messages. Journals provide
diagnostic detail; the shared evaluator provides the health meaning.

## Proposed architecture

```text
shared diagnostic collector (systemd service + timer)
        |
        | bounded local checks; atomic write of its own cache only
        v
/run/staticreports/diagnostics.json
        |
        v
small unprivileged HTTP exporter on 127.0.0.1:<selected-high-port>
        |
        | cos_agent scrape configuration
        v
OpenTelemetry Collector machine subordinate
        |
        +--> Prometheus/Mimir remote write
        +--> Loki for journald workload logs
        +--> Grafana dashboards
```

### Cached collector

Add a dedicated systemd service and timer only when implementing COS support.
The collector will invoke the same health implementation as the CLI and Juju
interfaces. It may write only its own diagnostic cache, atomically. It must not
write report state or run workload services.

A one-minute collection interval is a starting point, not a required SLO. The
collector needs a hard timeout smaller than that interval and a small output
cap. Do not rely on Juju's model-configurable `update-status` cadence for metric
freshness.

The cache belongs under `/run`, not in the web root. A reboot or missing recent
collection must result in unknown/missing telemetry, not stale healthy metrics.
Persistent per-wrapper normal outcomes remain under
`/var/lib/staticreports/health`.

### Metrics endpoint

Expose one loopback-only `/metrics` endpoint. The HTTP handler reads the cached
document and returns immediately; it must not invoke the diagnostic collector,
`systemctl`, `journalctl`, Git, Launchpad, or report output parsers during a
scrape. Target milliseconds, comfortably below the collector's default
10-second scrape timeout.

Run the exporter unprivileged. Do not bind to `0.0.0.0`, open a Juju port, or
publish the endpoint through ingress. Select and document the high port during
implementation. Do not expose secrets, raw logs, file contents, or command
parameters through it.

## Initial metric mapping

Use the `staticreports_` prefix and only the fixed `service` label for report
metrics. Proposed names are:

- `staticreports_report_health{service}`: `0=ok`, `1=warning/waiting`,
  `2=critical`, `3=unknown`;
- `staticreports_report_last_attempt_timestamp_seconds{service}`;
- `staticreports_report_last_success_timestamp_seconds{service}`;
- `staticreports_report_last_publication_timestamp_seconds{service}`;
- `staticreports_report_running_seconds{service}`;
- `staticreports_report_lineage_current{service}` for snapshot-backed reports;
- `staticreports_nginx_up`;
- `staticreports_diagnostics_collection_success`;
- `staticreports_diagnostics_collection_timestamp_seconds`.

Omit inapplicable timestamps and lineage values rather than reporting zero as a
real timestamp or false as a universal default. Keep outcome and reason details
in the diagnostic action and logs, not in high-cardinality labels.

Do not use snapshot IDs, package names, file paths, error strings, report
contents, or dynamically discovered series/architecture cells as labels. COS
will supply Juju topology labels; do not duplicate them in the exporter.

## Relation and log collection

Add a `cos-agent` provider relation with interface `cos_agent` and limit one.
Instantiate `COSAgentProvider` with the loopback metrics endpoint and explicit
rule/dashboard directories. Relate the principal to the
`opentelemetry-collector` machine subordinate from the supported 2.x channel,
then connect that collector to the COS remote-write, Loki, and Grafana offers.
Do not add direct per-principal backend relations.

Report services already write to journald. Validate collection through the
collector's documented machine `juju-info` path, including the resulting Juju
topology labels. Do not duplicate workload output into charm logs or create a
second workload log file solely for Loki. `juju debug-log` remains for charm
hook and action implementation errors.

## Dashboard and alert proposal

Start with one dashboard showing:

- local nginx and diagnostic collection health;
- per-report health;
- time since attempt, successful check, and publication where applicable;
- current runtime and snapshot lineage;
- archive → germinate → mismatch/NBS/indexer dependency state;
- relevant collector-provided host capacity metrics.

Initial alerts should be conservative and actionable:

- sustained diagnostic collection failure or stale cache;
- sustained individual report failure or staleness beyond the shared health
  thresholds;
- sustained critical local workload health;
- missing exporter telemetry, supplemented by COS generic host rules.

Do not alert on one missed invocation, a valid no-change result, an old snapshot
whose inputs have not changed, or intentionally waiting optional credentials.
Use `for` durations to require sustained conditions. Business-hours routing is
an Alertmanager/model concern, not something to hard-code in charm rules. Each
alert should link to the generic operations document and instruct the operator
to run `static-reports-diagnose`; environment-specific escalation remains in the
internal playbook.

## Implementation and validation sequence

When a staging COS path is available:

1. Add the cached collector and exporter with unit tests, including malformed
   cache data, stale cache, timeouts, and proof that scrapes do not execute
   diagnostics.
2. Validate loopback binding, metric names/types, bounded labels, JSON/dashboard
   syntax, Prometheus rules with `promtool`, and supported COS validation tools.
3. Add `cos-agent` and verify exactly one collector subordinate per principal.
4. Verify principal-unit metric topology, journal labels, rule delivery,
   dashboard delivery, and dashboard version upgrades.
5. In staging, induce selected report and exporter failures. Confirm sustained
   alerts, recovery, and absence of automatic workload mutation.
6. Confirm fresh deployments do not immediately alert while outcome records are
   still bootstrapping and that old caches cannot report healthy indefinitely.
7. Run the charm's full checks and user-run integration suite, then document
   the collector/COS offer integration required in the separate model
   automation repository.

Do not roll the COS integration into production before those end-to-end checks
pass. The existing CLI, actions, and Juju status behavior must continue to work
when COS is absent or its relation is removed.

## Open implementation checks

- Confirm the exact collector revision, scrape configuration, timeout behavior,
  and log topology in the target environment.
- Tune conservative alert durations using staging and production observations;
  the shared health thresholds are heuristics, not per-report SLOs.
- Decide separately whether public blackbox probing is wanted and where it
  belongs. It is not required for the local health exporter.
