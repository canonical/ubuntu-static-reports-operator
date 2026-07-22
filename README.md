# Ubuntu Static Reports Operator


**Ubuntu Static Reports Operator** is a [charm](https://juju.is/charms-architecture)
for building and presenting various static reports for Ubuntu. Fetching those
live from launchpad API or similar sources every time would be too stressful
and therefore are done on a regular schedule with the reports then being made
available on the web.

This reposistory contains the code for the charm, the applications are coming
from various sources depending on the respective service.

## List of covered services

* update-sync-blocklist
  * TL;DR: git checkout and serve as html avoiding pressure on the cgit frontend
  * Timing: every 5 minutes
  * Execution time: <30 seconds
  * Code: https://git.launchpad.net/ubuntu-archive-scripts/tree/update-sync-blocklist
  * Data: Maintained in git at https://git.launchpad.net/~ubuntu-archive/+git/sync-blocklist/tree/sync-blocklist.txt
  * Old location: https://ubuntu-archive-team.ubuntu.com/sync-blocklist.txt
  * New location: https://static-reports.ubuntu.com/sync-blocklist.txt

* update-seeds
  * TL;DR: conversion of git branches about the seeds into directories, avoids pressure on the git servers
  * Timing: every 5 minutes
  * Execution time: ~13 minutes initially, 1 minute on updates
  * Code: https://git.launchpad.net/ubuntu-archive-scripts/tree/update-seeds
  * Data: Maintained in git at `https://git.launchpad.net/~$team/ubuntu-seeds/+git/${dist%.*}`
  * Old location: https://ubuntu-archive-team.ubuntu.com/seeds/
  * New location: https://static-reports.ubuntu.com/seeds/

* package-subscribers
  * TL;DR: convert LP API information about package subscribers to json for faster consumption by other tools
  * Timing: twice an hour
  * Execution time: ~3 min
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/package-subscribers
  * Data: Structural subscriptions in Launchpad of registered teams to source packages
  * Old location: https://ubuntu-archive-team.ubuntu.com/package-team-mapping.json
  * New location: https://static-reports.ubuntu.com/package-team-mapping.json

* permissions-report
  * TL;DR: Convert LP API data into a report about package upload ACLs
  * Timing: every 6 hours
  * Execution time: ~30 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/permissions-report 
  * Data: Per Package ACLs stored in Launchpad
  * Old location: https://ubuntu-archive-team.ubuntu.com/archive-permissions/
  * New location: https://static-reports.ubuntu.com/archive-permissions/

* packageset-report
  * TL;DR: Convert LP API data into a report about package sets as used for upload permissions
  * Timing: every 6 hours
  * Execution time: ~30 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/permissions-report 
  * Data: Package Set information stored in Launchpad
  * Old location: https://ubuntu-archive-team.ubuntu.com/packagesets/
  * New location: https://static-reports.ubuntu.com/packagesets/

* update-bugpatterns
  * TL;DR: git checkout and serve as XML avoiding pressure on the cgit frontend
  * Timing: every hour
  * Execution time: <30 seconds
  * Code: included in this charm
  * Data: Maintained in git at https://git.launchpad.net/~ubuntu-bugcontrol/apport/+git/ubuntu-bugpatterns
  * Old location: https://ubuntu-archive-team.ubuntu.com/bugpatterns/bugpatterns.xml
  * New location: https://static-reports.ubuntu.com/bugpatterns/bugpatterns.xml

* sru-report
  * TL;DR: generate the Pending Ubuntu SRU report
  * Timing: every 30 minutes
  * Execution time: up to several hours (8h timeout)
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/sru-report
  * Data: Pending SRUs in the `-proposed` pockets for all stable releases of Ubuntu
  * Old location: https://ubuntu-archive-team.ubuntu.com/pending-sru.html
  * New location: https://static-reports.ubuntu.com/pending-sru/pending-sru.html

* update-archive-mirror
  * TL;DR: Build and atomically publish one consistent archive-index snapshot that other services (update-germinate, NBS) share as a single source of truth
  * Timing: every 30 minutes, but a new snapshot is only built and swapped in when the archive indices actually changed
  * Execution time: seconds when nothing changed; under a minute for a full rsync
  * Code: `update-archive-mirror`
  * Data: A local rsync mirror of the archive indices (the `dists` tree) from the archive (configurable via `rsync_archive_source`)
  * Re-used internally by update-germinate (and potentially NBS); not exposed directly via nginx

* update-germinate
  * TL;DR: Germinate the current archive-mirror snapshot against the published seeds, publishing the combined archive+germinate result for other services to consume
  * Timing: after the archive mirror was updated
  * Execution time: a few minutes when the snapshot changed, seconds otherwise
  * Code: wrapper `update-germinate` is included in this charm; the germinate tool itself comes from https://git.launchpad.net/ubuntu-archive-tools
  * Data: Current archive indices (from update-archive-mirror) and seeds (from update-seeds)
  * Old location: https://ubuntu-archive-team.ubuntu.com/germinate-output/
  * New location: https://static-reports.ubuntu.com/germinate/

* update-mismatches
  * TL;DR: Generate the archive override mismatch reports (architecture, component, pocket, priority)
  * Timing: after germinate completed
  * Execution time: a few minutes when the snapshot changed, seconds otherwise
  * Code: wrapper `update-mismatches` is included in this charm; the mismatch reports themselves come from https://git.launchpad.net/ubuntu-archive-tools
  * Data: Current germinate snapshot (which already includes the archive indices)
  * Old location: https://ubuntu-archive-team.ubuntu.com/ \*-mismatches.\*
  * New location: https://static-reports.ubuntu.com/mismatches/

* update-nbs
  * TL;DR: Generate the NBS ("Not Built from Source") report of binary packages still published in the archive whose source no longer builds them
  * Timing: after the archive mirror was updated (in parallel with update-germinate)
  * Execution time: a few minutes when the snapshot changed, seconds otherwise
  * Code: wrapper `update-nbs` is included in this charm; the underlying tools (archive-cruft-check, checkrdepends, nbs-report) come from ubuntu-archive-tools
  * Data: Current archive indices (from update-archive-mirror)
  * Old location: https://ubuntu-archive-team.ubuntu.com/nbs.html
  * New location: https://static-reports.ubuntu.com/nbs/nbs.html

## Basic usage

Assuming you have access to a bootstrapped [Juju](https://juju.is) controller, you can deploy the charm with (For details on local manual debug see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
❯ juju deploy ubuntu-static-reports
```

Once the charm is deployed, you can check the status with Juju status:

```bash
❯ $ juju status
Model        Controller  Cloud/Region         Version  SLA          Timestamp
welcome-lxd  lxd         localhost/localhost  3.6.7    unsupported  13:29:50+02:00

App       Version  Status  Scale  Charm             Channel  Rev  Exposed  Message
ubuntu-static-reports           active      1  ubuntu-static-reports             0  no

Unit          Workload  Agent  Machine  Public address  Ports  Message
ubuntu-static-reports/0*  active    idle    1       10.142.46.109

Machine  State    Address        Inst id         Base          AZ  Message
1        started  10.142.46.109  juju-fd4fe1-1   ubuntu@24.04      Running
```

On first start up, the charm will install the application and install a systemd timer unit to trigger tracker updates on a regular basis.

To refresh the report, you can use the provided Juju [Action](https://documentation.ubuntu.com/juju/3.6/howto/manage-actions/):

```bash
❯ juju run ubuntu-static-reports/0 refresh"
```

## Checking a live worker node

To analyze the status of all reports - when they ran or when they will run
next, the log output of each and more - use systemctl as the are all systemd
timers and services.

```bash
❯ systemctl list-timers --all update-bugpatterns update-seeds update-sync-blocklist packageset-report package-subscribers permissions-report update-mismatches update-germinate update-archive-mirror
```

Since the report execution is wrapped into systemd services, one can also use
`systemctl status` as well as `journalctl -eu` to assess state and debug if
neccessary.

```bash
❯ journalctl -eu update-sync-blocklist
```

```bash
❯ systemctl status update-sync-blocklist.service
```

## Testing

For information on running tests and development workflows, see the [contribution guide](CONTRIBUTING.md).

## Contribute to Ubuntu Static Reports Operator

Ubuntu Static Reports Operator is open source and part of the Canonical family. We would love your help.

If you're interested, start with the [contribution guide](CONTRIBUTING.md).

## License and copyright

Ubuntu Static Reports Operator is released under the [GPL-3.0 license](LICENSE).
