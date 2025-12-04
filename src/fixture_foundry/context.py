"""Fixture Foundry context utilities.

This module provides small context managers and helpers that make
ephemeral infrastructure easy to use in tests and dev scripts.

Features:
- deploy: Run a Pulumi program via the Automation API. Supports
    LocalStack by injecting AWS config. Yields a dict of stack outputs.
    Cleans up on exit when teardown is True.
- container_network_context: Ensure a Docker bridge network exists for
    the duration of a block. Optionally remove it on exit if created here.
- postgres_context: Start a disposable PostgreSQL container, wait for
    ready, and yield connection details (DSN, host port, credentials).
- localstack_context: Start LocalStack on a Docker network, wait for
    health, and yield endpoint and metadata. Optionally stop and remove
    on exit.
- exec_sql_file: Execute a .sql file against a DB-API connection in one
    call.
- to_localstack_url: Convert an AWS API Gateway URL to the LocalStack
    edge URL.

Requirements:
- Docker must be available for container contexts.
- Pulumi must be installed for deploy(). Uses the Automation API.

Notes:
- Contexts try to clean up on best effort and swallow errors during
    teardown so tests remain resilient.
- DEFAULT_REGION is taken from AWS_REGION or AWS_DEFAULT_REGION, then
    falls back to "us-east-1".
"""

import os
import time
import json
import logging
from contextlib import contextmanager
from typing import Dict, Generator, Optional
from pathlib import Path
import re
from urllib.parse import urlparse, urlunparse
import uuid

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
try:
    import docker
    import docker.errors
    import docker.types
except ImportError:
    docker = None  # type: ignore[assignment]

try:
    from pulumi import automation as auto  # Pulumi Automation API
except ImportError:
    auto = None  # type: ignore[assignment]

log = logging.getLogger(__name__)
DEFAULT_REGION = os.environ.get(
    "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
)


def build_deployment(
    project_name: str,
    stack_name: str,
    pulumi_program,
    localstack: Optional[Dict[str, str]] = None,
    fresh_deployment: bool = True,
) -> tuple[auto.Stack, Dict[str, str]]:
    """
    Build and deploy a Pulumi stack.

    Parameters:
      fresh_deployment: When True (default), destroys existing stack before
                       deploying to ensure a clean state. When False, updates
                       the existing stack in place.

    Returns:
      Tuple of (stack, outputs) where outputs is a dict of stack output values.
    """
    if auto is None:
        raise RuntimeError(
            "Pulumi SDK not available: cannot deploy Pulumi programs"
        )

    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=pulumi_program
    )

    # Only destroy before update if fresh_deployment=True
    if fresh_deployment:
        try:
            stack.destroy(on_output=lambda _: None)
        except (docker.errors.APIError, auto.CommandError):
            pass

    if localstack:
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
        stack.set_all_config(config)

    try:
        stack.refresh(on_output=lambda _: None)
    except auto.CommandError:
        pass

    up_result = stack.up(on_output=lambda _: None)
    outputs = {k: v.value for k, v in up_result.outputs.items()}

    return stack, outputs


def teardown_deployment(stack: auto.Stack, stack_name: str) -> None:
    """Destroy a Pulumi stack and remove it from the workspace."""
    try:
        stack.destroy(on_output=lambda _: None)
    except auto.CommandError:
        pass
    try:
        stack.workspace.remove_stack(stack_name)
    except auto.CommandError:
        pass


@contextmanager
def deploy(
    project_name: str,
    stack_name: str,
    pulumi_program,
    localstack: Optional[Dict[str, str]] = None,
) -> Generator[Dict[str, str], None, None]:
    """
    Deploy a Pulumi program and yield only the stack outputs (as a plain dict).

    Behavior:
    - If localstack is provided, injects AWS provider config (region,
      test creds, service endpoints, and "skip*" flags) so the program
      targets LocalStack instead of real AWS.
    - Destroys the stack before deploying to ensure a fresh run.
    - Runs refresh, then up; yields outputs as {name: value}.
    - On context exit, always destroys the stack and removes it from
      the workspace.

    Parameters:
      project_name: Pulumi project name for the Automation API Stack.
      stack_name  : Logical stack identifier (e.g., "test", "ci-123").
      pulumi_program: A zero-arg function that defines the Pulumi resources.
      localstack  : Optional dict from the localstack fixture with keys:
                    endpoint_url, region, services.

    Yields:
      Dict[str, str]: Exported stack outputs with raw values.
    """
    stack, outputs = build_deployment(
        project_name, stack_name, pulumi_program, localstack
    )
    try:
        yield outputs
    finally:
        teardown_deployment(stack, stack_name)


