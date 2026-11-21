import base64
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fakes import FakeConn, connect_returning

from loan_engine import api


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConn()
    monkeypatch.setattr(api.db, "connect", connect_returning(fake))
    return fake


def post(body, base64_encoded=False):
    raw = json.dumps(body) if not isinstance(body, str) else body
    if base64_encoded:
        raw = base64.b64encode(raw.encode()).decode()
    return api.create_upload({"body": raw, "isBase64Encoded": base64_encoded}, None)


def payload(response):
    return json.loads(response["body"])


def test_create_upload_rejects_non_csv_without_touching_the_database(conn):
    response = post({"filename": "users.xlsx"})
    assert response["statusCode"] == 400
    assert conn.statements == []


def test_create_upload_rejects_files_over_the_size_cap(conn):
    response = post({"filename": "users.csv", "size": api.MAX_UPLOAD_BYTES + 1})
    assert response["statusCode"] == 413
    assert conn.statements == []


def test_create_upload_records_the_batch_and_signs_a_capped_post(conn):
    response = post({"filename": "march.csv", "size": 1024})
    assert response["statusCode"] == 201
    body = payload(response)
    batch_id = str(uuid.UUID(body["batch_id"]))

    [(_, params)] = conn.sql_containing("INSERT INTO upload_batches")
    assert params == {"batch_id": batch_id, "key": f"uploads/{batch_id}.csv", "name": "march.csv"}

    upload = body["upload"]
    assert upload["url"].startswith("https://")
    assert "s3.ap-south-1.amazonaws.com" in upload["url"]  # regional endpoint, no 307 redirect
    assert upload["fields"]["key"] == f"uploads/{batch_id}.csv"

    # S3 enforces these itself, from the signed policy.
    policy = json.loads(base64.b64decode(upload["fields"]["policy"]))
    assert ["content-length-range", 1, api.MAX_UPLOAD_BYTES] in policy["conditions"]
    assert {"Content-Type": "text/csv"} in policy["conditions"]


def test_create_upload_accepts_a_base64_body(conn):
    response = post({"filename": "users.csv"}, base64_encoded=True)
    assert response["statusCode"] == 201


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", ""])
def test_create_upload_falls_back_to_a_default_name_for_unreadable_bodies(conn, raw):
    response = post(raw)
    assert response["statusCode"] == 201
    [(_, params)] = conn.sql_containing("INSERT INTO upload_batches")
    assert params["name"] == "users.csv"


def test_get_batch_rejects_an_invalid_id_without_touching_the_database(conn):
    response = api.get_batch({"pathParameters": {"batch_id": "../../etc"}}, None)
    assert response["statusCode"] == 400
    assert conn.statements == []


def test_get_batch_returns_404_for_an_unknown_batch(conn):
    response = api.get_batch({"pathParameters": {"batch_id": str(uuid.uuid4())}}, None)
    assert response["statusCode"] == 404


def test_get_batch_serialises_database_types(monkeypatch):
    batch_id = uuid.uuid4()
    fake = FakeConn(batch_rows=[{
        "batch_id": batch_id,
        "status": "matched",
        "row_count": 10_000,
        "stats": {"llm_calls": 34},
        "created_at": datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc),
        "match_count": Decimal("125284"),
    }])
    monkeypatch.setattr(api.db, "connect", connect_returning(fake))

    response = api.get_batch({"pathParameters": {"batch_id": str(batch_id)}}, None)

    assert response["statusCode"] == 200
    body = payload(response)
    assert body["batch_id"] == str(batch_id)
    assert body["created_at"] == "2026-10-22T09:30:00+00:00"
    assert body["match_count"] == 125284
    assert body["stats"] == {"llm_calls": 34}
