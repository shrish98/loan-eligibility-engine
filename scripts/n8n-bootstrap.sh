#!/usr/bin/env bash
# Loads credentials and workflows into the running n8n container.
#
# Credentials are built from .env, passed to `n8n import:credentials` (which
# encrypts them with N8N_ENCRYPTION_KEY) and the temporary file is deleted.
# The workflow JSON files reference these credentials by fixed IDs, so they
# work immediately after import. Re-running the script updates both in place.
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

# USE_LOCAL_DB=1 bash scripts/n8n-bootstrap.sh  -> point n8n at the local-db container instead of RDS.
if [ "${USE_LOCAL_DB:-}" = "1" ]; then
  export DB_HOST=postgres DB_PORT=5432 DB_NAME="$POSTGRES_DB" DB_USER="$POSTGRES_USER" \
         DB_PASSWORD="$POSTGRES_PASSWORD" DB_SSL=disable
fi

missing=()
for var in DB_HOST DB_PASSWORD GEMINI_API_KEY N8N_WEBHOOK_SECRET SES_ACCESS_KEY_ID SES_SECRET_ACCESS_KEY; do
  [ -n "${!var:-}" ] || missing+=("$var")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Warning: empty in .env: ${missing[*]} (the matching credentials will not work until set)" >&2
fi

creds_file="$(mktemp)"
trap 'rm -f "$creds_file"' EXIT
node - "$creds_file" <<'EOF'
const env = process.env;
const creds = [
  {
    id: 'pgLoanEngine0001', name: 'Loan Engine Postgres (RDS)', type: 'postgres',
    data: {
      host: env.DB_HOST, port: Number(env.DB_PORT || 5432), database: env.DB_NAME,
      user: env.DB_USER, password: env.DB_PASSWORD,
      ssl: env.DB_SSL === 'disable' ? 'disable' : 'require', allowUnauthorizedCerts: false,
    },
  },
  {
    id: 'awsSesSender0001', name: 'AWS SES sender', type: 'aws',
    data: { region: env.AWS_REGION, accessKeyId: env.SES_ACCESS_KEY_ID || '', secretAccessKey: env.SES_SECRET_ACCESS_KEY || '' },
  },
  {
    id: 'geminiApiKey0001', name: 'Gemini API key', type: 'httpHeaderAuth',
    data: { name: 'x-goog-api-key', value: env.GEMINI_API_KEY || '' },
  },
  {
    id: 'webhookSecret001', name: 'Ingest webhook secret', type: 'httpHeaderAuth',
    data: { name: 'X-Webhook-Secret', value: env.N8N_WEBHOOK_SECRET },
  },
];
require('fs').writeFileSync(process.argv[2], JSON.stringify(creds));
EOF

docker compose cp "$creds_file" n8n:/tmp/credentials.json >/dev/null
docker compose exec -T n8n n8n import:credentials --input=/tmp/credentials.json
docker compose exec -T -u root n8n rm -f /tmp/credentials.json

docker compose exec -T n8n n8n import:workflow --separate --input=/workflows
for id in LoanCrawlerWfA01 LoanMatcherWfB01 LoanNotifyWfC001; do
  docker compose exec -T n8n n8n publish:workflow --id="$id"
done

# Published workflows (schedule + webhook triggers) are picked up on start.
docker compose restart n8n >/dev/null
for _ in $(seq 1 60); do curl -sf http://localhost:5678/healthz >/dev/null && break; sleep 2; done

# Create the n8n login on first run (later runs get "already set up" and do nothing).
[ -n "${N8N_OWNER_PASSWORD:-}" ] || set_env N8N_OWNER_PASSWORD "Ln8n-$(random_secret 12)"
curl -s -o /dev/null -X POST http://localhost:5678/rest/owner/setup -H 'Content-Type: application/json' \
  -d "{\"email\":\"$N8N_OWNER_EMAIL\",\"firstName\":\"Loan\",\"lastName\":\"Engine\",\"password\":\"$N8N_OWNER_PASSWORD\"}" || true

echo "n8n ready at http://localhost:5678 (workflows imported and published)"
echo "Log in with $N8N_OWNER_EMAIL and the N8N_OWNER_PASSWORD from .env"
