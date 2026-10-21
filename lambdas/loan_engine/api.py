"""HTTP API (API Gateway) Lambdas used by the upload UI.

  POST /uploads              -> create a batch and return a presigned S3 POST
  GET  /uploads/{batch_id}   -> batch status and per-stage numbers, for polling

The CSV itself never passes through API Gateway or Lambda: the browser uploads
it straight to S3, which avoids API Gateway's 10 MB payload and 30 s timeout
limits. S3 then triggers the ingest Lambda (ingest.py).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.config import Config

from . import config, db

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
URL_EXPIRY_SECONDS = 900

# Regional endpoint: a new bucket outside us-east-1 answers the global endpoint
# with a 307 redirect, which a browser form POST does not follow.
_region = os.environ.get("AWS_REGION", "ap-south-1")
s3 = boto3.client(
    "s3",
    region_name=_region,
    endpoint_url=f"https://s3.{_region}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


def create_upload(event, context):
    body = _json_body(event)
    filename = str(body.get("filename") or "users.csv")[:255]
    if not filename.lower().endswith(".csv"):
        return _response(400, {"error": "Please upload a .csv file."})
    size = body.get("size")
    if isinstance(size, int) and size > MAX_UPLOAD_BYTES:
        return _response(413, {"error": f"File is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB."})

    batch_id = str(uuid.uuid4())
    key = f"uploads/{batch_id}.csv"

    with db.connect() as conn:
        conn.run(
            "INSERT INTO upload_batches (batch_id, s3_key, original_name) "
            "VALUES (CAST(:batch_id AS UUID), :key, :name)",
            batch_id=batch_id, key=key, name=filename,
        )

    # A presigned POST (unlike a presigned PUT) lets S3 itself enforce the
    # maximum file size and content type.
    upload = s3.generate_presigned_post(
        Bucket=config.env("UPLOAD_BUCKET"),
        Key=key,
        Fields={"Content-Type": "text/csv"},
        Conditions=[{"Content-Type": "text/csv"}, ["content-length-range", 1, MAX_UPLOAD_BYTES]],
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    log.info("Created batch %s for %s", batch_id, filename)
    return _response(201, {"batch_id": batch_id, "upload": upload})


def get_batch(event, context):
    batch_id = (event.get("pathParameters") or {}).get("batch_id", "")
    try:
        batch_id = str(uuid.UUID(batch_id))
    except ValueError:
        return _response(400, {"error": "Invalid batch id."})

    with db.connect() as conn:
        rows = db.rows_as_dicts(
            conn,
            """SELECT b.batch_id, b.original_name, b.status, b.row_count, b.rejected_count,
                      b.error_message, b.stats, b.created_at, b.updated_at,
                      (SELECT count(*) FROM matches m WHERE m.batch_id = b.batch_id) AS match_count,
                      (SELECT count(DISTINCT user_id) FROM matches m WHERE m.batch_id = b.batch_id) AS matched_users,
                      (SELECT count(DISTINCT user_id) FROM matches m
                        WHERE m.batch_id = b.batch_id AND m.notified_at IS NOT NULL) AS notified_users
               FROM upload_batches b WHERE b.batch_id = CAST(:batch_id AS UUID)""",
            batch_id=batch_id,
        )
    if not rows:
        return _response(404, {"error": "Batch not found."})
    return _response(200, rows[0])


def _json_body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _response(status: int, payload: dict) -> dict:
    # CORS headers are added by API Gateway (see httpApi.cors in serverless.yml).
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_default),
    }


def _json_default(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"Not JSON serialisable: {type(value)}")
