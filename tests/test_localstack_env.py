from fixture_foundry.context import localstack_container_env


def test_auth_token_passed_through(monkeypatch):
    monkeypatch.setenv("LOCALSTACK_AUTH_TOKEN", "ls-test-token")
    env = localstack_container_env("lambda,iam", "test-net")
    assert env["LOCALSTACK_AUTH_TOKEN"] == "ls-test-token"
    assert env["SERVICES"] == "lambda,iam"
    assert env["LAMBDA_DOCKER_NETWORK"] == "test-net"


def test_auth_token_omitted_when_unset(monkeypatch):
    monkeypatch.delenv("LOCALSTACK_AUTH_TOKEN", raising=False)
    env = localstack_container_env("lambda,iam", "test-net")
    assert "LOCALSTACK_AUTH_TOKEN" not in env
