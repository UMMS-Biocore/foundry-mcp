"""
Tests for HTTP server middleware and functionality.
"""

import pytest
from unittest.mock import AsyncMock


class TestCredentialsMiddleware:
    """Tests for the CredentialsMiddleware class."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset credentials before and after each test."""
        from src.viafoundry_mcp.config import set_credentials
        set_credentials(None, None)
        yield
        set_credentials(None, None)

    @pytest.fixture
    def middleware(self):
        """Create middleware instance with mock app."""
        from src.viafoundry_mcp.server import CredentialsMiddleware
        mock_app = AsyncMock()
        return CredentialsMiddleware(mock_app)

    @pytest.fixture
    def mock_send(self):
        """Create mock send function."""
        return AsyncMock()

    @pytest.fixture
    def mock_receive(self):
        """Create mock receive function."""
        return AsyncMock()

    def _create_scope(self, method: str = "POST", headers: list = None):
        """Helper to create HTTP scope."""
        return {
            "type": "http",
            "method": method,
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }

    @pytest.mark.asyncio
    async def test_options_request_bypasses_validation(self, middleware, mock_receive, mock_send):
        """OPTIONS requests should bypass credential validation for CORS."""
        scope = self._create_scope(method="OPTIONS", headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should be called, no 401 response
        middleware.app.assert_called_once()
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_401(self, middleware, mock_receive, mock_send):
        """Missing credentials should return 401 Unauthorized."""
        scope = self._create_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should NOT be called
        middleware.app.assert_not_called()
        
        # Should send 401 response
        assert mock_send.call_count == 2
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["type"] == "http.response.start"
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_hostname_returns_401(self, middleware, mock_receive, mock_send):
        """Missing hostname should return 401 Unauthorized."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-token", b"via_mcp_valid-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, middleware, mock_receive, mock_send):
        """Missing token should return 401 Unauthorized."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://viafoundry.com"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_invalid_hostname_format_returns_401(self, middleware, mock_receive, mock_send):
        """Invalid hostname format (no http/https) should return 401."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"viafoundry.com"),  # Missing protocol
            (b"x-viafoundry-token", b"via_mcp_valid-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_invalid_token_prefix_returns_401(self, middleware, mock_receive, mock_send):
        """Token without via_mcp_ prefix should return 401."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://viafoundry.com"),
            (b"x-viafoundry-token", b"invalid-token"),  # Missing via_mcp_ prefix
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_valid_credentials_passes_through(self, middleware, mock_receive, mock_send):
        """Valid credentials should pass through to the app."""
        from src.viafoundry_mcp.config import get_credentials, set_credentials
        
        set_credentials(None, None)
        
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://viafoundry.com"),
            (b"x-viafoundry-token", b"via_mcp_valid-token-12345"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should be called
        middleware.app.assert_called_once()
        
        # Credentials should be set in context
        hostname, token = get_credentials()
        assert hostname == "https://viafoundry.com"
        assert token == "via_mcp_valid-token-12345"

    @pytest.mark.asyncio
    async def test_valid_http_hostname_passes_through(self, middleware, mock_receive, mock_send):
        """HTTP hostname (not just HTTPS) should be valid."""
        from src.viafoundry_mcp.config import set_credentials
        
        set_credentials(None, None)
        
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"http://localhost:8080"),
            (b"x-viafoundry-token", b"via_mcp_dev-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self, middleware, mock_receive, mock_send):
        """Non-HTTP scopes (like websocket) should pass through without validation."""
        scope = {"type": "websocket"}
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_401_response_contains_helpful_message(self, middleware, mock_receive, mock_send):
        """401 response should contain helpful error details."""
        import json
        
        scope = self._create_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        body_call = mock_send.call_args_list[1][0][0]
        body = json.loads(body_call["body"].decode())
        
        assert "error" in body
        assert body["error"] == "Unauthorized"
        assert "detail" in body
        assert "help" in body

    @pytest.mark.asyncio
    async def test_empty_string_credentials_returns_401(self, middleware, mock_receive, mock_send):
        """Empty string credentials should return 401."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b""),
            (b"x-viafoundry-token", b""),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401
