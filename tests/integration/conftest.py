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
    """Create the Launchpad OAuth secret if LPUSER_OAUTH_FILE is set.

    Granting to the application happens after the deploy tests created it
    (see configure_lp_secret) - Juju cannot grant a secret to an
    application that does not exist yet.
    """
    oauth_file = os.environ.get("LPUSER_OAUTH_FILE")
    if not oauth_file or not Path(oauth_file).is_file():
        return None

    with open(oauth_file) as f:
        oauth_content = f.read()

    return juju.add_secret(
        name="lpuser-oauth",
        content={"lpoauthkey": oauth_content},
    )


@fixture(scope="module")
def ubuntu_static_reports_charm(request):
    """Build or return path to ubuntu-static-reports charm file."""
    charm_file = request.config.getoption("--charm-path")
    if not charm_file:
        subprocess.run(
            ["charmcraft", "pack", "--verbose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

        charm_file = next(Path.glob(Path("."), "*.charm")).absolute()

    return charm_file
