"""Postgres connections for the Lambdas.

Uses pg8000, a pure-Python driver: no compiled code, so the Lambda package can
be built on any OS without Docker. Connections to RDS are TLS-encrypted and the
server certificate is verified against the Amazon RDS CA bundle.
"""

from __future__ import annotations

import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pg8000.native

from . import config

# Downloaded by scripts/build-lambda-layer.sh from truststore.pki.rds.amazonaws.com.
RDS_CA_BUNDLE = Path(__file__).parent / "certs" / "rds-global-bundle.pem"


def _ssl_context() -> ssl.SSLContext | None:
    mode = config.env("DB_SSL", "verify-full")
    if mode == "disable":  # local Docker Postgres only
        return None
    ctx = ssl.create_default_context(cafile=str(RDS_CA_BUNDLE))
    ctx.check_hostname = mode == "verify-full"
    return ctx


@contextmanager
def connect() -> Iterator[pg8000.native.Connection]:
    conn = pg8000.native.Connection(
        host=config.env("DB_HOST"),
        port=int(config.env("DB_PORT", "5432")),
        database=config.env("DB_NAME"),
        user=config.env("DB_USER"),
        password=config.secret("DB_PASSWORD", "DB_PASSWORD_PARAM"),
        ssl_context=_ssl_context(),
        timeout=30,
        application_name="loan-engine-lambda",
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: pg8000.native.Connection) -> Iterator[None]:
    """pg8000's native API autocommits; wrap multi-statement work explicitly."""
    conn.run("START TRANSACTION")
    try:
        yield
    except BaseException:
        conn.run("ROLLBACK")
        raise
    conn.run("COMMIT")


def rows_as_dicts(conn: pg8000.native.Connection, sql: str, **params) -> list[dict]:
    rows = conn.run(sql, **params)
    columns = [c["name"] for c in conn.columns or []]
    return [dict(zip(columns, row)) for row in rows]
