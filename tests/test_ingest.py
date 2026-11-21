import csv
import io
import json
import uuid

import pytest
from fakes import FakeConn, FakeS3, connect_returning

from loan_engine import ingest

BATCH_ID = "7d0f8f8e-3b7a-4c55-9d43-2f1f4c9c1a01"
KEY = f"uploads/{BATCH_ID}.csv"
HEADER = "user_id,name,email,monthly_income,credit_score,employment_status,age\n"
USER_A = "b35de23e-755f-4c6a-81d8-f7dd99ce53ef"
USER_B = "0c6a3c8e-2f0e-4f0e-9a51-3c1f0d1a2b3c"


def csv_bytes(*rows: str, header: str = HEADER) -> bytes:
    return (header + "".join(row + "\n" for row in rows)).encode("utf-8")


def good_row(user_id: str = USER_A, income: str = "50000", email: str = "a@example.com") -> str:
    return f"{user_id},Asha,{email},{income},720,Salaried,30"


@pytest.fixture
def env(monkeypatch):
    """Wire the ingest module to fakes; returns (conn, s3, triggered batch ids)."""

    def setup(file: bytes | None = None, conn: FakeConn | None = None, s3: FakeS3 | None = None,
              trigger_error: Exception | None = None):
        conn = conn or FakeConn()
        s3 = s3 or FakeS3({KEY: file or b""})
        triggered: list[str] = []

        def trigger(batch_id):
            if trigger_error:
                raise trigger_error
            triggered.append(batch_id)

        monkeypatch.setattr(ingest.db, "connect", connect_returning(conn))
        monkeypatch.setattr(ingest, "s3", s3)
        monkeypatch.setattr(ingest.n8n, "trigger_matching", trigger)
        return conn, s3, triggered

    return setup


def status_updates(conn: FakeConn) -> list[dict]:
    return [params for _, params in conn.sql_containing("UPDATE upload_batches")]


def test_batch_id_comes_from_the_object_key():
    assert ingest.batch_id_from_key(KEY) == BATCH_ID


def test_hand_dropped_files_get_a_stable_batch_id():
    first = ingest.batch_id_from_key("uploads/march users.csv")
    assert first == ingest.batch_id_from_key("uploads/march users.csv")
    assert first != ingest.batch_id_from_key("uploads/april users.csv")
    uuid.UUID(first)


def test_valid_rows_are_copied_rejects_reported_and_matching_started(env):
    conn, s3, triggered = env(csv_bytes(
        good_row(USER_A),
        good_row(USER_B, email="not-an-email"),
        good_row(USER_B, income="61000", email="b@example.com"),
    ))

    ingest.process_object("bucket", KEY)

    assert [row[0] for row in conn.copied_rows] == [USER_A, USER_B]
    rejects = list(csv.reader(io.StringIO(s3.put[f"rejects/{BATCH_ID}.csv"].decode())))
    assert rejects[0] == ["line", "reason", "raw"]
    assert rejects[1][0] == "3"  # line numbers count the header as line 1
    assert "invalid email" in rejects[1][1]

    final = status_updates(conn)[-1]
    stats = json.loads(final["stats"])
    assert (final["rows"], final["rejected"]) == (2, 1)
    assert stats["rejects_key"] == f"rejects/{BATCH_ID}.csv"
    assert triggered == [BATCH_ID]


def test_a_user_repeated_in_the_file_is_stored_once(env):
    conn, _, _ = env(csv_bytes(good_row(USER_A, income="40000"), good_row(USER_A, income="45000")))

    ingest.process_object("bucket", KEY)

    stats = json.loads(status_updates(conn)[-1]["stats"])
    assert stats["valid_rows"] == 2
    assert stats["stored"] == 1
    assert stats["duplicates_in_file"] == 1


def test_large_files_are_copied_in_chunks(env, monkeypatch):
    monkeypatch.setattr(ingest, "COPY_CHUNK_ROWS", 2)
    rows = [good_row(str(uuid.uuid4()), email=f"u{i}@example.com") for i in range(5)]
    conn, s3, _ = env(csv_bytes(*rows))

    ingest.process_object("bucket", KEY)

    assert conn.copy_calls == 3  # 2 + 2 + 1
    assert len(conn.copied_rows) == 5
    assert s3.put == {}  # no rejects, no rejects file


def test_excel_byte_order_mark_is_ignored(env):
    conn, _, triggered = env(b"\xef\xbb\xbf" + csv_bytes(good_row()))
    ingest.process_object("bucket", KEY)
    assert len(conn.copied_rows) == 1
    assert triggered == [BATCH_ID]


def test_a_duplicate_s3_event_is_ignored(env):
    conn, s3, triggered = env(csv_bytes(good_row()), conn=FakeConn(claim_succeeds=False))

    ingest.process_object("bucket", KEY)

    assert conn.copy_calls == 0
    assert status_updates(conn) == []
    assert triggered == []


def test_a_file_missing_a_column_fails_the_batch_without_a_retry(env):
    conn, _, triggered = env(csv_bytes(header="user_id,email,monthly_income,employment_status,age\n"))

    ingest.process_object("bucket", KEY)  # does not raise, so Lambda will not retry

    [failed] = status_updates(conn)
    assert "credit_score" in failed["msg"]
    assert conn.copy_calls == 0
    assert triggered == []


def test_unexpected_errors_fail_the_batch_and_are_re_raised_for_lambda_retries(env):
    conn, _, triggered = env(s3=FakeS3(get_error=ConnectionError("S3 unavailable")))

    with pytest.raises(ConnectionError):
        ingest.process_object("bucket", KEY)

    [failed] = status_updates(conn)
    assert failed["msg"] == "Ingest failed: S3 unavailable"
    assert triggered == []


def test_users_stay_stored_when_n8n_cannot_be_reached(env):
    conn, _, _ = env(csv_bytes(good_row()), trigger_error=TimeoutError("tunnel down"))

    ingest.process_object("bucket", KEY)  # does not raise: the users are safely stored

    ingested, hand_off_error = status_updates(conn)
    assert ingested["rows"] == 1
    assert "n8n could not be reached" in hand_off_error["msg"]


def test_handler_decodes_url_encoded_keys(env, monkeypatch):
    seen = []
    monkeypatch.setattr(ingest, "process_object", lambda bucket, key: seen.append((bucket, key)))

    record = {"s3": {"bucket": {"name": "b"}, "object": {"key": "uploads/march+users%281%29.csv"}}}
    ingest.handler({"Records": [record]}, None)

    assert seen == [("b", "uploads/march users(1).csv")]
