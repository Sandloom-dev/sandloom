"""Identity test configuration.

The generic half (``.env.test`` loading, the test engine / ``test_db`` session,
the HTTP client bound to it as the request UoW) comes from the shared
``sandloom.core.testing`` harness. What stays here is identity's own domain: real
user rows, tokens minted through identity's login path, OAuth/SSO mocks and
the per-test Redis that holds identity's security state.
"""

import os
from collections.abc import AsyncGenerator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from sandloom.sdk.testing import recording_uow
from sandloom.sdk.testing.fixtures import *  # noqa: F403
from sandloom_identity.events import EventActor
from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole

DEFAULT_PASSWORD = "TestPassword123!"


@pytest.fixture(scope="session", autouse=True)
def _cheap_password_hashing() -> Iterator[None]:
    """Production-cost argon2 is ~60ms per hash/verify; tests only need the
    algorithm, not the work factor. Same scheme, minimal parameters."""
    from argon2 import PasswordHasher

    from sandloom_identity.services.auth_service import AuthService

    original = AuthService.password_hasher
    AuthService.password_hasher = PasswordHasher(
        time_cost=1, memory_cost=8, parallelism=1
    )
    yield
    AuthService.password_hasher = original


@pytest.fixture(scope="session", autouse=True)
def _test_sso_credentials(sandloom_test_env: Any) -> None:
    """Test SSO credentials win over whatever ``.env.test`` declares."""
    os.environ["GOOGLE_CLIENT_ID"] = "test_google_client_id"
    os.environ["GOOGLE_CLIENT_SECRET"] = "test_google_client_secret"


@pytest_asyncio.fixture(autouse=True)
async def _identity_test_cache(sandloom_cache: Any) -> Any:
    """EVERY identity test runs on the emptied test Redis (harness ``sandloom_cache``).

    Identity keeps security state in the cache (login lockout counters,
    session revocation marks, SSO state/MFA challenges), so no test may see a
    counter left by another.
    """
    return sandloom_cache


@pytest_asyncio.fixture(autouse=True)
async def _ambient_uow() -> AsyncGenerator[Any, None]:
    """Every identity test runs under a recording UoW scope.

    Identity's security audits are ``required=True`` (a missing UoW is an
    error, never a dropped audit row). Services exercised directly against a
    mocked / raw session record into this in-memory scope; API tests bind the
    real test session through the harness ``client`` instead.
    """
    with recording_uow() as session:
        yield session


@pytest_asyncio.fixture
async def test_redis(_identity_test_cache: Any) -> AsyncGenerator[Any, None]:
    """A client on the test Redis (the DB the cache instance uses), emptied
    before and after the test."""
    import redis.asyncio as aredis

    from sandloom.sdk.testing import flush_owned_redis

    redis_client = aredis.from_url(
        os.environ["CACHE_URL"], encoding="utf-8", decode_responses=True
    )
    await flush_owned_redis(redis_client)
    yield redis_client
    await flush_owned_redis(redis_client)
    await redis_client.connection_pool.disconnect()


@pytest_asyncio.fixture
async def async_client(client: AsyncClient, test_redis: Any) -> AsyncClient:
    """The harness client, on a freshly emptied test Redis."""
    return client


# === User Fixtures ===


async def _register(db: Any, email: str, full_name: str, password: str) -> User:
    from sandloom_identity.schemas.auth import RegisterIn
    from sandloom_identity.services.auth_service import AuthService

    data = RegisterIn(email=email, full_name=full_name, password=password)
    return await AuthService.register(data, db, actor=EventActor())


@pytest_asyncio.fixture
async def test_user(test_db: Any) -> User:
    """A registered, verified user (``testuser@example.com``)."""
    user = await _register(
        test_db, "testuser@example.com", "Test User", DEFAULT_PASSWORD
    )
    user.is_verified = True
    await test_db.commit()
    await test_db.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_user(test_db: Any) -> User:
    """A registered, verified superuser holding the ``admin`` role."""
    from sqlalchemy import select

    user = await _register(
        test_db, "admin@example.com", "Admin User", "AdminPassword123!"
    )
    user.is_verified = True
    user.is_superuser = True
    await test_db.commit()
    await test_db.refresh(user)

    result = await test_db.execute(select(Role).where(Role.code == "admin"))
    admin_role = result.scalar_one_or_none()
    if not admin_role:
        admin_role = Role(
            name="Admin", code="admin", description="Administrator role", is_system=True
        )
        test_db.add(admin_role)
        await test_db.commit()
        await test_db.refresh(admin_role)

    test_db.add(UserRole(user_id=user.id, role_id=admin_role.id))
    await test_db.commit()
    return user


