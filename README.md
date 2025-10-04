# Fixture Foundry

Pytest fixtures and context managers for ephemeral integration testing infrastructure: LocalStack (AWS services), PostgreSQL containers, and Pulumi deployment automation.

## Features

- **Session-scoped fixtures**: `localstack`, `postgres`, `container_network`
- **Context managers**: Standalone infrastructure orchestration for dev scripts
- **Pulumi Automation**: `deploy()` helper targets LocalStack automatically
- **Database seeding**: `exec_sql_file()` loads SQL schemas with transaction support
- **URL translation**: `to_localstack_url()` converts AWS Gateway URLs to LocalStack endpoints
- **Auto-cleanup**: Health checks, retries, and teardown on test completion

## Installation

```bash
pip install fixture-foundry docker requests pytest psycopg2-binary
# Optional for Pulumi deployment:
pip install pulumi pulumi-aws
```

**Requirements**: Python 3.8–3.12, Docker

## Quick Start

### Setup pytest fixtures in conftest.py

```python
import pytest
from fixture_foundry import localstack, postgres, container_network  # noqa: F401

def pytest_addoption(parser):
    g = parser.getgroup("localstack")
    g.addoption("--teardown", default="true")
    g.addoption("--localstack-port", type=int, default=0)  # 0 = random port
    g.addoption("--database-image", default="postgres:16")
```

### Database with Schema Loading

```python
from pathlib import Path
import psycopg2
from fixture_foundry import postgres, exec_sql_file

@pytest.fixture(scope="session")
def test_db(postgres):
    # Load schema from SQL file
    schema_file = Path(__file__).parent / "schema.sql"
    
    conn = psycopg2.connect(postgres["dsn"])
    conn.autocommit = True  # Required for multi-statement scripts
    exec_sql_file(conn, schema_file)
    conn.close()
    
    yield postgres
```

### Pulumi Deployment to LocalStack

```python
import json
from fixture_foundry import deploy, to_localstack_url

def my_pulumi_program():
    # Your Pulumi resources here
    pulumi.export("api_url", "https://example.execute-api.us-east-1.amazonaws.com")

@pytest.fixture(scope="module") 
def api_stack(test_db, localstack):
    with deploy("my-project", "test", my_pulumi_program, localstack=localstack) as outputs:
        yield outputs

@pytest.fixture(scope="module")
def api_endpoint(api_stack, localstack):
    # Convert AWS Gateway URL to LocalStack endpoint
    aws_url = api_stack["api_url"]
    yield to_localstack_url(aws_url, localstack["port"])

def test_api_call(api_endpoint):
    import requests
    response = requests.get(f"{api_endpoint}/health")
    assert response.status_code == 200
```

### Context Managers (for dev scripts)

```python
from pathlib import Path
import psycopg2
from fixture_foundry import postgres_context, localstack_context, deploy, exec_sql_file

# Standalone usage outside pytest
with postgres_context(database="mydb") as pg:
    # Seed database with schema
    conn = psycopg2.connect(pg["dsn"])
    conn.autocommit = True
    exec_sql_file(conn, Path("schema.sql"))
    conn.close()
    
    with localstack_context() as ls:
        with deploy("proj", "stack", program, localstack=ls) as outputs:
            # Infrastructure is now running - execute your dev workflow
            api_url = outputs['endpoint']
            print(f"API deployed at: {api_url}")
            
            # Start frontend dev server, run manual tests, etc.
            # subprocess.run(["npm", "run", "dev"], env={"VITE_API_URL": api_url})
            # Or keep running for interactive development...
```

## API Reference

### Fixtures (session-scoped)

| Fixture | Yields | Description |
|---------|--------|-------------|
| `container_network` | `str` | Docker bridge network name (default: "ls-dev") |
| `postgres` | `dict` | PostgreSQL container with `dsn`, `host_port`, `container_name`, credentials |
| `localstack` | `dict` | LocalStack container with `endpoint_url`, `port`, `region`, `services` |

### Functions

| Function | Purpose |
|----------|---------|
| `deploy(project, stack, program, localstack=None, teardown=True)` | Pulumi Automation API context manager |
| `exec_sql_file(conn, path)` | Execute SQL file (supports multi-statement, `DO $$` blocks) |
| `to_localstack_url(aws_url, edge_port=4566)` | Convert AWS Gateway URL to LocalStack endpoint |

### Connection Patterns

- **Container-to-container**: Use `postgres["container_name"]:5432` (Docker network)
- **Host-to-container**: Use `localhost:{postgres["host_port"]}` (port mapping)
- **Lambda-to-DB**: Requires shared Docker network (automatic with LocalStack)

### Environment Variables

- `AWS_REGION` / `AWS_DEFAULT_REGION`: AWS region (default: us-east-1)
- `DOCKER_TEST_NETWORK`: Override network name (default: ls-dev)
- `LAMBDA_DOCKER_NETWORK`: Set by LocalStack for container connectivity

### CLI Options

Add to your `conftest.py` via `pytest_addoption()`:

- `--teardown=true|false`: Control resource cleanup
- `--localstack-port=0`: LocalStack port (0 = random)
- `--database-image=postgres:16`: PostgreSQL Docker image
- `--localstack-image=localstack/localstack:latest`: LocalStack image

