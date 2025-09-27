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

log = logging.getLogger(__name__)
DEFAULT_REGION = os.environ.get(
    "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
)


@contextmanager
def deploy(
    project_name: str,
    stack_name: str,
    pulumi_program,
    config: Dict[str, auto.ConfigValue] | None = None,
    localstack: dict | None = None,
    teardown: bool = True,
) -> Generator[Dict[str, str], None, None]:
    """
    Deploy a Pulumi program and yield only the stack outputs (as a plain dict).

    Behavior:
    - If localstack is provided, injects AWS provider config (region, test creds,
      service endpoints, and "skip*" flags) so the program targets LocalStack
      instead of real AWS.
    - Best-effort pre-clean: attempts destroy before update to ensure a fresh run.
    - Runs refresh, then up; yields outputs as {name: value}.
    - On context exit, destroys the stack and removes it from the workspace when
      teardown=True.

    Parameters:
      project_name: Pulumi project name for the Automation API Stack.
      stack_name  : Logical stack identifier (e.g., "test", "ci-123").
      pulumi_program: A zero-arg function that defines the Pulumi resources.
      config      : Optional explicit config map to set on the stack.
      localstack  : Optional dict from the localstack fixture with keys:
                    endpoint_url, region, services.
      teardown    : Whether to destroy/remove the stack on exit.

    Yields:
      Dict[str, str]: Exported stack outputs with raw values.
    """
    stack = auto.create_or_select_stack(
        stack_name=stack_name, project_name=project_name, program=pulumi_program
    )

    try:
        # Best effort pre-clean
        try:
            stack.destroy(on_output=lambda _: None)
        except Exception:
            pass

        if config is None and localstack:
            services_map = [
                {
                    svc: localstack["endpoint_url"]
                    for svc in localstack["services"].split(",")
                }
            ]
            config = {
                "aws:region": auto.ConfigValue(localstack["region"]),
                "aws:accessKey": auto.ConfigValue("test"),
                "aws:secretKey": auto.ConfigValue("test"),
                "aws:endpoints": auto.ConfigValue(json.dumps(services_map)),
                "aws:skipCredentialsValidation": auto.ConfigValue("true"),
                "aws:skipRegionValidation": auto.ConfigValue("true"),
                "aws:skipRequestingAccountId": auto.ConfigValue("true"),
                "aws:skipMetadataApiCheck": auto.ConfigValue("true"),
                "aws:insecure": auto.ConfigValue("true"),
                "aws:s3UsePathStyle": auto.ConfigValue("true"),
            }

        if config:
            stack.set_all_config(config)

        try:
            stack.refresh(on_output=lambda _: None)
        except Exception:
            pass

        up_result = stack.up(on_output=lambda _: None)
        outputs = {k: v.value for k, v in up_result.outputs.items()}

        yield outputs
    finally:
        if teardown:
            try:
                stack.destroy(on_output=lambda _: None)
            except Exception:
                pass
            try:
                stack.workspace.remove_stack(stack_name)
            except Exception:
                pass


@pytest.fixture(scope="session")
def test_network(request: pytest.FixtureRequest) -> Generator[str, None, None]:
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
    client = docker.from_env()

    net = None
    for n in client.networks.list(names=[network_name]):
        if n.name == network_name:
            net = n
            break

    created = False
    if net is None:
        net = client.networks.create(network_name, driver="bridge")
        created = True

    try:
        yield network_name
    finally:
        # Remove only if we created it and teardown is enabled
        teardown = _get_bool_option(request, "teardown", default=True)
        if created and teardown:
            try:
                net.remove()
            except Exception:
                pass


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


def exec_sql_file(conn, sql_path: Path):
    """
    Execute a SQL script file against an open DB-API connection.

    Notes:
    - Reads the entire file and executes it in a single cursor.execute call.
    - Supports PostgreSQL DO $$ ... $$ blocks and multi-statement scripts.
    - Caller is responsible for transaction handling (e.g., conn.autocommit = True).

    Parameters:
      conn    : psycopg2 connection (or DB-API compatible).
      sql_path: Path to the .sql file to execute.
    """
    sql_text = sql_path.read_text(encoding="utf-8")
    # Execute entire script (supports DO $$ ... $$ blocks and multiple statements)
    with conn.cursor() as cur:
        cur.execute(sql_text)


