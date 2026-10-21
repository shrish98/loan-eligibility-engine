"""S3-triggered Lambda: stream an uploaded CSV into Postgres, then start matching.

Flow for one file (s3://<bucket>/uploads/<batch_id>.csv):
  1. Claim the batch (pending/failed -> processing). A duplicate S3 event for
     a batch that is already processing or done is ignored.
  2. Stream the object line by line; validate each row (csv_rows.py).
  3. COPY valid rows into a temporary staging table in chunks, so memory use
     does not grow with file size and inserts stay fast.
  4. Upsert staging -> users in one statement (last row wins for duplicate
     user_ids inside the file).
  5. Write rejected rows with their reasons to s3://<bucket>/rejects/<batch_id>.csv.
  6. Mark the batch 'ingested' and call n8n Workflow B's webhook.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import time
import urllib.parse
import uuid

import boto3

from . import config, db, n8n
from .csv_rows import CsvFormatError, RowError, normalise_header, parse_row

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")

COPY_CHUNK_ROWS = 5000

STAGING_DDL = """
CREATE TEMP TABLE staging_users (
    user_id UUID, name TEXT, email TEXT, monthly_income NUMERIC(12,2),
    credit_score INT, employment_status TEXT, age INT, line_no BIGSERIAL
) ON COMMIT DROP
"""

UPSERT_SQL = """
INSERT INTO users (user_id, name, email, monthly_income, credit_score,
                   employment_status, age, batch_id)
SELECT DISTINCT ON (user_id)
       user_id, NULLIF(name, ''), email, monthly_income, credit_score,
       employment_status, age, CAST(:batch_id AS UUID)
FROM staging_users
ORDER BY user_id, line_no DESC
ON CONFLICT (user_id) DO UPDATE SET
    name = EXCLUDED.name,
    email = EXCLUDED.email,
    monthly_income = EXCLUDED.monthly_income,
    credit_score = EXCLUDED.credit_score,
    employment_status = EXCLUDED.employment_status,
    age = EXCLUDED.age,
    batch_id = EXCLUDED.batch_id,
    updated_at = now()
"""


def handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        process_object(bucket, key)


def batch_id_from_key(key: str) -> str:
    """uploads/<uuid>.csv -> <uuid>. Files dropped in S3 by hand get a stable id."""
    stem = key.rsplit("/", 1)[-1].removesuffix(".csv")
    try:
        return str(uuid.UUID(stem))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def process_object(bucket: str, key: str) -> None:
    batch_id = batch_id_from_key(key)
    started = time.monotonic()

    with db.connect() as conn:
        if not _claim_batch(conn, batch_id, key):
            log.info("Batch %s already claimed or finished; ignoring duplicate event", batch_id)
            return

        try:
            stats = _load_file(conn, bucket, key, batch_id)
        except CsvFormatError as exc:
            # Retrying cannot fix a bad file, so record it and stop.
            _fail(conn, batch_id, str(exc))
            return
        except Exception as exc:
            _fail(conn, batch_id, f"Ingest failed: {exc}")
            raise  # lets Lambda retry, then send the event to the failure queue

        stats["duration_ms"] = int((time.monotonic() - started) * 1000)
        conn.run(
            """UPDATE upload_batches
               SET status = 'ingested', row_count = :rows, rejected_count = :rejected,
                   stats = stats || jsonb_build_object('ingest', CAST(:stats AS JSONB)),
                   updated_at = now()
               WHERE batch_id = :batch_id""",
            rows=stats["stored"],
            rejected=stats["rejected"],
            stats=json.dumps(stats),
            batch_id=batch_id,
        )
        log.info("Batch %s ingested: %s", batch_id, stats)

        try:
            n8n.trigger_matching(batch_id)
        except Exception as exc:
            # Users are safely stored; only the hand-off failed. Surface it in the UI.
            conn.run(
                "UPDATE upload_batches SET error_message = :msg, updated_at = now() WHERE batch_id = :batch_id",
                msg=f"Users stored, but n8n could not be reached: {exc}",
                batch_id=batch_id,
            )
            log.error("Could not trigger n8n for batch %s: %s", batch_id, exc)


def _claim_batch(conn, batch_id: str, key: str) -> bool:
    """Atomically move the batch to 'processing'. Returns False if another run owns it."""
    rows = conn.run(
        """INSERT INTO upload_batches (batch_id, s3_key, status)
           VALUES (CAST(:batch_id AS UUID), :key, 'processing')
           ON CONFLICT (batch_id) DO UPDATE
               SET status = 'processing', error_message = NULL, updated_at = now()
               WHERE upload_batches.status IN ('pending', 'failed')
           RETURNING batch_id""",
        batch_id=batch_id,
        key=key,
    )
    return bool(rows)


def _load_file(conn, bucket: str, key: str, batch_id: str) -> dict:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    # utf-8-sig drops the byte-order mark that Excel adds to exported CSVs.
    reader = csv.DictReader(codecs.getreader("utf-8-sig")(body))
    header_map = normalise_header(reader.fieldnames)

    rejects = io.StringIO()
    rejects_writer = csv.writer(rejects)
    rejects_writer.writerow(["line", "reason", "raw"])

    chunk = io.StringIO()
    chunk_writer = csv.writer(chunk)
    valid = rejected = chunk_rows = 0

    with db.transaction(conn):
        conn.run(STAGING_DDL)
        for line_no, raw in enumerate(reader, start=2):  # line 1 is the header
            try:
                row = parse_row(raw, header_map)
            except RowError as exc:
                rejected += 1
                rejects_writer.writerow([line_no, str(exc), json.dumps(raw, ensure_ascii=False)])
                continue
            chunk_writer.writerow(row.as_copy_fields())
            valid += 1
            chunk_rows += 1
            if chunk_rows >= COPY_CHUNK_ROWS:
                _copy_chunk(conn, chunk)
                chunk, chunk_rows = io.StringIO(), 0
                chunk_writer = csv.writer(chunk)
        if chunk_rows:
            _copy_chunk(conn, chunk)

        conn.run(UPSERT_SQL, batch_id=batch_id)
        stored = conn.row_count

    stats = {"valid_rows": valid, "stored": stored, "rejected": rejected,
             "duplicates_in_file": valid - stored}
    if rejected:
        rejects_key = f"rejects/{batch_id}.csv"
        s3.put_object(Bucket=bucket, Key=rejects_key, Body=rejects.getvalue().encode(),
                      ContentType="text/csv")
        stats["rejects_key"] = rejects_key
    return stats


def _copy_chunk(conn, chunk: io.StringIO) -> None:
    chunk.seek(0)
    conn.run(
        "COPY staging_users (user_id, name, email, monthly_income, credit_score, "
        "employment_status, age) FROM STDIN WITH (FORMAT csv)",
        stream=chunk,
    )


def _fail(conn, batch_id: str, message: str) -> None:
    log.error("Batch %s failed: %s", batch_id, message)
    conn.run(
        "UPDATE upload_batches SET status = 'failed', error_message = :msg, updated_at = now() "
        "WHERE batch_id = :batch_id",
        msg=message[:2000],
        batch_id=batch_id,
    )
