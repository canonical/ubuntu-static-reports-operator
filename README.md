# Ubuntu Transition Tracker Operator


**Ubuntu  Transition Tracker Operator** is a [charm](https://juju.is/charms-architecture) for deploying an Ubuntu transition tracker environment.

This reposistory contains the code for the charm, the application is coming from the `ben` package and the [configs repository](https://git.launchpad.net/~ubuntu-transition-trackers/ubuntu-transition-tracker/+git/configs).

## Basic usage

Assuming you have access to a bootstrapped [Juju](https://juju.is) controller, you can deploy the charm with:

```bash
❯ juju deploy ubuntu-transition-tracker
```

Once the charm is deployed, you can check the status with Juju status:

```bash
❯ $ juju status
Model        Controller  Cloud/Region         Version  SLA          Timestamp
welcome-lxd  lxd         localhost/localhost  3.6.7    unsupported  13:29:50+02:00

App       Version  Status  Scale  Charm             Channel  Rev  Exposed  Message
transition-tracker           active      1  ubuntu-transition-tracker             0  no

Unit          Workload  Agent  Machine  Public address  Ports  Message
transition-tracker/0*  active    idle    1       10.142.46.109

Machine  State    Address        Inst id         Base          AZ  Message
1        started  10.142.46.109  juju-fd4fe1-1   ubuntu@24.04      Running
```

On first start up, the charm will install the application and install a systemd timer unit to trigger tracker updates on a regular basis.

To refresh the report, you can use the provided Juju [Action](https://documentation.ubuntu.com/juju/3.6/howto/manage-actions/):

```bash
❯ juju run ubuntu-transition-tracker/0 refresh"
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
lxd:ubuntu-24.04:tests/spread/unit/transition-tracker
❯ charmcraft.spread -v -debug -reuse lxd:ubuntu-24.04:tests/spread/integration/deploy-charm:juju_3_6
```

## Contribute to Ubuntu Transition Tracker Operator

Ubuntu Transition Tracker Operator is open source and part of the Canonical family. We would love your help.

If you're interested, start with the [contribution guide](CONTRIBUTING.md).

## License and copyright

Ubuntu Transition Tracker Operator is released under the [GPL-3.0 license](LICENSE).
