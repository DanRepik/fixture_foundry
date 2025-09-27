# fixture_foundry

Pytest-friendly fixtures and helpers to stand up realistic local integration environments:
- LocalStack for AWS services (API Gateway, Lambda, Secrets Manager, CloudWatch, etc.)
- Containerized Postgres for databases
- Pulumi Automation API for ephemeral infra
- A shared Docker network to wire everything together

Works on macOS with Docker Desktop.

## Features

- pytest fixtures: localstack, postgres, test_network
- Infra deploy helper: deploy(...) for Pulumi Automation API
- SQL helper: exec_sql_file(...) to seed databases
- URL helper: to_localstack_url(...) to call API Gateway via LocalStack
- Sensible health checks, retries, and teardown

## Install

Install the library and common runtime deps:
```bash
python -m pip install fixture-foundry docker requests pytest psycopg2-binary
# Optional if you use deploy(...):
python -m pip install pulumi pulumi-aws
```

> Python 3.8–3.12 supported.

## Quick start
Enable the fixtures in your test suite. If you don’t expose CLI options already, add them in conftest.py.

```python
# filepath: [conftest.py](http://_vscodecontentref_/0)
import pytest
from fixture_foundry import (
    localstack,  # noqa: F401 - fixtures discovered by pytest
    postgres,    # noqa: F401
    test_network # noqa: F401
)

DEFAULT_IMAGE = "localstack/localstack:latest"
DEFAULT_SERVICES = "logs,iam,lambda,secretsmanager,apigateway,cloudwatch"

def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("localstack")
    g.addoption("--teardown", action="store", default="true")
    g.addoption("--localstack-image", action="store", default=DEFAULT_IMAGE)
    g.addoption("--localstack-services", action="store", default=DEFAULT_SERVICES)
    g.addoption("--localstack-timeout", action="store", type=int, default=90)
    g.addoption("--localstack-port", action="store", type=int, default=0)
    g.addoption("--database", action="store", type=str, default="chinook")
    g.addoption("--database-image", action="store", type=str, default="postgres:16")
```

### Set Up a Postges Database

The postgres fixture starts a Postgres container for the test session and yields connection details (including a host-accessible DSN) to tests or fixtures.  When the yield resumes the container is stopped and deleted.

Create a session-scoped fixture that seeds schemas and data by executing SQL files during setup, then yield the same connection info. This keeps your tests deterministic and repeatable.

The example below loads the Chinook sample schema into the Postgres container.

```python

from fixture_foundry import postgres, exec_sql_file

@pytest.fixture(scope="session")
def chinook_db(postgres):  # noqa F811
    # Locate DDL files 
    project_root = Path(__file__).resolve().chinook_sql = project_root / "tests" / "Chinook_Postgres.sql"

    assert chinook_sql.exists(), f"Missing {chinook_sql}"

    # Connect and load schemas
    dsn = f"postgresql://{postgres['username']}:{postgres['password']}@localhost:{postgres['host_port']}/{postgres['database']}"  # noqa E501

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True  # allow full scripts to run without transaction issues
        exec_sql_file(conn, chinook_sql)

        yield postgres

    finally:
        conn.close()
```

### Deploy Pulumi Infrastructure to Localstack

Provision AWS resources locally to LocalStack. The `deploy(...)` context manager, provided by `fixture_foundry`, configures Pulumi to deploy resources to LocalStack (endpoints, test credentials, region), runs your program, and yields stack outputs. When testing is complete `deploy` then destroys the deployed resources. 

In the example below, a module-scoped `chinook_api_stack` fixture composes `localstack` with `deploy(...)` to stand up the Chinook API. The `pulumi_program` consumes the chinook_db fixture to store DB connection details in Secrets Manager and create an API Foundry component (API Gateway + Lambda). The program exports the API endpoint, which you can translate to a LocalStack endpint URL with `to_localstack_url` for end-to-end HTTP tests.


