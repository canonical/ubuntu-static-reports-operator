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
  * Presented: at https://ubuntu-archive-team.ubuntu.com/sync-blocklist.txt

* update-seeds
  * TL;DR: conversion of git branches about the seeds into directories, avoids pressure on the git servers
  * Timing: every 5 minutes
  * Execution time: ~13 minutes initially, 1 minute on updates
  * Code: https://git.launchpad.net/ubuntu-archive-scripts/tree/update-seeds
  * Data: Maintained in git at `https://git.launchpad.net/~$team/ubuntu-seeds/+git/${dist%.*}`
  * Presented at https://ubuntu-archive-team.ubuntu.com/seeds/

* package-subscribers
  * TL;DR: convert LP API information about package subscribers to json for faster consumption by other tools
  * Timing: twice an hour
  * Execution time: ~3 min
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/package-subscribers
  * Data: Structural subscriptions in Launchpad of registered teams to source packages
  * Presented at: https://ubuntu-archive-team.ubuntu.com/package-team-mapping.json

* permissions-report
  * TL;DR: Convert LP API data into a report about package upload ACLs
  * Timing: every 6 hours
  * Execution time: ~30 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/permissions-report 
  * Data: Per Package ACLs stored in Launchpad
  * Presented at https://ubuntu-archive-team.ubuntu.com/archive-permissions/

* packageset-report
  * TL;DR: Convert LP API data into a report about package sets as used for upload permissions
  * Timing: every 6 hours
  * Execution time: ~30 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/permissions-report 
  * Data: Package Set information stored in Launchpad
  * Presented at https://ubuntu-archive-team.ubuntu.com/packagesets/

## Basic usage

Assuming you have access to a bootstrapped [Juju](https://juju.is) controller, you can deploy the charm with:

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
❯ systemctl list-timers --all update-seeds update-sync-blocklist packageset-report package-subscribers permissions-report
NEXT                        LEFT LAST                              PASSED UNIT                        ACTIVATES
Tue 2025-12-16 14:57:43 UTC   1s -                                      - package-subscribers.timer   package-subscribers.service
Tue 2025-12-16 14:57:45 UTC   3s Tue 2025-12-16 14:52:12 UTC     5min ago update-seeds.timer          update-seeds.service
Tue 2025-12-16 14:57:48 UTC   6s -                                      - packageset-report.timer     packageset-report.service
Tue 2025-12-16 14:57:56 UTC  13s Tue 2025-12-16 14:52:43 UTC 4min 58s ago update-sync-blocklist.timer update-sync-blocklist.service
-                              - Tue 2025-12-16 14:56:33 UTC  1min 8s ago permissions-report.timer    permissions-report.service
```

Since the report execution is wrapped into systemd services, one can also use
`systemctl status` as well as `journalctl -u` to assess state and debug if
neccessary.

```bash
❯ journalctl -u update-sync-blocklist
Dec 16 14:52:12 juju-c5cbb1-23 systemd[1]: Starting update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view...
Dec 16 14:52:12 juju-c5cbb1-23 update-sync-blocklist[6405]: Status: Initial clone
Dec 16 14:52:12 juju-c5cbb1-23 update-sync-blocklist[6423]: Cloning into '/home/ubuntu/sync-blocklist'...
Dec 16 14:52:13 juju-c5cbb1-23 update-sync-blocklist[6405]: Status: atomic exchange
Dec 16 14:52:13 juju-c5cbb1-23 systemd[1]: update-sync-blocklist.service: Deactivated successfully.
Dec 16 14:52:13 juju-c5cbb1-23 systemd[1]: Finished update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view.
Dec 16 14:52:43 juju-c5cbb1-23 systemd[1]: Starting update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view...
Dec 16 14:52:43 juju-c5cbb1-23 update-sync-blocklist[6569]: Status: In-place update from git
Dec 16 14:52:45 juju-c5cbb1-23 update-sync-blocklist[6569]: Status: atomic exchange
Dec 16 14:52:45 juju-c5cbb1-23 systemd[1]: update-sync-blocklist.service: Deactivated successfully.


❯ systemctl status update-sync-blocklist.service
○ update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view
     Loaded: loaded (/etc/systemd/system/update-sync-blocklist.service; static)
     Active: inactive (dead) since Tue 2025-12-16 14:57:57 UTC; 3min 45s ago
TriggeredBy: ● update-sync-blocklist.timer
    Process: 7536 ExecStart=update-sync-blocklist (code=exited, status=0/SUCCESS)
   Main PID: 7536 (code=exited, status=0/SUCCESS)
        CPU: 70ms

Dec 16 14:57:56 juju-c5cbb1-23 systemd[1]: Starting update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view...
Dec 16 14:57:56 juju-c5cbb1-23 update-sync-blocklist[7536]: Status: In-place update from git
Dec 16 14:57:57 juju-c5cbb1-23 update-sync-blocklist[7536]: Status: atomic exchange
Dec 16 14:57:57 juju-c5cbb1-23 systemd[1]: update-sync-blocklist.service: Deactivated successfully.
Dec 16 14:57:57 juju-c5cbb1-23 systemd[1]: Finished update-sync-blocklist.service - Ubuntu Static Reports - Sync Blocklist Web view.
```

## Testing

There are unit tests which can be run directly without influence to
the system and dependencies handled by uv.

```bash
❯ make unit
```

Furthermore there are integration tests. Those could be run directly,
but would the rather invasive juju setup and will via that create and
destroy units. This can be useful to run in an already established
virtual environment along CI.

```bash
❯ make integration
```

If instead integration tests shall be run, but with isolation.
[Spread](https://github.com/canonical/spread/blob/master/README.md)
is configured to create the necessary environment, setup the components needed
and then run the integration tests in there.

```bash
❯ charmcraft.spread -v -debug -reuse
```

For development and debugging it is recommended to select an individual test
from the list of tests, and run it with
[`-reuse` for faster setup](https://github.com/canonical/spread/blob/master/README.md#reuse)
and [`-debug`](https://github.com/canonical/spread/blob/master/README.md#reuse)
to drop into a shell after an error.

```bash
❯ charmcraft.spread -list
lxd:ubuntu-24.04:tests/spread/integration/deploy-charm:juju_3_6
lxd:ubuntu-24.04:tests/spread/integration/ingress:juju_3_6
lxd:ubuntu-24.04:tests/spread/unit/ubuntu-static-reports
❯ charmcraft.spread -v -debug -reuse lxd:ubuntu-24.04:tests/spread/integration/deploy-charm:juju_3_6
```

## Contribute to Ubuntu Static Reports Operator

Ubuntu Static Reports Operator is open source and part of the Canonical family. We would love your help.

If you're interested, start with the [contribution guide](CONTRIBUTING.md).

## License and copyright

Ubuntu Static Reports Operator is released under the [GPL-3.0 license](LICENSE).
