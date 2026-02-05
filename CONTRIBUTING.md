# Contributing

This documents explains the processes and practices recommended for contributing enhancements to
this operator.

- Generally, before developing enhancements to this charm, you should consider [opening an issue
  ](https://github.com/canonical/ubuntu-static-reports-operator/issues) explaining your use case.
- If you would like to chat with us about your use-cases or proposed implementation, you can reach
  us [on Matrix](https://ubuntu.com/community/communications/matrix) or [Discourse](https://discourse.charmhub.io/).
- Familiarising yourself with the [Operator Framework](https://ops.readthedocs.io/en/latest/) library
  will help you a lot when working on new features or bug fixes.
- All enhancements require review before being merged. Code review typically examines
  - code quality
  - test coverage
  - user experience for Juju administrators this charm.
- Please help us out in ensuring easy to review branches by rebasing your pull request branch onto
  the `main` branch. This also avoids merge commits and creates a linear Git commit history.

## Developing

This project uses [`uv`](https://github.com/astral-sh/uv) for managing dependencies and virtual
environments.

You can create a virtual environment manually should you wish, though most of that is taken
care of automatically if you use the `Makefile` provided:

```bash
❯ make format        # update your code according to linting rules
❯ make lint          # code style
❯ make unit          # run unit tests
❯ make integration   # run integration tests
```

To create the environment manually:

```bash
❯ uv venv
❯ source .venv/bin/activate
❯ uv sync --all-extras
```

## Running tests

### Unit tests

Unit tests can be run locally with no additional tools by running `make unit`. All of the project's unit tests are designed to run agnostic of machine and network, and shouldn't require any additional dependencies other than those injected by `uv run` and the `Make` target.

### Integration tests

Integration tests can be run directly with `make integration`, but this requires a rather invasive juju setup and will create and destroy units. This can be useful to run in an already established virtual environment or in CI.

```bash
❯ make integration
```

### Spread tests

If instead integration tests shall be run with isolation, [Spread](https://github.com/canonical/spread/blob/master/README.md) is configured to create the necessary environment, setup the components needed, and then run the integration tests in there.

```bash
❯ charmcraft.spread -v -debug -reuse
```

For development and debugging it is recommended to select an individual test from the list of tests, and run it with [`-reuse` for faster setup](https://github.com/canonical/spread/blob/master/README.md#reuse) and [`-debug`](https://github.com/canonical/spread/blob/master/README.md#reuse) to drop into a shell after an error.

```bash
❯ charmcraft.spread -list
lxd:ubuntu-24.04:tests/spread/integration/deploy-charm:juju_3_6
lxd:ubuntu-24.04:tests/spread/integration/ingress:juju_3_6
lxd:ubuntu-24.04:tests/spread/unit/ubuntu-static-reports
❯ charmcraft.spread -v -debug -reuse lxd:ubuntu-24.04:tests/spread/integration/deploy-charm:juju_3_6
```

## Build charm

Build the charm in this git repository using:

```bash
charmcraft pack
```

### Deploy and Debug

```bash
# Create a model
❯ juju add-model dev

# Enable DEBUG logging
❯ juju model-config logging-config="<root>=INFO;unit=DEBUG"
# To then permanently trace in another shell consider running
❯ juju debug-log --replay --level DEBUG

# Deploy for local testing with secrets
❯ juju add-secret lpuser_secret_id lpoauthkey#file=/root/lp-ubuntu-archive-unprivileged-bot.oauth
# This will return something like
secret:<someuuid>
# now deploy
❯ juju deploy -- ./ubuntu-static-reports_amd64.charm ubuntu-static-reports
# Then allow access
❯ juju grant-secret lpuser_secret_id ubuntu-static-reports
# glue the defined secret and the deployed charm together via config
❯ juju config ubuntu-static-reports lpuser_secret_id=secret:<someuuid>

# To blast it away no matter the open half debugged state
❯ juju remove-application --no-prompt --force --no-wait ubuntu-static-reports
```
