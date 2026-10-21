# Shared helpers for the scripts in this folder. Source it, don't run it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
STACK_NAME="loan-eligibility-engine-dev"

# Stop Git Bash on Windows from rewriting /paths in arguments to docker and aws.
export MSYS_NO_PATHCONV=1

[ -f "$ENV_FILE" ] || cp "$REPO_ROOT/.env.example" "$ENV_FILE"

# Add any keys that exist in .env.example but not yet in .env (keeps existing values).
while IFS= read -r line; do
  key="${line%%=*}"
  [[ "$line" =~ ^[A-Z0-9_]+= ]] || continue
  grep -q "^${key}=" "$ENV_FILE" || echo "$line" >> "$ENV_FILE"
done < "$REPO_ROOT/.env.example"

load_env() {
  set -a
  # shellcheck disable=SC1090
  source <(sed -e 's/\r$//' "$ENV_FILE")
  set +a
}

# set_env KEY VALUE : write KEY=VALUE into .env (replacing any existing line).
set_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" || true
  echo "${key}=${value}" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  export "$key=$value"
}

random_secret() {
  node -e "console.log(require('crypto').randomBytes(${1:-24}).toString('base64url'))"
}

# Fill a secret only if it is empty or still the example placeholder.
ensure_secret() {
  local key="$1" current="${!1:-}"
  if [ -z "$current" ] || [ "$current" = "change-me" ]; then
    set_env "$key" "$(random_secret "${2:-24}")"
    echo "Generated $key"
  fi
}

stack_output() {
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

load_env
