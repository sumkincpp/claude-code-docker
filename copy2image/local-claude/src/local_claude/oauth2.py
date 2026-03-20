"""OAuth2 authentication support for Ollama API.

Provides OAuth2 client credentials flow authentication for both
requests and httpx HTTP clients.
"""

import logging
import threading
import time
from typing import Any

import httpx
import requests

logger = logging.getLogger(__name__)


class OAuth2TokenManager:
    """Manages OAuth2 tokens with automatic refresh.

    Thread-safe token manager that handles token acquisition and refresh
    for client credentials flow.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        token_refresh_margin: int = 60,
    ) -> None:
        """Initialize the token manager.

        Args:
            client_id: OAuth2 client ID.
            client_secret: OAuth2 client secret.
            token_url: OAuth2 token endpoint URL.
            token_refresh_margin: Seconds before expiry to refresh token.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.token_refresh_margin = token_refresh_margin

        self._token: dict[str, Any] | None = None
        self._token_lock = threading.Lock()

        logger.info("OAuth2 token manager initialized for client_id: %s", client_id)

    def _is_token_expired(self) -> bool:
        """Check if the current token is expired or about to expire."""
        if self._token is None:
            return True

        expires_at = self._token.get("expires_at")
        if expires_at is None:
            # No expiration info, assume expired
            return True

        # Check if token expires within the margin
        return time.time() >= (expires_at - self.token_refresh_margin)

    def _fetch_token(self) -> dict[str, Any]:
        """Fetch a new token from the OAuth2 server."""
        logger.debug("Fetching new OAuth2 token from: %s", self.token_url)

        response = requests.post(
            self.token_url,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()

        token_data: dict[str, Any] = response.json()
        expires_in = token_data.get("expires_in")
        if expires_in is not None:
            token_data["expires_at"] = time.time() + int(expires_in)

        logger.info("OAuth2 token acquired, expires_at: %s", token_data.get("expires_at"))
        return token_data

    def get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary.

        Returns:
            Valid access token string.

        Raises:
            RuntimeError: If token acquisition fails.
        """
        with self._token_lock:
            if self._is_token_expired():
                try:
                    self._token = self._fetch_token()
                except Exception:
                    logger.exception("Failed to fetch OAuth2 token")
                    raise RuntimeError("OAuth2 token acquisition failed")

            if self._token is None:
                raise RuntimeError("OAuth2 token is None after fetch")

            access_token: str = self._token["access_token"]
            return access_token

    def get_auth_header(self) -> dict[str, str]:
        """Get the Authorization header with a valid token.

        Returns:
            Dictionary with Authorization header.
        """
        token = self.get_access_token()
        return {"Authorization": f"Bearer {token}"}


class OAuth2RequestsAuth:
    """Requests auth handler that uses OAuth2TokenManager."""

    def __init__(self, token_manager: OAuth2TokenManager) -> None:
        """Initialize with a token manager.

        Args:
            token_manager: OAuth2TokenManager instance for token management.
        """
        self.token_manager = token_manager

    def __call__(self, request: Any) -> Any:
        """Add OAuth2 authorization header to the request.

        Args:
            request: The prepared request object.

        Returns:
            The modified request with auth header.
        """
        token = self.token_manager.get_access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        return request


class OAuth2HTTPXAuth(httpx.Auth):
    """HTTPX auth handler that uses OAuth2TokenManager."""

    def __init__(self, token_manager: OAuth2TokenManager) -> None:
        """Initialize with a token manager.

        Args:
            token_manager: OAuth2TokenManager instance for token management.
        """
        self.token_manager = token_manager

    def auth_flow(self, request: httpx.Request) -> Any:
        """Add OAuth2 authorization header to the request.

        Args:
            request: The HTTPX request object.

        Yields:
            The modified request with auth header.
        """
        token = self.token_manager.get_access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def create_oauth2_token_manager(
    client_id: str | None = None,
    client_secret: str | None = None,
    token_url: str | None = None,
) -> OAuth2TokenManager | None:
    """Create an OAuth2TokenManager if all required parameters are provided.

    Args:
        client_id: OAuth2 client ID.
        client_secret: OAuth2 client secret.
        token_url: OAuth2 token endpoint URL.

    Returns:
        OAuth2TokenManager instance or None if parameters are missing.
    """
    if not all([client_id, client_secret, token_url]):
        logger.debug("OAuth2 not configured: missing required parameters")
        return None

    # Type narrowing - we know these are strings now
    assert client_id is not None
    assert client_secret is not None
    assert token_url is not None

    return OAuth2TokenManager(
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
    )
