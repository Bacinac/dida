from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime

import jwt
from fastapi import HTTPException, Request

from dida_api.auth import AuthUser, _token_hash, request_token

_AUDIENCE = "dida.oauth"
_TTL = 600


def _signing_key(secret: str) -> bytes:
    return hmac.digest(secret.encode(), b"dida:oauth-state:v1", "sha256")


async def issue_state(request: Request, user: AuthUser, integration: str) -> str:
    token = request_token(request)
    if user.role != "admin" or not token:
        raise HTTPException(403, "an administrator session is required")
    nonce = secrets.token_urlsafe(32)
    now = int(time.time())
    await request.app.state.pool.execute(
        "WITH expired AS (DELETE FROM oauth_states WHERE expires_at <= now()) "
        "INSERT INTO oauth_states (nonce_hash, integration, user_id, token_version, token_hash, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        hashlib.sha256(nonce.encode()).digest(), integration, user.id, user.token_version,
        _token_hash(token), datetime.fromtimestamp(now + _TTL, UTC),
    )
    return jwt.encode(
        {"aud": _AUDIENCE, "a": integration, "n": nonce, "sub": str(user.id),
         "tv": user.token_version, "iat": now, "exp": now + _TTL},
        _signing_key(request.app.state.secret_key), algorithm="HS256",
    )


async def consume_state(request: Request, integration: str, state: str) -> bool:
    try:
        claims = jwt.decode(
            state, _signing_key(request.app.state.secret_key), algorithms=["HS256"],
            audience=_AUDIENCE,
            options={"require": ["aud", "a", "n", "sub", "tv", "iat", "exp"]},
        )
        if (claims["a"] != integration or not isinstance(claims["n"], str)
                or not claims["n"] or type(claims["tv"]) is not int):
            return False
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError):
        return False
    return bool(await request.app.state.pool.fetchval(
        "DELETE FROM oauth_states s USING users u "
        "WHERE s.nonce_hash = $1 AND s.integration = $2 AND s.user_id = $3 "
        "AND s.token_version = $4 AND s.expires_at > now() "
        "AND u.id = s.user_id AND u.role = 'admin' AND u.token_version = s.token_version "
        "AND NOT EXISTS (SELECT 1 FROM revoked_sessions r WHERE r.token_hash = s.token_hash) "
        "RETURNING s.user_id",
        hashlib.sha256(claims["n"].encode()).digest(), integration, user_id, claims["tv"],
    ))
