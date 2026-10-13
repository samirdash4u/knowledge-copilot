"""JWT issuing and verification. The caller's role and tenant come only from the token."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from copilot.config import Settings
from copilot.models import Principal, Role


class AuthError(Exception):
    pass


def issue_token(settings: Settings, user_id: str, role: Role | str, tenant: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": Role.parse(role).label,
        "tenant": tenant,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_token(settings: Settings, token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "role", "tenant", "exp"]},
        )
        return Principal(
            user_id=claims["sub"], role=Role.parse(claims["role"]), tenant=claims["tenant"]
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise AuthError(str(exc)) from exc
