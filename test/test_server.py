"""
Tests for HTTP server middleware and functionality.
"""

import pytest
from unittest.mock import AsyncMock


class TestCredentialsMiddlewareLocalhost:
    """Tests for middleware with localhost requests (dev mode)."""

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

    def _create_localhost_scope(self, method: str = "POST", headers: list = None):
        """Helper to create HTTP scope for localhost requests."""
        base_headers = [(b"host", b"127.0.0.1:8000")]
        if headers:
            base_headers.extend(headers)
        return {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/mcp",
            "headers": base_headers,
            "client": ("127.0.0.1", 12345),
        }

    @pytest.mark.asyncio
    async def test_options_request_bypasses_validation(self, middleware, mock_receive, mock_send):
        """OPTIONS requests should bypass credential validation for CORS."""
        scope = self._create_localhost_scope(method="OPTIONS", headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should be called, no 401 response
        middleware.app.assert_called_once()
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_401(self, middleware, mock_receive, mock_send):
        """Missing credentials should return 401 Unauthorized."""
        scope = self._create_localhost_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should NOT be called
        middleware.app.assert_not_called()
        
        # Should send 401 response
        assert mock_send.call_count == 2
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["type"] == "http.response.start"
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_hostname_header_returns_401(self, middleware, mock_receive, mock_send):
        """Missing hostname header should return 401 for localhost."""
        scope = self._create_localhost_scope(headers=[
            (b"x-viafoundry-token", b"via_mcp_valid-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, middleware, mock_receive, mock_send):
        """Missing token should return 401 Unauthorized."""
        scope = self._create_localhost_scope(headers=[
            (b"x-viafoundry-hostname", b"https://viafoundry.com"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_invalid_hostname_format_returns_401(self, middleware, mock_receive, mock_send):
        """Invalid hostname format (no http/https) should return 401."""
        scope = self._create_localhost_scope(headers=[
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
        scope = self._create_localhost_scope(headers=[
            (b"x-viafoundry-hostname", b"https://viafoundry.com"),
            (b"x-viafoundry-token", b"invalid-token"),  # Missing via_mcp_ prefix
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_localhost_allows_custom_hostname(self, middleware, mock_receive, mock_send):
        """Localhost requests allow any X-ViaFoundry-Hostname value."""
        from src.viafoundry_mcp.config import get_credentials, set_credentials
        
        set_credentials(None, None)
        
        scope = self._create_localhost_scope(headers=[
            (b"x-viafoundry-hostname", b"https://any-viafoundry.example.com"),
            (b"x-viafoundry-token", b"via_mcp_valid-token-12345"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        # App should be called
        middleware.app.assert_called_once()
        
        # Custom hostname from header should be used
        hostname, token = get_credentials()
        assert hostname == "https://any-viafoundry.example.com"
        assert token == "via_mcp_valid-token-12345"

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self, middleware, mock_receive, mock_send):
        """Non-HTTP scopes (like websocket) should pass through without validation."""
        scope = {"type": "websocket"}
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_401_response_contains_localhost_mode(self, middleware, mock_receive, mock_send):
        """401 response should indicate localhost mode."""
        import json
        
        scope = self._create_localhost_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        body_call = mock_send.call_args_list[1][0][0]
        body = json.loads(body_call["body"].decode())
        
        assert "error" in body
        assert body["error"] == "Unauthorized"
        assert "detail" in body
        assert "help" in body
        assert "mode" in body
        assert body["mode"] == "localhost"


class TestCredentialsMiddlewareProduction:
    """Tests for middleware with production (non-localhost) requests."""

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

    def _create_production_scope(self, method: str = "POST", headers: list = None):
        """Helper to create HTTP scope for production (non-localhost) requests."""
        base_headers = [(b"host", b"mcp.viafoundry.com")]
        if headers:
            base_headers.extend(headers)
        return {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/mcp",
            "headers": base_headers,
            "client": ("10.0.0.1", 12345),
        }

    @pytest.mark.asyncio
    async def test_production_locks_hostname_to_host_without_path(self, middleware, mock_receive, mock_send):
        """Production requests lock hostname to scheme://host (no path)."""
        from src.viafoundry_mcp.config import get_credentials
        
        scope = self._create_production_scope(headers=[
            (b"x-viafoundry-token", b"via_mcp_prod-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()
        
        hostname, token = get_credentials()
        # Hostname should be scheme://host without path
        assert hostname == "https://mcp.viafoundry.com"
        assert token == "via_mcp_prod-token"

    @pytest.mark.asyncio
    async def test_production_ignores_hostname_header(self, middleware, mock_receive, mock_send):
        """Production requests ignore X-ViaFoundry-Hostname header."""
        from src.viafoundry_mcp.config import get_credentials
        
        scope = self._create_production_scope(headers=[
            (b"x-viafoundry-hostname", b"https://malicious.site.com"),  # Should be ignored
            (b"x-viafoundry-token", b"via_mcp_prod-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()
        
        hostname, _ = get_credentials()
        # Should be request host, not header value, without path
        assert hostname == "https://mcp.viafoundry.com"

    @pytest.mark.asyncio
    async def test_production_works_without_hostname_header(self, middleware, mock_receive, mock_send):
        """Production requests work without X-ViaFoundry-Hostname header."""
        from src.viafoundry_mcp.config import get_credentials
        
        scope = self._create_production_scope(headers=[
            (b"x-viafoundry-token", b"via_mcp_prod-token"),
        ])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()
        
        hostname, _ = get_credentials()
        assert hostname == "https://mcp.viafoundry.com"

    @pytest.mark.asyncio
    async def test_401_response_shows_production_mode(self, middleware, mock_receive, mock_send):
        """401 response should indicate production mode."""
        import json
        
        scope = self._create_production_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        body_call = mock_send.call_args_list[1][0][0]
        body = json.loads(body_call["body"].decode())
        
        assert body["mode"] == "production"

    @pytest.mark.asyncio
    async def test_production_missing_token_returns_401(self, middleware, mock_receive, mock_send):
        """Production requests still require token."""
        scope = self._create_production_scope(headers=[])
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401

    @pytest.mark.asyncio
    async def test_production_keeps_path_prefix(self, middleware, mock_receive, mock_send):
        """Production requests keep path prefix before /mcp."""
        from src.viafoundry_mcp.config import get_credentials
        
        # Simulate request to /beta/mcp
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/beta/mcp",
            "headers": [
                (b"host", b"dev-playground.infra.gcp.viafoundry.net"),
                (b"x-viafoundry-token", b"via_mcp_prod-token"),
            ],
            "client": ("10.0.0.1", 12345),
        }
        
        await middleware(scope, mock_receive, mock_send)
        
        middleware.app.assert_called_once()
        
        hostname, _ = get_credentials()
        # Should keep /beta but strip /mcp
        assert hostname == "https://dev-playground.infra.gcp.viafoundry.net/beta"

