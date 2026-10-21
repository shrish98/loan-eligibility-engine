#!/usr/bin/env bash
# 1. Asks SES to verify the sender and every test-inbox alias (SES sandbox only
#    delivers to verified addresses). Each gets a verification email: click the link.
# 2. Creates an access key for the send-only IAM user and writes it to .env,
#    from where n8n-bootstrap.sh loads it into n8n's encrypted credential store.
source "$(dirname "$0")/lib.sh"

if [[ "$SES_FROM_EMAIL" == you@* || -z "$SES_FROM_EMAIL" ]]; then
  echo "Set SES_FROM_EMAIL and TEST_INBOX_ALIASES in .env first." >&2
  exit 1
fi

IFS=',' read -ra aliases <<< "$TEST_INBOX_ALIASES"
for email in "$SES_FROM_EMAIL" "${aliases[@]}"; do
  email="$(echo "$email" | xargs)"
  status="$(aws sesv2 get-email-identity --region "$AWS_REGION" --email-identity "$email" \
            --query VerifiedForSendingStatus --output text 2>/dev/null || echo missing)"
  case "$status" in
    True)    echo "verified   $email" ;;
    missing) aws sesv2 create-email-identity --region "$AWS_REGION" --email-identity "$email" >/dev/null
             echo "requested  $email  -> click the link in the email from AWS" ;;
    *)       echo "pending    $email  -> still waiting for the verification link to be clicked" ;;
  esac
done

if [ -z "${SES_ACCESS_KEY_ID:-}" ]; then
  user="$(stack_output SesSenderUserName)"
  read -r key_id secret_key < <(aws iam create-access-key --user-name "$user" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
  set_env SES_ACCESS_KEY_ID "$key_id"
  set_env SES_SECRET_ACCESS_KEY "$secret_key"
  echo "Created access key for IAM user $user (saved to .env)"
fi
