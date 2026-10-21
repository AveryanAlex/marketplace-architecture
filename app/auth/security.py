from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from fastapi import Request
from fastapi.security import HTTPBearer
from marketplace_api.models.role import Role

from app.auth.models import RefreshToken, User
from app.core.config import settings
from app.core.database import SessionFactory
from app.core.errors import APIError

password_hasher = PasswordHasher()
bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


@dataclass(frozen=True)
class Principal:
    id: UUID
    role: Role


def digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def decode(token: str, token_type: str) -> dict:
    try:
        data = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "role", "exp", "iat", "jti", "type"]},
        )
        UUID(data["sub"])
        Role(data["role"])
        if data["type"] != token_type:
            raise ValueError("Wrong token type")
        return data
    except jwt.ExpiredSignatureError:
        code = "TOKEN_EXPIRED" if token_type == "access" else "REFRESH_TOKEN_INVALID"
        raise APIError(401, code, "Token expired", headers={"WWW-Authenticate": "Bearer"}) from None
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        code = "TOKEN_INVALID" if token_type == "access" else "REFRESH_TOKEN_INVALID"
        raise APIError(401, code, "Invalid token", headers={"WWW-Authenticate": "Bearer"}) from None


async def authenticate(request: Request) -> Principal:
    credentials = await bearer(request)
    if credentials is None:
        raise APIError(
            401,
            "TOKEN_INVALID",
            "Bearer access token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    data = decode(credentials.credentials, "access")
    async with SessionFactory() as session:
        user = await session.get(User, UUID(data["sub"]))
        if user is None:
            raise APIError(401, "TOKEN_INVALID", "User no longer exists")
        actor = Principal(user.id, user.role)
    request.state.user_id = str(actor.id)
    return actor


def require_role(actor: Principal, *roles: str) -> None:
    if actor.role not in roles:
        raise APIError(403, "ACCESS_DENIED", "Insufficient permissions")


def issue_tokens(user: User, session):
    now = datetime.now(UTC)

    def encode(token_type, expires):
        return jwt.encode(
            {
                "sub": str(user.id),
                "role": user.role.value,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "iat": now,
                "exp": expires,
                "jti": str(uuid4()),
                "type": token_type,
            },
            settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

    access = encode("access", now + timedelta(minutes=settings.access_token_minutes))
    refresh_expiry = now + timedelta(days=settings.refresh_token_days)
    refresh = encode("refresh", refresh_expiry)
    session.add(
        RefreshToken(token_hash=digest(refresh), user_id=user.id, expires_at=refresh_expiry)
    )
    from marketplace_api.models.token_response import TokenResponse

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        token_type="bearer",
        expires_in=settings.access_token_minutes * 60,
    )
