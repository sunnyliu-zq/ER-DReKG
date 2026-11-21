"""Collaboration-center authentication using only the Python standard library.

No third-party auth/JWT/bcrypt dependencies are added. Passwords are stored as
PBKDF2-HMAC-SHA256 hashes; session tokens are HMAC-signed and carry an expiry.
This module is independent from the research KG data and existing APIs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.models import CollabMember

_PBKDF2_ITERATIONS = 390_000
_PBKDF2_PREFIX = "pbkdf2_sha256"
_DEV_SECRET = "dev-only-insecure-collab-secret-change-in-production"
_TOKEN_SEPARATOR = "."

_INVALID_CREDENTIALS = HTTPException(status_code=401, detail="登录凭证无效或已过期", headers={"WWW-Authenticate": "Bearer"})


def _get_secret() -> str:
    secret = settings.collab_secret_key
    if secret:
        return secret
    if settings.app_env.lower() == "production":
        raise RuntimeError("COLLAB_SECRET_KEY must be set in production")
    return _DEV_SECRET


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_PREFIX}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, iterations, salt_hex, hash_hex = stored.split("$")
        if prefix != _PBKDF2_PREFIX:
            return False
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_access_token(member: CollabMember) -> str:
    import json

    expires_at = int(time.time()) + settings.collab_token_ttl_minutes * 60
    payload = {"sub": member.member_id, "username": member.username, "exp": expires_at}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_get_secret().encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}{_TOKEN_SEPARATOR}{_b64url_encode(signature)}"


def _decode_token(token: str) -> dict:
    try:
        payload_b64, signature_b64 = token.split(_TOKEN_SEPARATOR, 1)
    except ValueError:
        raise _INVALID_CREDENTIALS
    expected = hmac.new(_get_secret().encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_decode(signature_b64), expected):
        raise _INVALID_CREDENTIALS
    import json

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _INVALID_CREDENTIALS
    if int(payload.get("exp", 0)) < int(time.time()):
        raise _INVALID_CREDENTIALS
    return payload


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise _INVALID_CREDENTIALS
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _INVALID_CREDENTIALS
    return token.strip()


def get_current_member(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> CollabMember:
    token = _extract_bearer(authorization)
    payload = _decode_token(token)
    member = db.get(CollabMember, payload.get("sub"))
    if member is None or not member.is_active:
        raise _INVALID_CREDENTIALS
    return member
