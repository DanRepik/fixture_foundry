import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_REGION = os.environ.get(
    "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
)


def exec_sql_file(conn, sql_path: Path):
    """
    Execute a SQL script file against an open DB-API connection.

    Notes:
    - Reads the entire file and executes it in a single cursor.execute call.
    - Supports PostgreSQL DO $$ ... $$ blocks and multi-statement scripts.
    - Caller is responsible for transaction handling (e.g., conn.autocommit = True).

    Parameters:
      conn    : psycopg2 connection (or DB-API compatible).
      sql_path: Path to the .sql file to execute.
    """
    sql_text = sql_path.read_text(encoding="utf-8")
    # Execute entire script (supports DO $$ ... $$ blocks and multiple statements)
    with conn.cursor() as cur:
        cur.execute(sql_text)


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
    import re
    from urllib.parse import urlparse, urlunparse

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