def build_container_network(
    network_name: Optional[str],
) -> tuple[object, str, bool]:
    """
    Build or find a Docker bridge network.

    Returns:
      Tuple of (network_object, network_name, created_flag)
    """
    if docker is None:
        raise RuntimeError(
            "Docker SDK not available: cannot manage container networks"
        )

    client = docker.from_env()

    net = None
    for n in client.networks.list(
        names=[network_name] if network_name else []
    ):
        if n.name == network_name:
            net = n
            break

    created = False
    if net is None:
        if not network_name:
            network_name = f"test-network-{uuid.uuid4()}"
        net = client.networks.create(network_name, driver="bridge")
        created = True

    assert network_name is not None
    return net, network_name, created


def teardown_container_network(
    net: object, created: bool
) -> None:
    """Remove a Docker network if it was created by us."""
    if created:
        try:
            net.remove()
        except docker.errors.APIError:
            pass


@contextmanager
def container_network_context(
    network_name: Optional[str]
) -> Generator[str, None, None]:
    """
    Ensure a Docker bridge network exists for the duration of a block.

    Yields the network name. Always removes it on exit if created here.
    """
    net, network_name, created = build_container_network(network_name)
    try:
        yield network_name
    finally:
        teardown_container_network(net, created)


def build_postgres(
    username: Optional[str],
    password: Optional[str],
    database: str,
    image: Optional[str],
    container_network: str,
    seed_files: Optional[list[Path]] = None,
) -> tuple[object, dict[str, str | int]]:
    """
    Build or reuse a PostgreSQL container and wait for it to be ready.

    Parameters:
      seed_files: Optional list of SQL file paths to execute after
                  container is ready.

    Returns:
      Tuple of (container_object, connection_dict)
    """
    if docker is None:
        raise RuntimeError(
            "Docker SDK not available: cannot manage container networks"
        )

    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as e:
        assert False, f"Docker not available: {e}"

    # Use a deterministic container name for reuse when teardown=False
    container_name = f"fixture-foundry-postgres-{database}"
    container = None

    # Try to find existing container
    try:
        container = client.containers.get(container_name)
        log.info(
            "Reusing existing postgres container: %s", container_name
        )

        # Ensure it's running
        if container.status != "running":
            container.start()
            log.info(
                "Started existing postgres container: %s", container_name
            )

        # Ensure it's on the correct network
        networks = container.attrs.get(
            "NetworkSettings", {}
        ).get("Networks", {})
        if container_network not in networks:
            client.networks.get(container_network).connect(container)
            log.info(
                "Connected container to network: %s", container_network
            )

    except docker.errors.NotFound:
        # Container doesn't exist, create it
        log.info("Creating new postgres container: %s", container_name)
        container = client.containers.run(
            image or "postgres:15-alpine",
            name=container_name,
            environment={
                "POSTGRES_USER": username or "testuser",
                "POSTGRES_PASSWORD": password or "testpassword",
                "POSTGRES_DB": database,
            },
            ports={"5432/tcp": 0},  # random host port
            detach=True,
            network=container_network,
        )

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

    # Wait for readiness - connect from host machine using localhost
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                dbname=database,
                user=username,
                password=password,
                host="localhost",
                port=host_port,
            )
            conn.close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.5)

    # Execute seed files if provided - connect from host machine
    if seed_files:
        conn = psycopg2.connect(
            dbname=database,
            user=username,
            password=password,
            host="localhost",
            port=host_port,
        )
        conn.autocommit = True
        try:
            for sql_file in seed_files:
                log.info("Executing seed file: %s", sql_file)
                from fixture_foundry.utils import exec_sql_file
                exec_sql_file(conn, sql_file)
        finally:
            conn.close()

    connection_info = {
        "container_name": host,
        "container_port": 5432,
        "username": username,
        "password": password,
        "database": database,
        "host_port": host_port,
        "dsn": (
            f"postgresql://{username}:{password}@"
            f"localhost:{host_port}/{database}"
        ),
    }

    return container, connection_info