@pytest.fixture(scope="session")
def postgres(
    request: pytest.FixtureRequest, test_network
) -> Generator[dict, None, None]:
    """
    Start a PostgreSQL container and yield connection information for tests.

    Ports:
      - Inside Docker network: {container_name}:5432 (for other containers, e.g., Lambda)
      - From host (pytest process): localhost:{host_port} (random mapped port)

    Yields:
      Dict with:
        container_name : Docker name (reachable by other containers on test_network)
        container_port : 5432
        username       : Database user
        password       : Database password
        database       : Database name (from --database)
        host_port      : Host-mapped TCP port for 5432
        dsn            : postgresql://user:pass@localhost:{host_port}/{database}

    Teardown:
      Stops and removes the container when the session ends.
    """
    import psycopg2

    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:
        assert False, f"Docker not available: {e}"

    username = "test_user"
    password = "test_password"
    database = request.config.getoption("--database")
    image = request.config.getoption("--database-image")

    container = client.containers.run(
        image,
        environment={
            "POSTGRES_USER": username,
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": database,
        },
        ports={"5432/tcp": 0},  # random host port
        detach=True,
        network=test_network,
    )

    try:
        # Resolve mapped port
        host = container.name
        host_port = None
        deadline = time.time() + 60
        while time.time() < deadline:
            container.reload()
            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            mapping = ports.get("5432/tcp")
            if mapping and mapping[0].get("HostPort"):
                host_port = int(mapping[0]["HostPort"])
                break
            time.sleep(0.25)

        if not host_port:
            raise RuntimeError("Failed to map Postgres port")

        # Wait for readiness
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                conn = psycopg2.connect(
                    dbname=database,
                    user=username,
                    password=password,
                    host=host,
                    port=host_port,
                )
                conn.close()
                break
            except Exception:
                time.sleep(0.5)

        yield {
            "container_name": host,
            "container_port": 5432,
            "username": username,
            "password": password,
            "database": database,
            "host_port": host_port,
            "dsn": f"postgresql://{username}:{password}@localhost:{host_port}/{database}",
        }
    finally:
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            container.remove(v=True, force=True)
        except Exception:
            pass


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
                    except Exception:
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
            except Exception as e:  # noqa: PERF203 - simple polling loop
                last_err = str(e)
                time.sleep(0.5)
                continue
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for LocalStack at {endpoint} (last_err={last_err})"
    )


