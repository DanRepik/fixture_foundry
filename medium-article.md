# Stop Wrestling with Integration Tests: How Fixture Foundry Makes AWS + Database Testing Actually Enjoyable

*Building realistic integration tests shouldn't feel like configuring a small data center. Here's how one Python library changed everything.*

---

If you've ever tried to write integration tests for an AWS-based application that uses a database, you know the pain. You need LocalStack running, a database container, networking between them, proper cleanup, and somehow you need to orchestrate all of this without losing your sanity.

I spent months cobbling together Docker Compose files, custom test harnesses, and brittle setup scripts. Then I discovered a better way.

## The Problem: Integration Testing is Hard

Let's say you're building a serverless API with AWS Lambda, API Gateway, and PostgreSQL. Your integration tests need:

- A running PostgreSQL database with test data
- LocalStack to simulate AWS services
- Proper networking so Lambda can talk to the database
- Database schema loading and cleanup
- URL translation between AWS and LocalStack endpoints
- Reliable teardown when tests complete

The traditional approach involves juggling multiple tools, writing custom Docker scripts, and praying everything connects properly. It's brittle, slow to set up, and frustrating to debug.

## Enter Fixture Foundry

[Fixture Foundry](https://github.com/DanRepik/fixture_foundry) is a Python library that provides pytest fixtures and context managers for ephemeral integration testing infrastructure. It handles all the orchestration complexity and gives you a clean, reliable foundation for testing.

Here's what it provides out of the box:

- **Session-scoped pytest fixtures** for LocalStack and PostgreSQL
- **Context managers** for standalone scripts and development workflows
- **Pulumi Automation API integration** for infrastructure-as-code testing
- **Automatic networking** between containers
- **URL translation** from AWS Gateway URLs to LocalStack endpoints
- **Database seeding utilities** with transaction support
- **Intelligent cleanup** with health checks and retries

## Getting Started: The Basics

Installation is straightforward:

```bash
pip install fixture-foundry docker requests pytest psycopg2-binary
# Optional for Pulumi deployment:
pip install pulumi pulumi-aws
```

## Real-World Example: Testing a Complete API

Let's build something realistic. Imagine you have a Pulumi program that creates an API Gateway + Lambda that connects to PostgreSQL. Here's how you'd test it end-to-end:

### Step 1: Database Setup with Schema

```python
# conftest.py - Add these fixtures to your conftest.py file
from pathlib import Path
import psycopg2
from fixture_foundry import postgres, exec_sql_file

@pytest.fixture(scope="session")
def test_db(postgres):
    # Load your application schema
    schema_file = Path(__file__).parent / "schema.sql"
    
    conn = psycopg2.connect(postgres["dsn"])
    conn.autocommit = True  # Required for multi-statement scripts
    exec_sql_file(conn, schema_file)
    conn.close()
    
    yield postgres
```

### Step 2: Deploy Your Infrastructure

```python
# conftest.py - Continue adding to your conftest.py file
import json
from fixture_foundry import deploy, to_localstack_url

def my_api_program(database):
    def pulumi_program():
        import pulumi
        import pulumi_aws as aws
        
        # Store DB connection in Secrets Manager
        conn_info = {
            "host": database["container_name"],  # Container networking
            "port": database["container_port"],
            "username": database["username"],
            "password": database["password"],
            "database": database["database"]
        }
        
        secret = aws.secretsmanager.Secret("db-secret")
        aws.secretsmanager.SecretVersion(
            "db-secret-version",
            secret_id=secret.id,
            secret_string=json.dumps(conn_info)
        )
        
        # Your Lambda function code here...
        # Your API Gateway setup here...
        
        pulumi.export("api_url", api_gateway.execution_arn)
    
    return pulumi_program

@pytest.fixture(scope="module")
def api_stack(test_db, localstack):
    with deploy("my-api", "test", my_api_program(test_db), localstack=localstack) as outputs:
        yield outputs
```

### Step 3: URL Translation and Testing

```python
@pytest.fixture(scope="module")
def api_endpoint(api_stack, localstack):
    # Convert AWS Gateway URL to LocalStack endpoint
    aws_url = api_stack["api_url"]
    yield to_localstack_url(aws_url, localstack["port"])

def test_api_creates_user(api_endpoint, test_db):
    import requests
    
    # Test the API
    response = requests.post(f"{api_endpoint}/users", json={
        "name": "John Doe",
        "email": "john@example.com"
    })
    
    assert response.status_code == 201
    user_id = response.json()["id"]
    
    # Verify in database
    import psycopg2
    conn = psycopg2.connect(test_db["dsn"])
    cursor = conn.cursor()
    cursor.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
    name, email = cursor.fetchone()
    
    assert name == "John Doe"
    assert email == "john@example.com"
    
    conn.close()
```

## Beyond Testing: Development Workflows

One of my favorite features is using the context managers for development scripts. You can spin up the same infrastructure outside of pytest:

```python
from pathlib import Path
import psycopg2
import subprocess
from fixture_foundry import postgres_context, localstack_context, deploy, exec_sql_file

# Development server script
with postgres_context(database="myapp") as pg:
    # Seed database with schema
    conn = psycopg2.connect(pg["dsn"])
    conn.autocommit = True
    exec_sql_file(conn, Path("schema.sql"))
    conn.close()
    
    with localstack_context() as ls:
        with deploy("myapp", "dev", my_pulumi_program, localstack=ls) as outputs:
            api_url = outputs['api_url']
            print(f"API deployed at: {api_url}")
            
            # Start your frontend dev server with the API URL
            subprocess.run([
                "npm", "run", "dev"
            ], env={
                "VITE_API_URL": api_url,
                **os.environ
            })
```

This pattern is incredibly powerful for full-stack development. Your backend infrastructure spins up automatically, your frontend gets the correct API endpoint, and everything tears down cleanly when you're done.

## The Connection Pattern That Changes Everything

One of the most elegant aspects of Fixture Foundry is how it handles container networking. The library automatically:

1. Creates a shared Docker bridge network
2. Connects all containers to it
3. Sets `LAMBDA_DOCKER_NETWORK` for LocalStack
4. Provides both container names (for inter-container communication) and host ports (for your test code)

This means your Lambda functions can connect to PostgreSQL using `container_name:5432`, while your test code connects using `localhost:random_port`. No more networking headaches.

## Real-World Benefits

After adopting Fixture Foundry in our team:

- **Setup time dropped from 30 minutes to 30 seconds** - No more Docker Compose debugging sessions
- **Test reliability improved dramatically** - Proper cleanup and health checks eliminate flaky tests
- **Developer onboarding simplified** - New team members can run integration tests immediately
- **CI/CD became faster** - Parallel test execution with proper isolation
- **Development workflow improved** - Same infrastructure for testing and local development

## Key Patterns to Remember

### 1. Always Use Session Scope for Infrastructure
```python
@pytest.fixture(scope="session")
def my_database(postgres):
    # Infrastructure setup is expensive - do it once per test session
```

### 2. Leverage Container Networking
```python
# In your Pulumi program, use container names for inter-container communication
"host": database["container_name"]  # Not localhost!
```

### 3. Set autocommit for SQL Files
```python
conn.autocommit = True  # Required for multi-statement SQL scripts
exec_sql_file(conn, schema_file)
```

### 4. Use URL Translation for API Tests
```python
localstack_url = to_localstack_url(aws_gateway_url, localstack["port"])
```

## Looking Forward

Integration testing doesn't have to be painful. With the right abstractions, you can have:

- Tests that actually test your real infrastructure
- Development environments that mirror production
- Onboarding that takes minutes, not hours
- CI/CD that developers trust

Fixture Foundry provides these abstractions without hiding the underlying tools. You still use pytest, Docker, Pulumi, and LocalStack - you just don't have to orchestrate them manually anymore.

The result is integration tests that are fast, reliable, and actually enjoyable to write. And in my experience, when tests are enjoyable to write, developers write more of them.

---

*Fixture Foundry is open source and available on [PyPI](https://pypi.org/project/fixture-foundry/) and [GitHub](https://github.com/DanRepik/fixture_foundry). The library works on Python 3.8-3.12 and requires Docker.*

*Have you tried Fixture Foundry in your projects? I'd love to hear about your experience in the comments below.*