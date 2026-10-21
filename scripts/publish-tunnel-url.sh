#!/usr/bin/env bash
# Restarts the Cloudflare quick tunnel (quick tunnels expire after some hours)
# and stores Workflow B's webhook URL in SSM, where the ingest Lambda looks it
# up. Run it after `docker compose up` and before a demo.
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose up -d tunnel >/dev/null 2>&1
docker compose restart tunnel >/dev/null 2>&1

url=""
for _ in $(seq 1 30); do
  url="$(docker compose logs --since "$since" tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [ -n "$url" ] && break
  sleep 2
done
[ -n "$url" ] || { echo "No tunnel URL found. Check: docker compose logs tunnel" >&2; exit 1; }

webhook="$url/webhook/loan-engine/batch-ingested"
aws ssm put-parameter --region "$AWS_REGION" --name /loan-engine/n8n-webhook-url \
  --type String --value "$webhook" --overwrite >/dev/null

# Wait until the new public URL actually reaches n8n (403 = n8n answered and wanted the secret).
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$webhook" || true)"
  [ "$code" = "403" ] && break
  sleep 2
done
echo "Lambda will call: $webhook (public check: HTTP $code)"
