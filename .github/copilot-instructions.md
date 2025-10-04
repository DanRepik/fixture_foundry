# AI Coding Agent Instructions for Fixture Foundry

Fixture Foundry provides pytest fixtures and context managers for ephemeral integration testing infrastructure: LocalStack (AWS services), PostgreSQL containers, and Pulumi deployment automation.

## Core Architecture
- **Context managers**: `src/fixture_foundry/context.py` - Standalone infra orchestration
- **Pytest fixtures**: `src/fixture_foundry/fixtures.py` - Session-scoped test fixtures wrapping contexts
- **Utilities**: `src/fixture_foundry/utils.py` - SQL execution and URL transformation helpers

## Essential Patterns

### Context Manager Design
All infrastructure uses context managers for automatic cleanup:
```python
# Standalone usage (dev scripts, notebooks)
with postgres_context(database="test") as pg:
    with localstack_context() as ls:
        with deploy("proj", "stack", pulumi_program, localstack=ls) as outputs:
            # Use infrastructure...
            pass
# Auto-cleanup on exit
```

### Pytest Integration
Session-scoped fixtures wrap contexts for test suites:
```python
@pytest.fixture(scope="session")
def chinook_db(postgres):  # postgres fixture from fixture_foundry
    conn = psycopg2.connect(postgres["dsn"])
    conn.autocommit = True
    exec_sql_file(conn, "schema.sql")  # Load test data
    yield postgres
```

## Key Components

### Infrastructure Orchestration
- **`deploy()`**: Pulumi Automation API wrapper that targets LocalStack when provided
- **`postgres_context()`**: Spins up PostgreSQL container with random host port mapping  
- **`localstack_context()`**: Starts LocalStack with Docker network for Lambda→DB connectivity
- **`container_network_context()`**: Creates shared Docker bridge network

### Connection Patterns
- **Container-to-container**: Use `container_name:5432` (internal Docker network)
- **Host-to-container**: Use `localhost:host_port` (port mapping from fixture)
- **Lambda-to-database**: Requires shared Docker network via `LAMBDA_DOCKER_NETWORK`

### SQL Database Setup
```python
# Standard pattern for loading test schemas
conn = psycopg2.connect(postgres["dsn"])
conn.autocommit = True  # Required for multi-statement scripts
exec_sql_file(conn, Path("test_schema.sql"))
```

## Testing Workflows

### CLI Options (add to conftest.py)
```python
def pytest_addoption(parser):
    g = parser.getgroup("localstack")
    g.addoption("--teardown", default="true")  # Control cleanup
    g.addoption("--localstack-port", type=int, default=0)  # 0 = random port
    g.addoption("--database-image", default="postgres:16")
```

### URL Translation for API Tests
```python
# Convert AWS API Gateway URLs to LocalStack endpoints
localstack_url = to_localstack_url(
    "https://abc123.execute-api.us-east-1.amazonaws.com/prod/path",
    edge_port=localstack["port"]
)
# Result: http://abc123.execute-api.localhost.localstack.cloud:4566/prod/path
```

## File Organization
- **`src/fixture_foundry/context.py`**: Core context managers (479 lines)
- **`src/fixture_foundry/fixtures.py`**: Pytest fixture wrappers (185 lines)  
- **`src/fixture_foundry/utils.py`**: Helper functions (97 lines)
- **`test/conftest.py`**: Example pytest configuration

## Common Integration Issues
- **Docker socket access**: Set `DOCKER_HOST` env var for non-default Docker runtimes (macOS)
- **Network connectivity**: LocalStack sets `LAMBDA_DOCKER_NETWORK` automatically for container-to-container access
- **Port conflicts**: Use `--localstack-port=0` for random port assignment in CI
- **SQL script execution**: Always set `conn.autocommit = True` for multi-statement files