def teardown_postgres(container: object) -> None:
    """Stop and remove a PostgreSQL container."""
    try:
        container.stop(timeout=5)
    except (docker.errors.APIError, docker.errors.NotFound):
        pass
    try:
        container.remove(v=True, force=True)
    except (docker.errors.APIError, docker.errors.NotFound):
        pass


@contextmanager
def postgres_context(
    username: Optional[str],
    password: Optional[str],
    database: str,
    image: Optional[str],
    container_network: str,
    seed_files: Optional[list[Path]] = None,
) -> Generator[dict[str, str | int], None, None]:
    """
    Start a PostgreSQL container and yield connection details.

    Yields connection info dict with DSN, host port, and credentials.
    Always removes container on exit.
    """
    container, connection_info = build_postgres(
        username, password, database, image, container_network, seed_files
    )
    try:
        yield connection_info
    finally:
        teardown_postgres(container)


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
                    except ValueError:
                        data = {}
                    # Healthy if initialized true or services reported
                    if isinstance(data, dict):
                        if data.get("initialized") is True:
                            return
                        if "services" in data:
                            # services dict often present when up
                            return
                    else:
                        return
            # noqa: PERF203 - simple polling loop
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(0.5)
                continue
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for LocalStack at {endpoint} (last_err={last_err})"
    )


def build_localstack(
    image: str,
    services: str,
    port: int,
    timeout: int,
    container_network: str,
) -> tuple[object, Dict[str, str | int]]:
    """
    Build or reuse a LocalStack container and wait for it to be ready.

    Returns:
      Tuple of (container_object, endpoint_dict)
    """
    if docker is None:
        assert False, (
            "Docker SDK not available: "
            "skipping LocalStack-dependent tests"
        )

    try:
        client = docker.from_env()
    except docker.errors.DockerException:
        assert False, (
            "Docker daemon not available: "
            "skipping LocalStack-dependent tests"
        )

    # Use deterministic name for container reuse when teardown=False
    container_name = (
        f"fixture-foundry-localstack-"
        f"{port if port != 0 else 'auto'}"
    )
    container = None

    # Try to find and reuse existing container
    try:
        container = client.containers.get(container_name)
        log.info(
            "Reusing existing LocalStack container: %s",
            container_name
        )

        # Ensure it's running
        if container.status != "running":
            container.start()
            log.info(
                "Started existing LocalStack container: %s",
                container_name
            )

        # Ensure it's on the correct network
        networks = container.attrs.get(
            "NetworkSettings", {}
        ).get("Networks", {})
        if container_network not in networks:
            client.networks.get(container_network).connect(container)
            log.info(
                "Connected LocalStack to network: %s",
                container_network
            )

    except docker.errors.NotFound:
        # Container doesn't exist, create it
        log.info(
            "Creating new LocalStack container: %s",
            container_name
        )

        # Pull image to ensure availability
        try:
            client.images.pull(image)
        except docker.errors.ImageNotFound:
            # Already have it locally, proceed
            pass
        except docker.errors.APIError:
            # Already have it locally, proceed
            pass

        # Publish edge port only
        ports = {
            "4566/tcp": port,
        }
        env = {
            "SERVICES": services,
            "LS_LOG": "warn",
            "AWS_DEFAULT_REGION": DEFAULT_REGION,
            # Ensure Lambda containers join this network
            "LAMBDA_DOCKER_NETWORK": container_network,
            "DISABLE_CORS_CHECKS": "1",
        }
        # Mount Docker socket for LocalStack to access Docker
        volume_dir = os.environ.get(
            "LOCALSTACK_VOLUME_DIR", "./volume"
        )
        Path(volume_dir).mkdir(parents=True, exist_ok=True)
        mounts = [
            docker.types.Mount(
                target="/var/run/docker.sock",
                source="/var/run/docker.sock",
                type="bind",
                read_only=False,
            ),
            docker.types.Mount(
                target="/var/lib/localstack",
                source=os.path.abspath(volume_dir),
                type="bind",
                read_only=False,
            ),
        ]
        container = client.containers.run(
            image,
            name=container_name,
            detach=True,
            environment=env,
            ports=ports,
            tty=False,
            mounts=mounts,
            network=container_network,
        )

    if port == 0:
        # Resolve host port assigned for edge
        host_port = None
        max_attempts = 10
        for _ in range(max_attempts):
            container.reload()
            try:
                port_info = container.attrs[
                    "NetworkSettings"
                ]["Ports"]["4566/tcp"]
                if (port_info and port_info[0] and
                        port_info[0].get("HostPort")):
                    host_port = int(port_info[0]["HostPort"])
                    break
            except (docker.errors.APIError, docker.errors.NotFound):
                pass
            time.sleep(0.5)
        if host_port is None:
            # Clean up if mapping not available
            try:
                container.stop(timeout=5)
            finally:
                raise RuntimeError(
                    "Failed to determine LocalStack edge port "
                    "after retries"
                )
    else:
        host_port = port

    endpoint = f"http://localhost:{host_port}"

    # Set common AWS envs for child code that relies on defaults
    os.environ.setdefault("AWS_REGION", DEFAULT_REGION)
    os.environ.setdefault("AWS_DEFAULT_REGION", DEFAULT_REGION)
    os.environ.setdefault(
        "AWS_ACCESS_KEY_ID",
        os.environ.get("AWS_ACCESS_KEY_ID", "test")
    )
    os.environ.setdefault(
        "AWS_SECRET_ACCESS_KEY",
        os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
    )

    # Wait for the health endpoint to be ready
    _wait_for_localstack(endpoint, timeout=timeout)

    endpoint_info = {
        "endpoint_url": endpoint,
        "region": DEFAULT_REGION,
        "container_id": str(container.id),
        "container_name": container.name or "",
        "container_port": 4566,
        "services": services,
        "port": str(host_port),
    }

    return container, endpoint_info


