"""Runtime settings for the Lambdas.

Plain settings come from environment variables set in serverless.yml. Secrets
(the DB password, the n8n webhook secret) and the n8n webhook URL live in SSM
Parameter Store and are read at runtime, so they never appear in the Lambda
console or in the CloudFormation template.
"""

from __future__ import annotations

import os
import time

import boto3

_ssm = None
_cache: dict[str, tuple[float, str]] = {}


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing environment variable {name}")
    return value


def ssm_param(name: str, ttl_seconds: int = 300) -> str:
    """Read a (possibly encrypted) SSM parameter, cached for ttl_seconds.

    The cache survives between invocations of a warm Lambda. The webhook URL
    uses a short TTL because the n8n tunnel URL can change between restarts.
    """
    now = time.monotonic()
    cached = _cache.get(name)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]

    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    value = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    _cache[name] = (now, value)
    return value


def secret(env_name: str, param_env_name: str, ttl_seconds: int = 300) -> str:
    """Return a secret from env var `env_name` if set (local runs), else from SSM."""
    direct = os.environ.get(env_name)
    if direct:
        return direct
    return ssm_param(env(param_env_name), ttl_seconds)
