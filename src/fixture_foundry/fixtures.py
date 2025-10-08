import os
import time
import json
import logging
from contextlib import contextmanager
from typing import Dict, Generator, Optional
from pathlib import Path

import docker
import pytest
import requests
from pulumi import automation as auto  # Pulumi Automation API

from docker.errors import DockerException
from docker.types import Mount

from .context import container_network_context
from .context import localstack_context
from .context import postgres_context
from .utils import to_localstack_url

log = logging.getLogger(__name__)
DEFAULT_REGION = os.environ.get(
    "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
)


@pytest.fixture(scope="session")
def container_network(request: pytest.FixtureRequest) -> Generator[str, None, None]: #noqa: C901
    """
    Ensure a user-defined Docker bridge network exists for cross-container traffic
    (e.g., LocalStack Lambdas connecting to Postgres). Yields the network name.

    Environment:
      DOCKER_TEST_NETWORK: Override the network name (default: "ls-dev").

    Teardown:
      If the fixture created the network and --teardown=true, the network is
      removed at session end.
    """

    network_name = os.environ.get("DOCKER_TEST_NETWORK", "ls-dev")
    with container_network_context(network_name, teardown=True) as net:
        yield net


# Helper for boolean CLI options
def _get_bool_option(
    request: pytest.FixtureRequest, name: str, default: bool = True
) -> bool:
    """
    Read a boolean CLI option added via pytest_addoption.

    Accepted truthy values (case-insensitive): 1, true, yes, y
    Accepted falsy  values (case-insensitive): 0, false, no, n

    Falls back to 'default' if the option is missing or not parseable.
    """
    opt = f"--{name}"
    try:
        raw = request.config.getoption(opt)
    except (AttributeError, ValueError):
        return default
    if raw is None:
        return default
    return str(raw).lower() in ("1", "true", "yes", "y")


@pytest.fixture(scope="session")
def postgres(
    request: pytest.FixtureRequest, container_network
) -> Generator[dict, None, None]:
    """
    Start a PostgreSQL container and yield connection information for tests.

    Ports:
      - Inside Docker network: {container_name}:5432 (for other containers, e.g., Lambda)
      - From host (pytest process): localhost:{host_port} (random mapped port)

    Yields:
      Dict with:
        container_name : Docker name (reachable by other containers on container_network)
        container_port : 5432
        username       : Database user
        password       : Database password
        database       : Database name (from --database)
        host_port      : Host-mapped TCP port for 5432
        dsn            : postgresql://user:pass@localhost:{host_port}/{database}

    Teardown:
      Stops and removes the container when the session ends.
    """
    username = request.config.getoption("--database-username", "testuser")
    password = request.config.getoption("--database-password", "testpass")
    database = request.config.getoption("--database", "testdb")
    image = request.config.getoption("--database-image", "postgres:latest")

    with postgres_context(username, password, database, image, container_network) as pg:
        yield pg


def _wait_for_localstack(endpoint: str, timeout: int = 90) -> None:
    """
    Poll LocalStack health endpoints until ready or timeout.

    Tries both /_localstack/health (newer) and /health (legacy) and considers
    LocalStack ready when:
      - JSON includes initialized=true, or
      - a services map is present, or
      - a 200 OK is returned with parseable/empty body.

    Raises:
      RuntimeError if the timeout elapses without a healthy response.
    """
    url_candidates = [
        f"{endpoint}/_localstack/health",  # modern health endpoint
        f"{endpoint}/health",  # legacy fallback
    ]

    start = time.time()
    last_err: Optional[str] = None
    while time.time() - start < timeout:
        for url in url_candidates:
            try:
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except json.JSONDecodeError:
                        data = {}
                    # Heuristics: consider healthy if initialized true or services reported
                    if isinstance(data, dict):
                        if data.get("initialized") is True:
                            return
                        if "services" in data:
                            # services dict often present when up
                            return
                    else:
                        return
            except requests.RequestException as e:  # noqa: PERF203 - simple polling loop
                last_err = str(e)
                time.sleep(0.5)
                continue
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for LocalStack at {endpoint} (last_err={last_err})"
    )

@pytest.fixture(scope="session")
def localstack(
    request: pytest.FixtureRequest, container_network
) -> Generator[Dict[str, str], None, None]:
    """
    Run a LocalStack container for the test session and yield connection details.

    Configuration (pytest options):
      --localstack-image     : Image tag (default: localstack/localstack:latest)
      --localstack-services  : Comma-separated services to enable
      --localstack-timeout   : Health check timeout (seconds)
      --localstack-port      : Host edge port (0 = random)
      --teardown             : Stop/remove container at session end (default true)

    Behavior:
      - Joins the shared Docker network (container_network).
      - Mounts /var/run/docker.sock so LocalStack can run Lambda containers.
      - Sets LAMBDA_DOCKER_NETWORK so Lambda containers can reach Postgres by
        container name.
      - Exposes only the edge port (4566).

    Yields:
      Dict with:
        endpoint_url : e.g., http://localhost:4566
        region       : AWS region in use
        container_id : LocalStack container id
        services     : Comma list of configured services
        port         : Host port for the edge endpoint (as string)
    """
    teardown: bool = _get_bool_option(request, "--teardown", default=True)
    port: int = int(request.config.getoption("--localstack-port", "0"))
    image: str = request.config.getoption("--localstack-image", "localstack/localstack:latest")
    services: str = request.config.getoption("--localstack-services", "s3,lambda")
    timeout: int = int(request.config.getoption("--localstack-timeout", "90"))

    with localstack_context(image, services, port, timeout, teardown, container_network) as ls:
        yield ls