@pytest.fixture(scope="session")
def localstack(
    request: pytest.FixtureRequest, test_network
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
      - Joins the shared Docker network (test_network).
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
    port: int = int(request.config.getoption("--localstack-port"))
    image: str = request.config.getoption("--localstack-image")
    services: str = request.config.getoption("--localstack-services")
    timeout: int = int(request.config.getoption("--localstack-timeout"))

    if docker is None:
        assert False, "Docker SDK not available: skipping LocalStack-dependent tests"

    try:
        client = docker.from_env()
    except DockerException:
        assert False, "Docker daemon not available: skipping LocalStack-dependent tests"

    # Pull image to ensure availability
    try:
        client.images.pull(image)
    except Exception:
        # If pull fails, we may already have it locally — proceed
        pass

    # Publish only the edge port; service port range is not needed with edge
    ports = {
        "4566/tcp": port,
    }
    env = {
        "SERVICES": services,
        "LS_LOG": "warn",
        "AWS_DEFAULT_REGION": DEFAULT_REGION,
        "LAMBDA_DOCKER_NETWORK": test_network,  # ensure Lambda containers join this network
        "DISABLE_CORS_CHECKS": "1",
    }
    # Mount Docker socket for LocalStack to access Docker if needed
    volume_dir = os.environ.get("LOCALSTACK_VOLUME_DIR", "./volume")
    Path(volume_dir).mkdir(parents=True, exist_ok=True)
    mounts = [
        Mount(
            target="/var/run/docker.sock",
            source="/var/run/docker.sock",
            type="bind",
            read_only=False,
        ),
        Mount(
            target="/var/lib/localstack",
            source=os.path.abspath(volume_dir),
            type="bind",
            read_only=False,
        ),
    ]
    container = client.containers.run(
        image,
        detach=True,
        environment=env,
        ports=ports,
        name=None,
        tty=False,
        mounts=mounts,
        network=test_network,
    )

    if port == 0:
        # Resolve host port assigned for edge, with retries to avoid race condition
        host_port = None
        max_attempts = 10
        for attempt in range(max_attempts):
            container.reload()
            try:
                port_info = container.attrs["NetworkSettings"]["Ports"]["4566/tcp"]
                if port_info and port_info[0] and port_info[0].get("HostPort"):
                    host_port = int(port_info[0]["HostPort"])  # type: ignore[arg-type]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if host_port is None:
            # Clean up if mapping not available
            try:
                container.stop(timeout=5)
            finally:
                raise RuntimeError(
                    "Failed to determine LocalStack edge port after retries"
                )
    else:
        host_port = port

    endpoint = f"http://localhost:{host_port}"

    # Set common AWS envs for child code that relies on defaults
    os.environ.setdefault("AWS_REGION", DEFAULT_REGION)
    os.environ.setdefault("AWS_DEFAULT_REGION", DEFAULT_REGION)
    os.environ.setdefault(
        "AWS_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "test")
    )
    os.environ.setdefault(
        "AWS_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
    )

    # Wait for the health endpoint to be ready
    _wait_for_localstack(endpoint, timeout=timeout)

    try:
        yield {
            "endpoint_url": endpoint,
            "region": DEFAULT_REGION,
            "container_id": str(container.id),
            "services": services,
            "port": str(host_port),
        }
    finally:
        if teardown:
            # Stop container if still running
            try:
                container.stop(timeout=5)
            except Exception:
                pass
            try:
                container.remove(v=True, force=True)
            except Exception:
                pass


def to_localstack_url(api_url: str, edge_port: int = 4566, scheme: str = "http") -> str:
    """
    Convert an AWS API Gateway invoke URL into the equivalent LocalStack edge URL.

    Accepts:
      - Full URLs: https://{id}.execute-api.{region}.amazonaws.com/{stage}/path?query
      - Bare host/path: {id}.execute-api.{region}.amazonaws.com/{stage}/path
      - Already-converted LocalStack hostnames are normalized and returned.

    Returns:
      URL targeting {id}.execute-api.localhost.localstack.cloud:{edge_port} with the
      same path, query, and fragment, using the provided scheme (default http).

    Raises:
      ValueError if the hostname does not match an API Gateway pattern or if the
      stage segment is missing from the path.
    """
    import re
    from urllib.parse import urlparse, urlunparse

    if not re.match(r"^[a-z]+://", api_url):
        # prepend dummy scheme so urlparse works uniformly
        api_url = f"https://{api_url}"

    parsed = urlparse(api_url)

    # If already a LocalStack style host, normalize (ensure port & scheme) and return
    ls_host_re = re.compile(
        r"^[a-z0-9]+\.execute-api\.localhost\.localstack\.cloud(?::\d+)?$",
        re.IGNORECASE,
    )
    if ls_host_re.match(parsed.netloc):
        # Inject / adjust port if different
        host_no_port = parsed.netloc.split(":")[0]
        netloc = f"{host_no_port}:{edge_port}"
        return urlunparse(
            (
                scheme,
                netloc,
                parsed.path or "/",
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    # Match standard AWS execute-api host
    aws_host_re = re.compile(
        r"^(?P<api_id>[a-z0-9]+)\.execute-api\.(?P<region>[-a-z0-9]+)\.amazonaws\.com$",
        re.IGNORECASE,
    )
    m = aws_host_re.match(parsed.netloc)
    if not m:
        raise ValueError(f"Unrecognized API Gateway hostname: {parsed.netloc}")

    api_id = m.group("api_id")
    path = parsed.path or "/"

    # Require a stage as first path segment
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise ValueError("Missing stage segment in API Gateway path")
    # Reconstruct path exactly as given (we don't strip or re-add trailing slash)
    new_host = f"{api_id}.execute-api.localhost.localstack.cloud:{edge_port}"

    return urlunparse(
        (scheme, new_host, path, parsed.params, parsed.query, parsed.fragment)
    )
