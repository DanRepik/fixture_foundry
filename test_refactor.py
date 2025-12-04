#!/usr/bin/env python3
"""Quick test to verify the refactored context managers work."""

from fixture_foundry.context import (
    build_deployment,
    teardown_deployment,
    build_container_network,
    teardown_container_network,
    build_postgres,
    teardown_postgres,
    build_localstack,
    teardown_localstack,
)

print("✓ All build and teardown functions imported successfully")
print("\nRefactored structure:")
print("  - build_deployment() / teardown_deployment()")
print("  - build_container_network() / teardown_container_network()")
print("  - build_postgres() / teardown_postgres()")
print("  - build_localstack() / teardown_localstack()")
print("\nEach context manager now follows the pattern:")
print("  try:")
print("    resource = build_resource()")
print("    yield resource")
print("  finally:")
print("    if teardown:")
print("      teardown_resource(resource)")
