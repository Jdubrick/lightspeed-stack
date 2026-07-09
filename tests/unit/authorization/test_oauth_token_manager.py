"""Unit tests for OAuth2 token manager (POC ONLY — do not commit)."""

# pylint: disable=protected-access

import asyncio
import os
from collections.abc import Generator
from typing import Any

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr
from pytest_mock import MockerFixture

from authorization import oauth_token_manager as otm
from authorization.oauth_token_manager import (
    OAuthTokenManager,
    get_oauth_manager,
    oauth_env_var_name,
    prefetch_oauth_tokens,
    register_oauth_manager,
)
from models.config import OAuthConfiguration


@pytest.fixture(autouse=True)
def clear_registry() -> Generator[None, None, None]:
    """Clear the OAuth manager registry before and after each test."""
    otm._registry.clear()
    yield
    otm._registry.clear()


@pytest.fixture(name="oauth_config")
def oauth_config_fixture() -> OAuthConfiguration:
    """Return a dummy OAuthConfiguration for testing."""
    return OAuthConfiguration(
        token_url=AnyHttpUrl(
            "http://localhost:8082/realms/poc/protocol/openid-connect/token"
        ),
        client_id=SecretStr("lcs-client"),
        client_secret=SecretStr("poc-secret"),
        scope=None,
        token_expiration_leeway=10,
    )


@pytest.fixture(name="token_manager")
def token_manager_fixture(oauth_config: OAuthConfiguration) -> OAuthTokenManager:
    """Return a fresh OAuthTokenManager for provider_id=vllm."""
    return OAuthTokenManager(
        provider_id="vllm",
        oauth_config=oauth_config,
        provider_data_key="vllm_api_token",
    )


def _mock_token_response(
    mocker: MockerFixture,
    access_token: str = "test-access-token",
    expires_in: int = 120,
    status_code: int = 200,
) -> Any:
    """Patch httpx.AsyncClient.post to return a token response."""
    response = mocker.Mock()
    response.status_code = status_code
    response.raise_for_status = mocker.Mock()
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=mocker.Mock(),
            response=response,
        )
    response.json.return_value = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }

    mock_client = mocker.AsyncMock()
    mock_client.post = mocker.AsyncMock(return_value=response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=None)

    return mocker.patch("httpx.AsyncClient", return_value=mock_client)


class TestOAuthEnvVarName:
    """Tests for oauth_env_var_name helper."""

    def test_vllm_env_var_name(self) -> None:
        """vllm maps to _LCS_OAUTH_VLLM_TOKEN."""
        assert oauth_env_var_name("vllm") == "_LCS_OAUTH_VLLM_TOKEN"


class TestRegistry:
    """Tests for the module-level manager registry."""

    def test_register_and_get(self, token_manager: OAuthTokenManager) -> None:
        """Register then look up a manager by provider_id."""
        assert get_oauth_manager("vllm") is None
        register_oauth_manager("vllm", token_manager)
        assert get_oauth_manager("vllm") is token_manager