```python
def chinook_api(chinook_db):
    def pulumi_program():
        from api_foundry import APIFoundry

        # Extract connection info from the chinook_db fixture
        conn_info = {
            "engine": "postgres",
            "host": chinook_db["container_name"],
            "port": chinook_db["container_port"],
            "username": chinook_db["username"],
            "password": chinook_db["password"],
            "database": chinook_db["database"],
            "dsn": chinook_db["dsn"],
        }

        # Put the connection info in a secret
        secret = aws.secretsmanager.Secret("test-secret", name="test/secret")
        aws.secretsmanager.SecretVersion(
            "test-secret-value",
            secret_id=secret.id,
            secret_string=json.dumps(conn_info),
        )

        # Create the API
        chinook_api = secret.arn.apply(
            lambda arn: APIFoundry(
                "chinook-api",
                api_spec="resources/chinook_api.yaml",
                secrets=json.dumps({"chinook": arn}),
            )
        )
        pulumi.export("endpoint", chinook_api.domain)

    return pulumi_program


@pytest.fixture(scope="module")
def chinook_api_stack(request, chinook_db, localstack):  # noqa F811
    teardown = request.config.getoption("--teardown").lower() == "true"
    with deploy(
        "api-foundry",
        "test-api",
        chinook_api(chinook_db),
        localstack=localstack,
        teardown=teardown,
    ) as outputs:
        yield outputs
```

The `chinook_api_stack` yields the ouputs of `pulumi_program` the AWS Gateway API endpoint in its outputs that allows access to the API when running on AWS.  Since the API is running on Localstack this endpoint needs to be translated into a LocalStack endpoint URL with `to_localstack_url` for end-to-end HTTP tests.

For convienance this url translation is handled by another session scoped fixture.  Tests can use this fixture to access the endpoint to send requests to the test API.

```python
@pytest.fixture(scope="module")
def chinook_api_endpoint(chinook_api_stack, localstack):  # noqa F811
    domain = chinook_api_stack["domain"]
    port = localstack["port"]
    yield to_localstack_url(f"https://{domain}", port)

```

With the `chinook_api_endpoint` tests can directly use the Localstack URL.  

```python
import requests
import logging

log = logging.getLogger(__name__)

def test_chinook_get_album(chinook_api_endpoint):
    url = f"{chinook_api_endpoint}/album/1"
    response = requests.get(url)
    log.info(f"Response Status Code: {response.status_code}")
    log.info(f"Response: {response.text}")
    assert response.status_code == 200
```

When the testing session is complete all testing resources and containers are stopped and deleted.

## Reference

Fixtures
- test_network (session)
  - Yields: network name (str)
  - Env: DOCKER_TEST_NETWORK (default: ls-dev)
  - Teardown: removed if --teardown=true and the fixture created it

- postgres (session)
  - Starts a Postgres container on test_network
  - Yields (dict):
    - container_name, container_port (5432)
    - username, password, database
    - host_port (random mapped), dsn (postgresql://...)
  - Use container_name:5432 from other containers (e.g., Lambda) and localhost:host_port from tests

- localstack (session)
  - Starts LocalStack on test_network, mounts /var/run/docker.sock
  - Sets LAMBDA_DOCKER_NETWORK to enable Lambda -> Postgres connectivity
  - Yields (dict): endpoint_url, region, port, services

Helpers
- deploy(project_name, stack_name, pulumi_program, config=None, localstack=None, teardown=True)
  - Pulumi Automation context manager that targets LocalStack when localstack is provided
  - Injects endpoints, region, and test creds; yields stack outputs; destroys on exit if teardown

- exec_sql_file(conn, path: Path)
  - Executes a SQL script file; supports DO $$ ... $$ blocks
  - Set conn.autocommit = True for multi-statement scripts

- to_localstack_url(api_url, edge_port=4566, scheme="http") -> str
  - Converts API Gateway invoke URLs to the LocalStack edge hostname/port

Pytest CLI options
- --teardown=true|false
- --localstack-image=localstack/localstack:latest
- --localstack-services=logs,iam,lambda,secretsmanager,apigateway,cloudwatch
- --localstack-timeout=90
- --localstack-port=0  (0 = random host port)
- --database=chinook
- --database-image=postgres:16

Environment
- AWS_REGION / AWS_DEFAULT_REGION (default: us-east-1)
- LAMBDA_DOCKER_NETWORK set to test_network inside LocalStack
- DOCKER_TEST_NETWORK to override the shared network name

Ports
- LocalStack edge: 4566 (mapped to a host port; use localstack["port"])
- Postgres: 5432 inside Docker; host port is dynamically mapped

Health checks
- LocalStack readiness: GET /_localstack/health or /health on endpoint_url
- Postgres readiness: simple connect retry loop before yielding

Notes
- Session-scoped fixtures run once per pytest process (per xdist worker)
- Prefer container_name:5432 for cross-container access; use host_port for host-based tests

