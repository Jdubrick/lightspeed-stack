"""OAuth2 client-credentials token manager for inference providers.

POC ONLY — do not commit. This module intentionally logs raw access tokens
for debugging. Production code must never do that.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional

import httpx
from pydantic import SecretStr

from log import get_logger

if TYPE_CHECKING:
    from models.config import Configuration, OAuthConfiguration

logger = get_logger(__name__)

# Maps synthesized provider_id -> Llama Stack provider_data API key field.
# Must match the provider's provider_data_api_key_field (e.g. VLLMInferenceAdapter).
PROVIDER_DATA_API_KEY_FIELD: dict[str, str] = {
    "vllm": "vllm_api_token",
    "vllm-rhaiis": "vllm_api_token",
    "vllm-rhel-ai": "vllm_api_token",
    "openai": "openai_api_key",
    "azure": "azure_api_key",
    "watsonx": "watsonx_api_key",
    "vertexai": "gemini_api_key",
}

_registry: dict[str, "OAuthTokenManager"] = {}


def oauth_env_var_name(provider_type: str) -> str:
    """Return the auto-generated env var name for an OAuth provider token.

    Parameters:
        provider_type: High-level provider type (e.g. ``vllm``).

    Returns:
        Env var name such as ``_LCS_OAUTH_VLLM_TOKEN``.
    """
    return f"_LCS_OAUTH_{provider_type.upper()}_TOKEN"


def get_oauth_manager(provider_id: str) -> Optional["OAuthTokenManager"]:
    """Look up a registered OAuth token manager by provider_id.

    Parameters:
        provider_id: Synthesized provider id (e.g. ``vllm``).

    Returns:
        The manager if registered, otherwise None.
    """
    return _registry.get(provider_id)


def get_all_oauth_managers() -> list["OAuthTokenManager"]:
    """Return all registered OAuth token managers.

    Returns:
        List of managers (empty when no oauth providers are configured).
    """
    return list(_registry.values())


def register_oauth_manager(provider_id: str, manager: "OAuthTokenManager") -> None:
    """Register an OAuth token manager for a provider.

    Parameters:
        provider_id: Synthesized provider id (e.g. ``vllm``).
        manager: The token manager instance.
    """
    _registry[provider_id] = manager
    logger.info("Registered OAuthTokenManager for provider_id=%s", provider_id)


class OAuthTokenManager:
    """Manages OAuth2 client-credentials access tokens for one inference provider.

    POC ONLY — logs raw tokens on fetch and refresh for debugging.
    """

    def __init__(
        self,
        provider_id: str,
        oauth_config: "OAuthConfiguration",
        provider_data_key: str,
    ) -> None:
        """Initialize the token manager.

        Parameters:
            provider_id: Synthesized provider id (e.g. ``vllm``).
            oauth_config: OAuth2 client-credentials configuration.
            provider_data_key: Key written into Llama Stack provider_data
                (e.g. ``vllm_api_token``).
        """
        self._provider_id = provider_id
        self._oauth_config = oauth_config
        self._provider_data_key = provider_data_key
        self._access_token: SecretStr = SecretStr("")
        self._expires_on: int = 0
        self._lock = asyncio.Lock()

    @property
    def provider_id(self) -> str:
        """Return the provider id this manager is bound to."""
        return self._provider_id

    @property
    def is_token_expired(self) -> bool:
        """Return True if the cached token is missing or past its leeway window."""
        return self._expires_on == 0 or time.time() > self._expires_on

    @property
    def access_token(self) -> SecretStr:
        """Return the cached access token."""
        return self._access_token

    def build_provider_data(self) -> dict[str, str]:
        """Build a provider_data dict with the cached access token.

        Returns:
            Dict suitable for merging into ``client.provider_data``, or empty
            if no token is cached.
        """
        token = self._access_token.get_secret_value()
        if not token:
            return {}
        return {self._provider_data_key: token}

    async def fetch_initial_token(self) -> str:
        """Fetch an access token and cache it.

        Returns:
            The access token string.

        Raises:
            RuntimeError: If the token endpoint request fails.
        """
        token, expires_in = await self._request_token()
        self._update_access_token(token, expires_in, initial=True)
        return token

    async def refresh_token(self) -> bool:
        """Refresh the cached token with double-checked locking.

        Acquires the lock, re-checks expiry (another request may have refreshed
        while waiting), and only fetches if still expired.

        Returns:
            True if a fresh token is available after this call, False on failure.
        """
        async with self._lock:
            if not self.is_token_expired:
                logger.info(
                    "OAuth token for %s already refreshed by another request",
                    self._provider_id,
                )
                return True

            logger.info("Refreshing OAuth token for provider_id=%s", self._provider_id)
            try:
                token, expires_in = await self._request_token()
            except RuntimeError:
                logger.error(
                    "Failed to refresh OAuth token for provider_id=%s",
                    self._provider_id,
                )
                return False

            self._update_access_token(token, expires_in, initial=False)
            return True

    def _update_access_token(
        self, token: str, expires_in: int, *, initial: bool
    ) -> None:
        """Cache the token and compute the leeway-adjusted expiry.

        Parameters:
            token: Access token string.
            expires_in: Lifetime in seconds from the token response.
            initial: True for startup fetch, False for refresh (affects log text).
        """
        leeway = self._oauth_config.token_expiration_leeway
        self._access_token = SecretStr(token)
        self._expires_on = int(time.time()) + expires_in - leeway
        expiry_time = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self._expires_on)
        )
        action = "fetched (startup)" if initial else "refreshed"
        # POC ONLY: intentionally log the raw token for debugging.
        logger.info(
            "OAuth token %s for provider_id=%s, expires_at=%s (leeway=%ss), "
            "token=%s",
            action,
            self._provider_id,
            expiry_time,
            leeway,
            token,
        )

    async def _request_token(self) -> tuple[str, int]:
        """POST to the OAuth2 token endpoint using client_credentials.

        Returns:
            Tuple of (access_token, expires_in).

        Raises:
            RuntimeError: If the HTTP request fails or the response is invalid.
        """
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._oauth_config.client_id.get_secret_value(),
            "client_secret": self._oauth_config.client_secret.get_secret_value(),
        }
        if self._oauth_config.scope:
            data["scope"] = self._oauth_config.scope

        token_url = str(self._oauth_config.token_url)
        logger.info(
            "Requesting OAuth token from %s for provider_id=%s",
            token_url,
            self._provider_id,
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(
                f"OAuth token request failed for {self._provider_id}: {exc}"
            ) from exc

        access_token = body.get("access_token")
        expires_in = body.get("expires_in", 3600)
        if not access_token or not isinstance(access_token, str):
            raise RuntimeError(
                f"OAuth token response missing access_token for {self._provider_id}"
            )
        if not isinstance(expires_in, int):
            try:
                expires_in = int(expires_in)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"OAuth token response has invalid expires_in for "
                    f"{self._provider_id}: {expires_in!r}"
                ) from exc

        return access_token, expires_in


async def prefetch_oauth_tokens(config: "Configuration") -> None:
    """Fetch initial OAuth tokens for all oauth-configured inference providers.

    For each provider with an ``oauth`` block, creates an OAuthTokenManager,
    fetches a token, registers the manager, and sets the auto-generated env
    var so Llama Stack can resolve ``${env._LCS_OAUTH_*_TOKEN}`` at init.

    Parameters:
        config: Loaded Lightspeed configuration.

    Raises:
        RuntimeError: If any OAuth-configured provider fails to fetch a token
            (POC: hard-fail only; no graceful degradation).
    """
    providers = config.inference.providers if config.inference else []
    oauth_providers = [p for p in providers if p.oauth is not None]
    if not oauth_providers:
        return

    logger.info(
        "Prefetching OAuth tokens for %d provider(s)",
        len(oauth_providers),
    )

    for provider in oauth_providers:
        assert provider.oauth is not None  # narrowed by filter above
        provider_id = provider.type.replace("_", "-")
        provider_data_key = PROVIDER_DATA_API_KEY_FIELD.get(
            provider_id, f"{provider_id}_api_key"
        )
        manager = OAuthTokenManager(provider_id, provider.oauth, provider_data_key)
        token = await manager.fetch_initial_token()
        env_name = oauth_env_var_name(provider.type)
        os.environ[env_name] = token
        register_oauth_manager(provider_id, manager)
        logger.info(
            "Set env var %s for provider_id=%s (token length=%d)",
            env_name,
            provider_id,
            len(token),
        )