class TestOAuthTokenManager:
    """Unit tests for OAuthTokenManager."""

    def test_initial_state(self, token_manager: OAuthTokenManager) -> None:
        """Fresh manager has no token and is expired."""
        assert token_manager.access_token.get_secret_value() == ""
        assert token_manager.is_token_expired
        assert token_manager.build_provider_data() == {}

    @pytest.mark.asyncio
    async def test_fetch_initial_token(
        self, token_manager: OAuthTokenManager, mocker: MockerFixture
    ) -> None:
        """fetch_initial_token caches the token from the HTTP response."""
        _mock_token_response(mocker, access_token="startup-token", expires_in=120)

        token = await token_manager.fetch_initial_token()

        assert token == "startup-token"
        assert token_manager.access_token.get_secret_value() == "startup-token"
        assert not token_manager.is_token_expired
        assert token_manager.build_provider_data() == {
            "vllm_api_token": "startup-token"
        }

    @pytest.mark.asyncio
    async def test_fetch_initial_token_http_error(
        self, token_manager: OAuthTokenManager, mocker: MockerFixture
    ) -> None:
        """fetch_initial_token raises RuntimeError on HTTP failure."""
        _mock_token_response(mocker, status_code=401)

        with pytest.raises(RuntimeError, match="OAuth token request failed"):
            await token_manager.fetch_initial_token()

    def test_is_token_expired_fresh(self, token_manager: OAuthTokenManager) -> None:
        """Token within leeway window is not expired."""
        token_manager._update_access_token("tok", expires_in=120, initial=True)
        assert not token_manager.is_token_expired

    def test_is_token_expired_past_leeway(
        self, token_manager: OAuthTokenManager
    ) -> None:
        """Token past leeway-adjusted expiry is expired."""
        # expires_in=5 with leeway=10 => expires_on = now - 5 => already expired
        token_manager._update_access_token("tok", expires_in=5, initial=True)
        assert token_manager.is_token_expired

    def test_build_provider_data(self, token_manager: OAuthTokenManager) -> None:
        """build_provider_data returns the correct key/value."""
        token_manager._access_token = SecretStr("abc")
        assert token_manager.build_provider_data() == {"vllm_api_token": "abc"}

    @pytest.mark.asyncio
    async def test_refresh_token(
        self, token_manager: OAuthTokenManager, mocker: MockerFixture
    ) -> None:
        """refresh_token fetches a new token when expired."""
        token_manager._expires_on = 0
        _mock_token_response(mocker, access_token="refreshed-token", expires_in=120)

        ok = await token_manager.refresh_token()

        assert ok is True
        assert token_manager.access_token.get_secret_value() == "refreshed-token"
        assert not token_manager.is_token_expired

    @pytest.mark.asyncio
    async def test_refresh_token_double_checked_locking(
        self, token_manager: OAuthTokenManager, mocker: MockerFixture
    ) -> None:
        """Concurrent refresh_token callers only hit the token endpoint once."""
        token_manager._expires_on = 0
        call_count = {"n": 0}

        async def slow_request() -> tuple[str, int]:
            call_count["n"] += 1
            await asyncio.sleep(0.05)
            return "shared-token", 120

        mocker.patch.object(token_manager, "_request_token", side_effect=slow_request)

        results = await asyncio.gather(
            token_manager.refresh_token(),
            token_manager.refresh_token(),
            token_manager.refresh_token(),
        )

        assert all(results)
        assert call_count["n"] == 1
        assert token_manager.access_token.get_secret_value() == "shared-token"

    @pytest.mark.asyncio
    async def test_refresh_token_skips_when_not_expired(
        self, token_manager: OAuthTokenManager, mocker: MockerFixture
    ) -> None:
        """refresh_token is a no-op when the token is still valid."""
        token_manager._update_access_token("still-valid", expires_in=120, initial=True)
        request_mock = mocker.patch.object(
            token_manager, "_request_token", new_callable=mocker.AsyncMock
        )

        ok = await token_manager.refresh_token()

        assert ok is True
        request_mock.assert_not_awaited()
        assert token_manager.access_token.get_secret_value() == "still-valid"


class TestPrefetchOAuthTokens:
    """Tests for prefetch_oauth_tokens startup helper."""

    @pytest.mark.asyncio
    async def test_prefetch_sets_env_and_registry(
        self, mocker: MockerFixture, oauth_config: OAuthConfiguration
    ) -> None:
        """prefetch_oauth_tokens sets env var and registers the manager."""
        provider = mocker.MagicMock()
        provider.type = "vllm"
        provider.oauth = oauth_config

        inference = mocker.MagicMock()
        inference.providers = [provider]

        config = mocker.MagicMock()
        config.inference = inference

        _mock_token_response(mocker, access_token="prefetch-token", expires_in=120)
        env_name = oauth_env_var_name("vllm")
        os.environ.pop(env_name, None)

        try:
            await prefetch_oauth_tokens(config)

            assert os.environ[env_name] == "prefetch-token"
            manager = get_oauth_manager("vllm")
            assert manager is not None
            assert manager.access_token.get_secret_value() == "prefetch-token"
        finally:
            os.environ.pop(env_name, None)

    @pytest.mark.asyncio
    async def test_prefetch_noop_without_oauth_providers(
        self, mocker: MockerFixture
    ) -> None:
        """prefetch_oauth_tokens is a no-op when no providers have oauth."""
        provider = mocker.MagicMock()
        provider.oauth = None

        inference = mocker.MagicMock()
        inference.providers = [provider]

        config = mocker.MagicMock()
        config.inference = inference

        client_mock = mocker.patch("httpx.AsyncClient")
        await prefetch_oauth_tokens(config)
        client_mock.assert_not_called()
