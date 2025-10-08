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


@contextmanager
def deploy(
    project_name: str,
    stack_name: str,
    pulumi_program,
    localstack: Optional[Dict[str, str]] = None,
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
    if auto is None:
        raise RuntimeError("Pulumi SDK not available: cannot deploy Pulumi programs")

    stack = auto.create_or_select_stack(
        stack_name=stack_name, project_name=project_name, program=pulumi_program
    )

    try:
        # Best effort pre-clean
        try:
            stack.destroy(on_output=lambda _: None)
        except docker.errors.APIError:
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

        yield outputs
    finally:
        if teardown:
            try:
                stack.destroy(on_output=lambda _: None)
            except auto.CommandError:
                pass
            try:
                stack.workspace.remove_stack(stack_name)
            except auto.CommandError:
                pass


@contextmanager
def container_network_context(network_name: Optional[str], teardown: Optional[bool]) -> Generator[str, None, None]:
    if docker is None:
        raise RuntimeError("Docker SDK not available: cannot manage container networks")
    
    client = docker.from_env()

    net = None
    for n in client.networks.list(names=[network_name] if network_name else []):
        if n.name == network_name:
            net = n
            break

    created = False
    if net is None:
        if not network_name:
            network_name = f"test-network-{uuid.uuid4()}"
        net = client.networks.create(network_name, driver="bridge")
        created = True

    try:
        # Ensure network_name is always a str here
        assert network_name is not None
        yield network_name
    finally:
        if created and teardown:
            try:
                net.remove()
            except Exception:
                pass

@contextmanager
def postgres_context(username: Optional[str], 
                     password: Optional[str], 
                     database: str, image: Optional[str], 
                     container_network: str) -> Generator[dict[str, str | int], None, None]:

    if docker is None:
        raise RuntimeError("Docker SDK not available: cannot manage container networks")
    
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as e:
        assert False, f"Docker not available: {e}"

    container = client.containers.run(
        image or "postgres:15-alpine",
        environment={
            "POSTGRES_USER": username or "testuser",
            "POSTGRES_PASSWORD": password or "testpassword",
            "POSTGRES_DB": database,
        },
        ports={"5432/tcp": 0},  # random host port
        detach=True,
        network=container_network,
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
            except psycopg2.OperationalError:
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
                    except ValueError:
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

@contextmanager
def localstack_context(image: str, services: str, port: int, timeout: int, teardown: bool, container_network: str) -> Generator[Dict[str, str], None, None]:
    if docker is None:
        assert False, "Docker SDK not available: skipping LocalStack-dependent tests"

    try:
        client = docker.from_env()
    except docker.errors.DockerException:
        assert False, "Docker daemon not available: skipping LocalStack-dependent tests"

    # Pull image to ensure availability
    try:
        client.images.pull(image)
    except docker.errors.ImageNotFound:
        # If pull fails because image not found, we may already have it locally — proceed
        pass
    except docker.errors.APIError:
        # If pull fails due to API error, we may already have it locally — proceed
        pass

    # Publish only the edge port; service port range is not needed with edge
    ports = {
        "4566/tcp": port,
    }
    env = {
        "SERVICES": services,
        "LS_LOG": "warn",
        "AWS_DEFAULT_REGION": DEFAULT_REGION,
        "LAMBDA_DOCKER_NETWORK": container_network,  # ensure Lambda containers join this network
        "DISABLE_CORS_CHECKS": "1",
    }
    # Mount Docker socket for LocalStack to access Docker if needed
    volume_dir = os.environ.get("LOCALSTACK_VOLUME_DIR", "./volume")
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
        detach=True,
        environment=env,
        ports=ports,
        name=None,
        tty=False,
        mounts=mounts,
        network=container_network,
    )

    if port == 0:
        # Resolve host port assigned for edge, with retries to avoid race condition
        host_port = None
        max_attempts = 10
        for _ in range(max_attempts):
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
