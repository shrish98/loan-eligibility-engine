#!/usr/bin/env bash
# One-command AWS deployment: secrets -> Lambda layer -> CloudFormation stack ->
# database schema -> SES identities -> UI config.
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

bash scripts/setup-aws-params.sh
bash scripts/build-lambda-layer.sh
npx serverless deploy --region "$AWS_REGION"
bash scripts/init-rds.sh
bash scripts/setup-ses.sh

api_url="$(stack_output HttpApiUrl)"
cat > ui/config.js <<EOF
// Written by scripts/deploy.sh from the CloudFormation stack outputs.
window.LOAN_ENGINE_API = "${api_url}";
EOF
echo "UI configured for $api_url"
