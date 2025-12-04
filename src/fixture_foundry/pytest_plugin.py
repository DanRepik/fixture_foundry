"""
Pytest plugin for fixture_foundry with common CLI options.

This module provides a pytest_addoption function that can be imported
and used in other projects' conftest.py files.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

DEFAULT_IMAGE = "localstack/localstack:latest"
DEFAULT_SERVICES = "logs,iam,lambda,secretsmanager,apigateway,cloudwatch"


def pytest_addoption(parser: "pytest.Parser") -> None:
    """
    Add common CLI options for fixture_foundry tests.
    
    This function can be imported and called from other projects' conftest.py:
    
        from fixture_foundry.pytest_plugin import pytest_addoption
        
        # Use as-is or call it from your own pytest_addoption function
        pytest_addoption(parser)
    """
    group = parser.getgroup("localstack")
    
    group.addoption(
        "--teardown",
        action="store",
        default="true",
        help="Whether to tear down containers and stacks after tests (default: true). "
        "Set to 'false' to keep infrastructure running for faster development iteration.",
    )
    group.addoption(
        "--localstack-image",
        action="store",
        default=DEFAULT_IMAGE,
        help="Docker image to use for LocalStack",
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
        help="Port for LocalStack edge service (default: 0 = random)",
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
        help="Docker image to use for the database",
    )


def add_fixture_foundry_options(parser: "pytest.Parser") -> None:
    """
    Alias for pytest_addoption for clearer importing.
    """
    pytest_addoption(parser)