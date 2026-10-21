#!/usr/bin/env bash
# Builds the Lambda layer (.build/layer/python) and downloads the Amazon RDS CA
# bundle that the Lambdas and n8n use to verify the database's TLS certificate.
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

if [ -x .venv/Scripts/python ]; then PY=.venv/Scripts/python
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
else PY="${PYTHON:-python3}"; fi
[[ "$PY" == */* && "$PY" != /* ]] && PY="$REPO_ROOT/$PY"

rm -rf .build/layer
mkdir -p .build/layer/python
# Target the Lambda platform (Linux arm64, Python 3.12) regardless of this machine's OS.
"$PY" -m pip install --quiet -r lambdas/requirements.txt --target .build/layer/python \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all:
(cd .build/layer && rm -f ../layer.zip && "$PY" -m zipfile -c ../layer.zip python/)
echo "Layer built: .build/layer.zip"

mkdir -p certs
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o certs/rds-global-bundle.pem
mkdir -p lambdas/loan_engine/certs
cp certs/rds-global-bundle.pem lambdas/loan_engine/certs/
echo "RDS CA bundle saved to certs/"
