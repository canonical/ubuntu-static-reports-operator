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

Integration tests run inside the workshop staging VM (see [Workshop staging environment](#workshop-staging-environment) below) against a temporary Juju model:

```bash
❯ make integration     # or: workshop run staging -- integration
```

The harness bootstraps Juju if needed, removes leftover state first (the staging app and any leaked test models) so runs do not accumulate it, packs the charm, and runs the pytest suite in a fresh temp model that is destroyed on exit. To focus on single tests during debugging:

```bash
❯ make integration ARGS="-k test_content_sync_blocklist"
```

`make integration-execution` runs the same pytest suite directly against any controller you already have; it packs the charm itself when no `--charm-path` is given and reads `LPUSER_OAUTH_FILE` for the secret-dependent tests.

## Build charm

Build the charm in this git repository using:

```bash
charmcraft pack
```

## Workshop staging environment

For debugging the charm in a realistic Juju deployment there is a Workshop
staging VM. It packs the charm, runs nested LXD/Juju inside the VM and
deploys the unit with real systemd services to poke at.

```bash
workshop launch staging        # ~26.04 VM with all tools (takes a while)
workshop run staging -- lint   # make lint inside the VM
workshop run staging -- unit   # make unit inside the VM
workshop run staging -- deploy # pack + juju deploy (see below)
workshop shell staging         # interactive shell in the VM
workshop run staging -- ssh    # straight into the charm unit
```

### Prerequisites (host, once)

- A KVM-capable host with the [Workshop snap](https://documentation.ubuntu.com/canonical-workshop/latest/).
- VM workshops are experimental and must be enabled:

  ```bash
  sudo snap set workshop workshop.experimental-vms=1
  sudo snap restart workshop.workshopd
  ```

- The repo must be a **regular clone**, not a linked worktree: the charm
  build runs `git describe` inside the VM, and worktrees confuse it.
- Store the Launchpad OAuth token in the host keyring (Workshop reads it
  from there, it never enters the repo or the CLI history). `secret-tool
  store` reads the secret value from **stdin**, so redirect the OAuth file
  into it - the password prompt is gnome-keyring asking to unlock/create
  your login keyring, not the secret itself:

  ```bash
  secret-tool store --label='LP OAuth' \
    service launchpad account lp-ubuntu-archive-unprivileged-bot \
    < /path/to/lp-ubuntu-archive-unprivileged-bot.oauth
  ```

  Verify from the same (unlocked) session, it must print the file content:

  ```bash
  secret-tool lookup service launchpad account lp-ubuntu-archive-unprivileged-bot
  ```

- Connect the secret after launch (and after each refresh, if `workshop
  connections staging` shows it disconnected):

  ```bash
  workshop connect staging/staging:lp-oauth staging/system:lp-oauth
  ```

  If it is not connected, `deploy` fails fast with these instructions.

### Resize the workshop VM (after every launch)

The staging VM mirrors the production machine shape (2 cores, 8G RAM) and
the report data needs more than the launch default (10G disk, 1G RAM) can
hold. VM size is fixed at launch and cannot be declared in `workshop.yaml`
yet, so from the host, once after `workshop launch staging` (and again
after `workshop refresh`, which recreates the VM) [Note: this being declared
is a feature that is expected to come to workshop, then we can simplify this]:

```bash
lxc project list | grep workshop   # -> your project, e.g. workshop.1474078681
P=workshop.1474078681              # substitute yours below
lxc --project "$P" list | grep ^staging   # -> VM name, e.g. staging-633d817d
lxc --project "$P" config device set <VM> root size=80GiB    # allowed while running
lxc --project "$P" stop <VM>                                   # memory is fixed at boot
lxc --project "$P" config set <VM> limits.memory=8GiB limits.cpu=2
lxc --project "$P" start <VM>
workshop exec staging -- bash -lc 'sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1'
```

Tip: presetting the memory and CPU for all your future workshop VMs avoids
the stop/start dance:
`lxc --project "$P" profile set default limits.memory=8GiB limits.cpu=2`
(the root disk size cannot be preset this way - workshop VMs shadow the
profile's root device, so it stays a per-launch `config device set`).

The root partition does not grow on its own - the final `workshop exec`
line grows it (`growpart`/`resize2fs`). The disk `size` may also be raised
later on a running VM (grow the partition again afterwards); the memory
limit can only be changed while the VM is stopped.

### Deploy

`workshop run staging -- deploy` is always a cold-capable path:

1. preflight free disk/memory in the VM,
2. ensure the production-shape 8G swap file (`/swapfile.swp`, same path
   and size as on the real unit) so report generation hits swap instead of
   the kernel OOM killer,
3. bootstrap Juju via concierge on first run,
4. pack the charm with charmcraft (nested LXD),
5. `juju deploy` with `--storage staticreports=${STORAGE_SIZE:-50G}`, or
   `juju refresh` if the application already exists,
6. create/update/grant the `lp-oauth` Juju secret from the host keyring and
   set `lpuser_secret_id`,
7. wait for the unit to settle and print `juju status --relations --storage`.

`workshop refresh` **discards the VM writable filesystem, including the
nested Juju controller and model** - just re-run `deploy` afterwards.
`redeploy` removes and re-deploys the application for a fully fresh unit.

### Debugging

```bash
workshop run staging -- status    # juju status --relations --storage
workshop run staging -- logs       # juju debug-log (args pass through)
workshop run staging -- services   # unit timers + failed services
workshop run staging -- ssh        # shell in the charm unit
workshop run staging -- destroy    # remove the application
```

VM workshops support no `tunnel` interface - for web UI access use an SSH
local forward from the host, e.g.
`ssh -L 8080:10.x.x.x:80 workshop@$(workshop info staging | awk '/hostname:/{print $2}')`
with the unit address from `juju status`.

The staging workshop intentionally replaces the old host-side `local-debug`
runner: it is a real deployment with real systemd services, at the cost of
being slower than host-side unit tests.

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

## Integration Testing

Integration tests run in the workshop staging VM:

```bash
make integration
```

This will:
1. Bootstrap Juju via concierge inside the VM if needed (e.g. after `workshop refresh`)
2. Remove the staging app and any leaked test models so runs do not accumulate state
3. Pack the charm and deploy it in a fresh temporary model
4. Run the pytest integration tests, destroying the model afterwards

If the VM has accumulated state that the automatic cleanup cannot reach, the
nuclear reset is `workshop refresh` (discards the whole VM writable
filesystem), then redo the resize steps above and re-run `make integration`.

### Testing with Secrets

Some services require Launchpad OAuth credentials:
- `permissions-report.service`
- `packageset-report.service`

The token is taken from the host keyring (see [Workshop staging environment](#workshop-staging-environment) above, the secret is shared with the `deploy` flow) and handed to the tests as `LPUSER_OAUTH_FILE`.

Tests requiring secrets will automatically skip if the token is unavailable.
