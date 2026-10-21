#!/usr/bin/env bash
# Generates the secrets (if .env doesn't have them yet) and stores them in SSM
# Parameter Store, where CloudFormation (DB password) and the Lambdas read them.
source "$(dirname "$0")/lib.sh"

ensure_secret DB_PASSWORD 18
ensure_secret N8N_WEBHOOK_SECRET 24

put_param() {
  aws ssm put-parameter --region "$AWS_REGION" --name "$1" --type "$2" --value "$3" --overwrite >/dev/null
  echo "SSM $1 ($2) saved"
}

put_param /loan-engine/db-password SecureString "$DB_PASSWORD"
put_param /loan-engine/n8n-webhook-secret SecureString "$N8N_WEBHOOK_SECRET"

# The real URL is written by publish-tunnel-url.sh once the tunnel is running.
if ! aws ssm get-parameter --region "$AWS_REGION" --name /loan-engine/n8n-webhook-url >/dev/null 2>&1; then
  put_param /loan-engine/n8n-webhook-url String "http://n8n-not-connected-yet.invalid"
fi
