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

* package-subscribers
  * TL;DR: convert LP API information about package subscribers to json for faster consumption by other tools
  * Timing: hourly
  * Execution time: ~3 min
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/package-subscribers
  * Data: Structural subscriptions in Launchpad of registered teams to source packages
  * Presented at: https://ubuntu-archive-team.ubuntu.com/package-team-mapping.json

* update-seeds
  * TL;DR: conversion of git branches about the seeds into directories, to avoid pressure on the git servers
  * Timing: every 5 minutes
  * Execution time: ~8 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-scripts/tree/update-seeds
  * Data: Maintained in git at `https://git.launchpad.net/~$team/ubuntu-seeds/+git/${dist%.*}`
  * Presented at https://ubuntu-archive-team.ubuntu.com/seeds/

* permissions-report
  * TL;DR: Convert LP API data into a report about package upload ACLs
  * Timing: daily
  * Execution time: ~30 minutes
  * Code: https://git.launchpad.net/ubuntu-archive-tools/tree/permissions-report 
  * Data: Per Package ACLs stored in Launchpad
  * Presented at https://ubuntu-archive-team.ubuntu.com/archive-permissions/

* packageset-report
  * TL;DR: Convert LP API data into a report about package sets as used for upload permissions
  * Timing: daily
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
