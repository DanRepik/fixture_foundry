import json
import os
from pathlib import Path

import pytest
import psycopg2
import pulumi
import pulumi_aws as aws
import yaml

from fixture_foundry import to_localstack_url
from fixture_foundry import deploy  # noqa F401
from fixture_foundry import exec_sql_file
from fixture_foundry import postgres  # noqa F401
from fixture_foundry import localstack  # noqa F401
from fixture_foundry import container_network  # noqa F401

os.environ["PULUMI_BACKEND_URL"] = "file://~"

DEFAULT_IMAGE = "localstack/localstack:latest"
DEFAULT_SERVICES = "logs,iam,lambda,secretsmanager,apigateway,cloudwatch"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("localstack")
    group.addoption(
        "--teardown",
        action="store",
        default="true",
        help="Whether to tear down the LocalStack/Postgres containers after tests (default: true)",
    )
    group.addoption(
        "--localstack-image",
        action="store",
        default=DEFAULT_IMAGE,
        help="Docker image to use for LocalStack (default: localstack/localstack:latest)",
    )
    group.addoption(
        "--localstack-services",
        action="store",
        default=DEFAULT_SERVICES,
        help="Comma-separated list of LocalStack services to start",
    )
    group.addoption(
        "--localstack-timeout",
        action="store",
        type=int,
        default=90,
        help="Seconds to wait for LocalStack to become healthy (default: 90)",
    )
    group.addoption(
        "--localstack-port",
        action="store",
        type=int,
        default=0,
        help="Port for LocalStack edge service (default: 0 = random available port)",
    )
    group.addoption(
        "--database",
        action="store",
        type=str,
        default="chinook",
        help="Name of the database to use (default: chinook)",
    )
    group.addoption(
        "--database-image",
        action="store",
        type=str,
        default="postgis/postgis:16-3.4",
        help="Docker image to use for the database (default: chinook)",
    )


@pytest.fixture(scope="session")
def chinook_db(postgres):  # noqa F811
    # Locate DDL files (project root is one parent up from this test file: backend/tests/ -> farm_market/)
    project_root = Path(__file__).resolve().parents[1]
    chinook_sql = project_root / "tests" / "Chinook_Postgres.sql"

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


def chinook_api(chinook_db):
    def pulumi_program():

        # Extract connection info from freemium_model
        conn_info = {
            "engine": "postgres",
            "host": chinook_db["container_name"],
            "port": chinook_db["container_port"],
            "username": chinook_db["username"],
            "password": chinook_db["password"],
            "database": chinook_db["database"],
            "dsn": chinook_db["dsn"],
        }

        secret = aws.secretsmanager.Secret("test-secret", name="test/secret")
        aws.secretsmanager.SecretVersion(
            "test-secret-value",
            secret_id=secret.id,
            secret_string=json.dumps(conn_info),
        )

        # Create the FarmMarket component
        chinook_api = secret.arn.apply(
            lambda arn: APIFoundry(
                "chinook-api",
                api_spec="resources/chinook_api.yaml",
                secrets=json.dumps({"chinook": arn}),
            )
        )
        pulumi.export("domain", chinook_api.domain)

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


@pytest.fixture(scope="module")
def chinook_api_endpoint(chinook_api_stack, localstack):  # noqa F811
    domain = chinook_api_stack["domain"]
    port = localstack["port"]
    yield to_localstack_url(f"https://{domain}", port)


@pytest.fixture(scope="module")
def load_api_model():
    filename = os.path.join(os.getcwd(), "resources/api_spec.yaml")
    with open(filename, "r", encoding="utf-8") as file:
        yield APIModel(yaml.safe_load(file))


@pytest.fixture(scope="module")
def chinook_api_model():
    filename = os.path.join(os.getcwd(), "resources/chinook_api.yaml")
    with open(filename, "r", encoding="utf-8") as file:
        yield yaml.safe_load(file)
