"""Shared test setup.

The Lambda modules create boto3 clients at import time. Dummy credentials and a
region let those clients be built (and presigned posts be signed) offline, with
no AWS account. Nothing in the tests talks to AWS or Postgres.
"""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("UPLOAD_BUCKET", "loan-engine-uploads-test")
