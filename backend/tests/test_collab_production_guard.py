"""Stage Collab-Security-1: production startup guard for COLLAB_SECRET_KEY.

Covers the seven required behaviors:
- production + missing key  -> startup fails (import-time guard);
- production + empty/whitespace key -> startup fails;
- production + valid key    -> startup succeeds;
- production + public dev constant -> startup fails;
- development + missing key -> existing dev convenience preserved;
- test environment          -> guard is a no-op;
- token signing/verification round-trips in dev and production modes.

All secret values here are obviously fake unit-test constants. No real API,
database, or network calls are made; subprocess checks only import the config
module with overridden environment variables.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.collab_auth import (
    _DEV_SECRET,
    _decode_token,
    create_access_token,
    hash_password,
    verify_password,
)
from app.core.config import Settings, settings, validate_production_settings
from app.models import CollabMember

BACKEND_DIR = Path(__file__).resolve().parents[1]
FAKE_KEY = "unit-test-fake-secret-not-a-real-key"


def _settings(**kw) -> Settings:
    """Isolated Settings: ignore the real root .env; init kwargs win over env."""
    return Settings(_env_file=None, **kw)


def _member() -> CollabMember:
    return CollabMember(
        member_id="member-test-1",
        username="tester",
        password_hash=hash_password("fake-password"),
        display_name="Tester",
        role="member",
        is_active=True,
    )


# --- guard unit behavior ---------------------------------------------------


def test_production_missing_key_fails(monkeypatch):
    monkeypatch.delenv("COLLAB_SECRET_KEY", raising=False)
    s = _settings(app_env="production")
    with pytest.raises(RuntimeError, match="COLLAB_SECRET_KEY"):
        validate_production_settings(s)


def test_production_empty_key_fails():
    s = _settings(app_env="production", collab_secret_key="")
    with pytest.raises(RuntimeError, match="COLLAB_SECRET_KEY"):
        validate_production_settings(s)


def test_production_whitespace_key_fails():
    s = _settings(app_env="production", collab_secret_key="   \t ")
    with pytest.raises(RuntimeError, match="COLLAB_SECRET_KEY"):
        validate_production_settings(s)


def test_production_valid_key_starts():
    s = _settings(app_env="production", collab_secret_key=FAKE_KEY)
    assert validate_production_settings(s) is None


def test_production_public_dev_secret_is_rejected():
    s = _settings(app_env="production", collab_secret_key=_DEV_SECRET)
    with pytest.raises(RuntimeError, match="public development") as excinfo:
        validate_production_settings(s)
    assert _DEV_SECRET not in str(excinfo.value)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_non_production_public_dev_secret_is_allowed(app_env):
    s = _settings(app_env=app_env, collab_secret_key=_DEV_SECRET)
    assert validate_production_settings(s) is None


def test_production_env_case_and_whitespace_normalized():
    s = _settings(app_env=" Production ", collab_secret_key="")
    with pytest.raises(RuntimeError):
        validate_production_settings(s)


def test_development_missing_key_keeps_dev_behavior(monkeypatch):
    monkeypatch.delenv("COLLAB_SECRET_KEY", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    assert validate_production_settings(_settings(app_env="development")) is None
    # default app_env (development) must also stay convenient
    assert validate_production_settings(_settings()) is None


def test_test_env_missing_key_is_noop(monkeypatch):
    monkeypatch.delenv("COLLAB_SECRET_KEY", raising=False)
    assert validate_production_settings(_settings(app_env="test")) is None


def test_error_message_never_contains_key_value():
    secret_like = " \t "
    s = _settings(app_env="production", collab_secret_key=secret_like)
    with pytest.raises(RuntimeError) as excinfo:
        validate_production_settings(s)
    message = str(excinfo.value)
    assert "COLLAB_SECRET_KEY" in message
    assert secret_like not in message
    assert FAKE_KEY not in message


# --- real import-time enforcement (subprocess, no server needed) -----------


@pytest.mark.parametrize(
    "app_env,secret,expect_success",
    [
        ("production", "", False),
        ("production", FAKE_KEY, True),
        ("development", "", True),
    ],
)
def test_config_import_enforces_guard(app_env, secret, expect_success):
    env = os.environ.copy()
    env["APP_ENV"] = app_env
    env["COLLAB_SECRET_KEY"] = secret
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", "import app.core.config"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if expect_success:
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0
        assert "RuntimeError" in proc.stderr
        assert "COLLAB_SECRET_KEY" in proc.stderr
        # the failing message must not leak any key material
        assert FAKE_KEY not in proc.stderr


def test_config_import_rejects_public_dev_secret_in_production():
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["COLLAB_SECRET_KEY"] = _DEV_SECRET
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", "import app.core.config"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "RuntimeError" in proc.stderr
    assert "COLLAB_SECRET_KEY" in proc.stderr
    assert "public development" in proc.stderr
    # the public constant is the rejected value, not something to echo back
    assert _DEV_SECRET not in proc.stderr


# --- token signing / verification ------------------------------------------


def test_token_roundtrip_development_fallback(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "collab_secret_key", "")
    member = _member()
    token = create_access_token(member)
    payload = _decode_token(token)
    assert payload["sub"] == member.member_id
    assert payload["username"] == member.username
    assert payload["exp"] > 0


def test_token_roundtrip_production_with_key(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "collab_secret_key", FAKE_KEY)
    member = _member()
    token = create_access_token(member)
    payload = _decode_token(token)
    assert payload["sub"] == member.member_id


def test_token_production_without_key_refuses_to_sign(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "collab_secret_key", "")
    with pytest.raises(RuntimeError, match="COLLAB_SECRET_KEY"):
        create_access_token(_member())


def test_password_hash_roundtrip_and_reject_wrong():
    stored = hash_password("fake-password")
    assert stored.startswith("pbkdf2_sha256$")
    assert "fake-password" not in stored
    assert verify_password("fake-password", stored) is True
    assert verify_password("wrong-password", stored) is False