import asyncio
from datetime import UTC, datetime
from uuid import UUID

from argon2.exceptions import VerificationError
from marketplace_api.models.role import Role
from marketplace_api.models.user_response import UserResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.auth.models import RefreshToken, User
from app.auth.security import decode, digest, issue_tokens, password_hasher
from app.core.config import settings
from app.core.errors import APIError

# A fixed dummy hash keeps an unknown-username login on the password-verification path.
DUMMY_HASH = password_hasher.hash("not-a-real-user-password")


async def register(session, data):
    password_hash = await asyncio.to_thread(password_hasher.hash, data.password)
    async with session.begin():
        user_id = await session.scalar(
            insert(User)
            .values(
                username=data.username,
                password_hash=password_hash,
                role=Role(data.role.value),
            )
            .on_conflict_do_nothing(index_elements=["username"])
            .returning(User.id)
        )
        if user_id is None:
            raise APIError(409, "USER_ALREADY_EXISTS", "Username is already registered")
        user = await session.get(User, user_id)
        return UserResponse.model_validate(user)


async def login(session, data, request):
    async with session.begin():
        user = await session.scalar(select(User).where(User.username == data.username))
        try:
            valid = await asyncio.to_thread(
                password_hasher.verify, user.password_hash if user else DUMMY_HASH, data.password
            )
        except VerificationError:
            valid = False
        if user is None or not valid:
            raise APIError(401, "TOKEN_INVALID", "Invalid username or password")
        request.state.user_id = str(user.id)
        return issue_tokens(user, session)


async def refresh(session, data, request):
    claims = decode(data.refresh_token, "refresh")
    async with session.begin():
        token = await session.scalar(
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == digest(data.refresh_token),
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if (
            token is None
            or token.revoked
            or token.expires_at <= now
            or token.user_id != UUID(claims["sub"])
        ):
            raise APIError(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid or already used")
        user = await session.get(User, token.user_id)
        if user is None:
            raise APIError(401, "REFRESH_TOKEN_INVALID", "User no longer exists")
        token.revoked = True
        request.state.user_id = str(user.id)
        return issue_tokens(user, session)


async def bootstrap_admin(session):
    """Explicit local bootstrap, never a public registration role."""
    if settings.bootstrap_admin_password is None:
        return
    password = settings.bootstrap_admin_password.get_secret_value()
    if len(password) < 8:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must contain at least 8 characters")
    password_hash = await asyncio.to_thread(password_hasher.hash, password)
    async with session.begin():
        await session.execute(
            insert(User)
            .values(
                username=settings.bootstrap_admin_username,
                password_hash=password_hash,
                role=Role.ADMIN,
            )
            .on_conflict_do_nothing(index_elements=["username"])
        )