@pytest_asyncio.fixture
async def inactive_user(test_db: Any) -> User:
    """A registered but deactivated user."""
    user = await _register(
        test_db, "inactive@example.com", "Inactive User", DEFAULT_PASSWORD
    )
    user.is_active = False
    await test_db.commit()
    await test_db.refresh(user)
    return user


@pytest_asyncio.fixture
async def make_user(test_db: Any) -> Any:
    """Factory: insert a user row directly (no registration side effects)."""
    from sandloom_identity.services.auth_service import AuthService

    async def _make(
        email: str,
        *,
        password: str | None = DEFAULT_PASSWORD,
        verified: bool = True,
        active: bool = True,
        full_name: str = "Test User",
    ) -> User:
        user = User(
            email=email,
            full_name=full_name,
            hashed_password=AuthService.get_password_hash(password)
            if password
            else None,
            is_active=active,
            is_verified=verified,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)
        return user

    return _make


# === Authentication Fixtures ===


async def _login_headers(user: User, db: Any, core: Any) -> dict[str, str]:
    """Mint through identity's real token path, so role/permission claims are
    embedded exactly as on login (claims-based authz reads them with zero I/O).
    Other modules' claims contributors run through the contract runtime; the
    test owns the (uncommitted) transaction, so bind it for them."""
    from sandloom.sdk.testing import bind_contract_session
    from sandloom_identity.services.token_service import TokenService

    with bind_contract_session(db):
        tokens = await TokenService.create_tokens(user, db, core)
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "Content-Type": "application/json",
    }


@pytest_asyncio.fixture
async def authenticated_headers(
    test_user: User, test_db: Any, sandloom_app: Any
) -> dict[str, str]:
    """Headers with an access token for ``test_user``."""
    return await _login_headers(test_user, test_db, sandloom_app.state.core)


@pytest_asyncio.fixture
async def admin_headers(
    admin_user: User, test_db: Any, sandloom_app: Any
) -> dict[str, str]:
    """Headers with an access token for ``admin_user`` (admin role claim)."""
    return await _login_headers(admin_user, test_db, sandloom_app.state.core)


# === Mock Fixtures ===


@pytest.fixture
def mock_email_sender() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_event_publisher() -> Mock:
    return Mock()


class TestConfig:
    """Static user payloads (legacy compatibility for test_database_setup)."""

    @staticmethod
    def get_test_user_data() -> dict[str, str]:
        return {
            "email": "newuser@example.com",
            "username": "testuser",
            "password": DEFAULT_PASSWORD,
            "first_name": "Test",
            "last_name": "User",
        }

    @staticmethod
    def get_admin_user_data() -> dict[str, str]:
        return {
            "email": "newadmin@example.com",
            "username": "admin",
            "password": "AdminPassword123!",
            "first_name": "Admin",
            "last_name": "User",
        }


@pytest.fixture
def mock_oauth_client_factory() -> Iterator[MagicMock]:
    """OAuth client factory returning a mock Google client that always works."""
    from sandloom_identity.integrations import GoogleOAuthClient, OAuthClientFactory
    from sandloom_identity.models.external_identity import Provider

    mock_google_client = MagicMock(spec=GoogleOAuthClient)
    mock_google_client.get_auth_url.return_value = (
        "https://accounts.google.com/oauth/authorize?test=1"
    )
    mock_google_client.exchange_code_for_user_info.return_value = {
        "id": "test_google_id",
        "email": "test@google.com",
        "name": "Test User",
    }

    def mock_create_client(provider: Any, redirect_uri: Any = None) -> Any:
        return mock_google_client if provider == Provider.GOOGLE else None

    with patch.object(
        OAuthClientFactory, "create_client", side_effect=mock_create_client
    ):
        yield mock_google_client
