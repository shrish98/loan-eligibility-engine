import json
import urllib.error

import pytest

from loan_engine import config, n8n


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def webhook(monkeypatch):
    """Fake webhook: fails `failures` times, then accepts. Returns (requests, sleeps)."""
    secrets = {"N8N_WEBHOOK_URL": "https://tunnel.example/webhook/match", "N8N_WEBHOOK_SECRET": "s3cret"}
    monkeypatch.setattr(n8n.config, "secret", lambda name, param, ttl_seconds=300: secrets[name])
    sleeps: list[float] = []
    monkeypatch.setattr(n8n.time, "sleep", sleeps.append)
    requests = []

    def install(failures: int):
        def urlopen(request, timeout):
            requests.append(request)
            if len(requests) <= failures:
                raise urllib.error.URLError("connection refused")
            return Response()

        monkeypatch.setattr(n8n.urllib.request, "urlopen", urlopen)
        return requests, sleeps

    return install


def test_webhook_sends_the_batch_and_the_shared_secret(webhook):
    requests, sleeps = webhook(failures=0)

    n8n.trigger_matching("batch-1")

    [request] = requests
    assert request.full_url == "https://tunnel.example/webhook/match"
    assert request.get_header("X-webhook-secret") == "s3cret"
    assert json.loads(request.data) == {"batch_id": "batch-1"}
    assert sleeps == []


def test_webhook_retries_with_exponential_backoff(webhook):
    requests, sleeps = webhook(failures=2)

    n8n.trigger_matching("batch-1")

    assert len(requests) == 3
    assert sleeps == [2, 4]


def test_webhook_gives_up_after_the_last_attempt(webhook):
    requests, sleeps = webhook(failures=99)

    with pytest.raises(urllib.error.URLError):
        n8n.trigger_matching("batch-1")

    assert len(requests) == 3
    assert sleeps == [2, 4]


class FakeSsm:
    def __init__(self):
        self.calls = 0

    def get_parameter(self, Name, WithDecryption):
        self.calls += 1
        return {"Parameter": {"Value": f"{Name}-v{self.calls}"}}


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSsm()
    clock = [1000.0]
    monkeypatch.setattr(config, "_ssm", fake)
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    return fake, clock


def test_ssm_values_are_cached_until_the_ttl_expires(ssm):
    fake, clock = ssm

    assert config.ssm_param("/loan-engine/x", ttl_seconds=30) == "/loan-engine/x-v1"
    clock[0] += 29
    assert config.ssm_param("/loan-engine/x", ttl_seconds=30) == "/loan-engine/x-v1"
    clock[0] += 2
    assert config.ssm_param("/loan-engine/x", ttl_seconds=30) == "/loan-engine/x-v2"
    assert fake.calls == 2


def test_secret_prefers_an_environment_variable_for_local_runs(ssm, monkeypatch):
    fake, _ = ssm
    monkeypatch.setenv("DB_PASSWORD", "local-password")
    assert config.secret("DB_PASSWORD", "DB_PASSWORD_PARAM") == "local-password"
    assert fake.calls == 0


def test_secret_falls_back_to_ssm(ssm, monkeypatch):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("DB_PASSWORD_PARAM", "/loan-engine/db-password")
    assert config.secret("DB_PASSWORD", "DB_PASSWORD_PARAM") == "/loan-engine/db-password-v1"


def test_a_missing_setting_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(RuntimeError, match="NOT_SET_ANYWHERE"):
        config.env("NOT_SET_ANYWHERE")
