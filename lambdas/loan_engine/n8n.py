"""Calls from the Lambdas into the self-hosted n8n instance."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)


def trigger_matching(batch_id: str, attempts: int = 3) -> None:
    """POST to Workflow B's webhook so it matches the users of this batch.

    The webhook is protected by a shared secret sent in a header (n8n's
    "Header Auth" credential checks it). The URL is read from SSM on every
    cold start / cache expiry, so a restarted tunnel only needs an SSM update,
    not a redeploy.
    """
    url = config.secret("N8N_WEBHOOK_URL", "N8N_WEBHOOK_URL_PARAM", ttl_seconds=30)
    token = config.secret("N8N_WEBHOOK_SECRET", "N8N_WEBHOOK_SECRET_PARAM")
    body = json.dumps({"batch_id": batch_id}).encode()

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-Webhook-Secret": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                log.info("n8n webhook accepted batch %s (HTTP %s)", batch_id, response.status)
                return
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("n8n webhook attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)
