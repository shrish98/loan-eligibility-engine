"""In-memory stand-ins for Postgres and S3, so Lambda logic is tested without AWS."""

from __future__ import annotations

import csv
import io
from contextlib import contextmanager


class FakeConn:
    """Records every statement; answers the few the Lambdas read results from."""

    def __init__(self, claim_succeeds: bool = True, batch_rows: list[dict] | None = None):
        self.claim_succeeds = claim_succeeds
        self.batch_rows = batch_rows or []
        self.statements: list[tuple[str, dict]] = []
        self.copied_rows: list[list[str]] = []
        self.copy_calls = 0
        self.row_count = 0
        self.columns = None

    def run(self, sql: str, stream=None, **params):
        self.statements.append((sql, params))
        if "INSERT INTO upload_batches" in sql and "RETURNING" in sql:
            return [[params["batch_id"]]] if self.claim_succeeds else []
        if sql.startswith("COPY"):
            self.copy_calls += 1
            self.copied_rows.extend(csv.reader(io.StringIO(stream.getvalue())))
            return []
        if "INSERT INTO users" in sql:
            # The real upsert keeps one row per user_id.
            self.row_count = len({row[0] for row in self.copied_rows})
            return []
        if "FROM upload_batches b" in sql:
            if not self.batch_rows:
                self.columns = []
                return []
            self.columns = [{"name": name} for name in self.batch_rows[0]]
            return [list(row.values()) for row in self.batch_rows]
        return []

    def close(self):
        pass

    def sql_containing(self, fragment: str) -> list[tuple[str, dict]]:
        return [(sql, params) for sql, params in self.statements if fragment in sql]


def connect_returning(conn: FakeConn):
    @contextmanager
    def _connect():
        yield conn

    return _connect


class FakeS3:
    def __init__(self, files: dict[str, bytes] | None = None, get_error: Exception | None = None):
        self.files = files or {}
        self.get_error = get_error
        self.put: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):
        if self.get_error:
            raise self.get_error
        return {"Body": io.BytesIO(self.files[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put[Key] = Body
