# Copyright 2025 Canonical
# See LICENSE file for licensing details.

import os
import subprocess
from pathlib import Path

import jubilant
from pytest import fixture


@fixture(scope="module")
def juju():
    with jubilant.temp_model() as juju:
        yield juju


@fixture(scope="module")
def lpuser_secret(juju):
    """Create and grant Launchpad OAuth secret if LPUSER_OAUTH_FILE is set."""
    oauth_file = os.environ.get("LPUSER_OAUTH_FILE")
    if not oauth_file or not Path(oauth_file).exists():
        return None

    with open(oauth_file) as f:
        oauth_content = f.read()

    secret_uri = juju.add_secret(
        name="lpuser-oauth",
        content={"lpoauthkey": oauth_content},
    )

    juju.grant_secret(secret_uri, "ubuntu-static-reports")

    return secret_uri


@fixture(scope="module")
def ubuntu_static_reports_charm(request, juju, lpuser_secret):
    """Deploy ubuntu-static-reports charm with optional secret configuration."""
    charm_file = request.config.getoption("--charm-path")
    if not charm_file:
        working_dir = os.getenv("SPREAD_PATH", Path("."))

        subprocess.run(
            ["/snap/bin/charmcraft", "pack", "--verbose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=working_dir,
            check=True,
        )

        charm_file = next(Path.glob(Path(working_dir), "*.charm")).absolute()

    juju.deploy(charm_file, app="ubuntu-static-reports")

    if lpuser_secret:
        juju.config("ubuntu-static-reports", {"lpuser_secret_id": lpuser_secret})

    return charm_file
