#!/usr/bin/env bash
# Reads the public URL of the Cloudflare quick tunnel and stores Workflow B's
# webhook URL in SSM, where the ingest Lambda looks it up. Run it after every
# `docker compose up` (quick-tunnel URLs change when the tunnel restarts).
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

url=""
for _ in $(seq 1 30); do
  url="$(docker compose logs tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [ -n "$url" ] && break
  sleep 2
done
[ -n "$url" ] || { echo "No tunnel URL found. Is the tunnel container running?" >&2; exit 1; }

webhook="$url/webhook/loan-engine/batch-ingested"
aws ssm put-parameter --region "$AWS_REGION" --name /loan-engine/n8n-webhook-url \
  --type String --value "$webhook" --overwrite >/dev/null
echo "Lambda will call: $webhook"