def teardown_localstack(container: object) -> None:
    """Stop and remove a LocalStack container."""
    if container is None:
        return
    
    try:
        log.info(f"Stopping LocalStack container {container.name}...")
        container.stop(timeout=5)
        log.info(f"LocalStack container {container.name} stopped")
    except (docker.errors.APIError, docker.errors.NotFound) as e:
        log.warning(f"Error stopping LocalStack container: {e}")
    except Exception as e:
        log.error(f"Unexpected error stopping LocalStack container: {e}")
    
    try:
        log.info(f"Removing LocalStack container {container.name}...")
        container.remove(v=True, force=True)
        log.info(f"LocalStack container {container.name} removed")
    except (docker.errors.APIError, docker.errors.NotFound) as e:
        log.warning(f"Error removing LocalStack container: {e}")
    except Exception as e:
        log.error(f"Unexpected error removing LocalStack container: {e}")


@contextmanager
def localstack_context(
    image: str,
    services: str,
    port: int,
    timeout: int,
    container_network: str
) -> Generator[Dict[str, str | int], None, None]:
    """
    Start LocalStack on a Docker network and yield endpoint metadata.

    Yields endpoint info dict with endpoint_url, region, and services.
    Always removes container on exit.
    """
    container, endpoint_info = build_localstack(
        image, services, port, timeout, container_network
    )
    try:
        yield endpoint_info
    finally:
        teardown_localstack(container)


def to_localstack_url(
    api_url: str,
    edge_port: int = 4566,
    scheme: str = "http"
) -> str:
    """
    Convert an AWS API Gateway invoke URL into the equivalent
    LocalStack edge URL.

    Accepts:
      - Full URLs:
        https://{id}.execute-api.{region}.amazonaws.com/{stage}/path
      - Bare host/path: {id}.execute-api.{region}.amazonaws.com/{stage}/path
      - Already-converted LocalStack hostnames are normalized and returned.

    Returns:
      URL targeting {id}.execute-api.localhost.localstack.cloud:{edge_port}
      with the same path, query, and fragment, using the provided scheme
      (default http).

    Raises:
      ValueError if the hostname does not match an API Gateway pattern or
      if the stage segment is missing from the path.
    """
    if not re.match(r"^[a-z]+://", api_url):
        # prepend dummy scheme so urlparse works uniformly
        api_url = f"https://{api_url}"

    parsed = urlparse(api_url)

    # If already a LocalStack style host, normalize and return
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
        r"^(?P<api_id>[a-z0-9]+)\.execute-api\."
        r"(?P<region>[-a-z0-9]+)\.amazonaws\.com$",
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
    # Reconstruct path exactly as given
    # (we don't strip or re-add trailing slash)
    new_host = f"{api_id}.execute-api.localhost.localstack.cloud:{edge_port}"

    return urlunparse(
        (scheme, new_host, path, parsed.params, parsed.query, parsed.fragment)
    )
