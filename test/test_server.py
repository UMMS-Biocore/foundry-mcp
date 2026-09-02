"""
Tests for HTTP server middleware and functionality.
"""

import json

import pytest
import requests
from unittest.mock import AsyncMock, MagicMock, patch

from src.foundry_mcp import server


class TestCredentialsMiddleware:
    """Tests for the CredentialsMiddleware class."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset credentials before and after each test."""
        from src.foundry_mcp.config import set_credentials
        set_credentials(None, None)
        yield
        set_credentials(None, None)

    @pytest.fixture
    def middleware(self):
        """Create middleware instance with mock app."""
        from src.foundry_mcp.server import CredentialsMiddleware
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
        from src.foundry_mcp.config import get_credentials, set_credentials
        
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
        from src.foundry_mcp.config import set_credentials
        
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


    @pytest.mark.asyncio
    async def test_accepts_new_foundry_connect_headers(self, middleware, mock_receive, mock_send):
        """New X-Foundry-Connect-* headers are accepted (dual-accept during the rebrand)."""
        scope = self._create_scope(headers=[
            (b"x-foundry-connect-hostname", b"https://foundry.example.com"),
            (b"x-foundry-connect-token", b"via_mcp_valid-token"),
        ])
        await middleware(scope, mock_receive, mock_send)
        middleware.app.assert_called_once()
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_viafoundry_headers_still_accepted(self, middleware, mock_receive, mock_send):
        """Legacy X-ViaFoundry-* headers still work (backward compatibility)."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://legacy.example.com"),
            (b"x-viafoundry-token", b"via_mcp_valid-token"),
        ])
        await middleware(scope, mock_receive, mock_send)
        middleware.app.assert_called_once()
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_headers_take_precedence_when_both_present(self, middleware, mock_receive, mock_send):
        """Both header families present and valid -> request is accepted."""
        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://legacy.example.com"),
            (b"x-viafoundry-token", b"via_mcp_legacy"),
            (b"x-foundry-connect-hostname", b"https://foundry.example.com"),
            (b"x-foundry-connect-token", b"via_mcp_new"),
        ])
        await middleware(scope, mock_receive, mock_send)
        middleware.app.assert_called_once()
        mock_send.assert_not_called()


class TestOAuthBearerSupport:
    """Tests for OAuth `Authorization: Bearer` fallback + WWW-Authenticate on 401."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset credentials before and after each test."""
        from src.foundry_mcp.config import set_credentials
        set_credentials(None, None)
        yield
        set_credentials(None, None)

    @pytest.fixture
    def middleware(self):
        """Create middleware instance with mock app (open mode, no fixed hostname)."""
        from src.foundry_mcp.server import CredentialsMiddleware
        mock_app = AsyncMock()
        return CredentialsMiddleware(mock_app)

    @pytest.fixture
    def fixed_middleware(self):
        """Create middleware instance in fixed-hostname (production) mode."""
        from src.foundry_mcp.server import CredentialsMiddleware
        mock_app = AsyncMock()
        return CredentialsMiddleware(mock_app, fixed_hostname="https://prod.viafoundry.com")

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
    async def test_bearer_header_populates_credentials(self, middleware, mock_receive, mock_send):
        """Authorization: Bearer <token> should be used when X-ViaFoundry-Token is
        absent, and hostname should be derived from Host + X-Forwarded-Proto."""
        from src.foundry_mcp.config import get_credentials

        scope = self._create_scope(headers=[
            (b"authorization", b"Bearer via_mcp_abc123"),
            (b"host", b"viafoundry.example.com"),
            (b"x-forwarded-proto", b"https"),
        ])

        await middleware(scope, mock_receive, mock_send)

        middleware.app.assert_called_once()
        hostname, token = get_credentials()
        assert token == "via_mcp_abc123"
        assert hostname == "https://viafoundry.example.com"

    @pytest.mark.asyncio
    async def test_bearer_header_case_insensitive_prefix_and_default_proto(
        self, middleware, mock_receive, mock_send
    ):
        """The 'Bearer' prefix match is case-insensitive; proto defaults to https
        when X-Forwarded-Proto is absent."""
        from src.foundry_mcp.config import get_credentials

        scope = self._create_scope(headers=[
            (b"authorization", b"bearer via_mcp_xyz789"),
            (b"host", b"viafoundry.example.com"),
        ])

        await middleware(scope, mock_receive, mock_send)

        middleware.app.assert_called_once()
        hostname, token = get_credentials()
        assert token == "via_mcp_xyz789"
        assert hostname == "https://viafoundry.example.com"

    @pytest.mark.asyncio
    async def test_explicit_headers_take_priority_over_bearer(self, middleware, mock_receive, mock_send):
        """Explicit X-ViaFoundry-Token/Hostname headers must win over Authorization: Bearer."""
        from src.foundry_mcp.config import get_credentials

        scope = self._create_scope(headers=[
            (b"x-viafoundry-hostname", b"https://explicit.example.com"),
            (b"x-viafoundry-token", b"via_mcp_explicit-token"),
            (b"authorization", b"Bearer via_mcp_should-be-ignored"),
            (b"host", b"viafoundry.example.com"),
        ])

        await middleware(scope, mock_receive, mock_send)

        middleware.app.assert_called_once()
        hostname, token = get_credentials()
        assert token == "via_mcp_explicit-token"
        assert hostname == "https://explicit.example.com"

    @pytest.mark.asyncio
    async def test_bearer_header_works_in_fixed_hostname_mode(self, fixed_middleware, mock_receive, mock_send):
        """Bearer fallback should also work in fixed-hostname (production) mode; the
        fixed hostname still wins over any Host-derived value."""
        from src.foundry_mcp.config import get_credentials

        scope = self._create_scope(headers=[
            (b"authorization", b"Bearer via_mcp_prod-token"),
            (b"host", b"anything.example.com"),
        ])

        await fixed_middleware(scope, mock_receive, mock_send)

        fixed_middleware.app.assert_called_once()
        hostname, token = get_credentials()
        assert token == "via_mcp_prod-token"
        assert hostname == "https://prod.viafoundry.com"

    @pytest.mark.asyncio
    async def test_missing_creds_returns_www_authenticate(self, middleware, mock_receive, mock_send):
        """401 response should include WWW-Authenticate pointing at the OAuth discovery doc."""
        scope = self._create_scope(headers=[
            (b"host", b"viafoundry.example.com"),
            (b"x-forwarded-proto", b"https"),
        ])

        await middleware(scope, mock_receive, mock_send)

        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401
        header_map = dict(start_call["headers"])
        assert header_map[b"www-authenticate"] == (
            b'Bearer resource_metadata="https://viafoundry.example.com/.well-known/oauth-protected-resource"'
        )

    @pytest.mark.asyncio
    async def test_www_authenticate_present_in_fixed_hostname_mode(
        self, fixed_middleware, mock_receive, mock_send
    ):
        """401 in fixed-hostname mode should also carry WWW-Authenticate, built from
        the request Host (not the fixed hostname)."""
        scope = self._create_scope(headers=[
            (b"host", b"prod.viafoundry.com"),
        ])

        await fixed_middleware(scope, mock_receive, mock_send)

        fixed_middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401
        header_map = dict(start_call["headers"])
        assert header_map[b"www-authenticate"] == (
            b'Bearer resource_metadata="https://prod.viafoundry.com/.well-known/oauth-protected-resource"'
        )

    @pytest.mark.asyncio
    async def test_no_host_header_omits_www_authenticate(self, middleware, mock_receive, mock_send):
        """When Host is unavailable, the 401 response should omit WWW-Authenticate
        rather than crash."""
        scope = self._create_scope(headers=[])

        await middleware(scope, mock_receive, mock_send)

        middleware.app.assert_not_called()
        start_call = mock_send.call_args_list[0][0][0]
        assert start_call["status"] == 401
        header_map = dict(start_call["headers"])
        assert b"www-authenticate" not in header_map


class TestCompareProcessRevisions:
    """Tests for the compare_process_revisions tool.

    These mock the SDK client and the outbound HTTP call, so they prove the
    tool is registered on the server module and shapes its output correctly.
    They do not exercise the real backend route or a real Foundry Connect
    deployment.
    """

    def _client(self, hostname="https://foundry.example.com"):
        client = MagicMock()
        client.auth.hostname = hostname
        client.auth.get_headers.return_value = {"Authorization": "Bearer via_mcp_test-token"}
        return client

    def _response(self, status_code=200, payload=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = {} if payload is None else payload
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
        else:
            response.raise_for_status.return_value = None
        return response

    def test_requests_the_compare_route_with_revision_ids_as_query_params(self):
        client = self._client(hostname="https://foundry.example.com")
        response = self._response(payload={"paths": []})
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response) as mock_get:
            server.compare_process_revisions(process_id="42", revision_a="7", revision_b="9")

        mock_get.assert_called_once_with(
            "https://foundry.example.com/api/v1/process/42/revisions/compare",
            params={"a": "7", "b": "9"},
            headers={"Authorization": "Bearer via_mcp_test-token"},
            timeout=(20, 120),
        )

    def test_strips_a_trailing_slash_from_the_hostname_before_concatenating(self):
        # Matches the _upload_run_file precedent: a hostname stored with a trailing
        # slash must not turn into a doubled slash in the request path.
        client = self._client(hostname="https://foundry.example.com/")
        response = self._response(payload={"paths": []})
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response) as mock_get:
            server.compare_process_revisions(process_id="42", revision_a="7", revision_b="9")

        mock_get.assert_called_once_with(
            "https://foundry.example.com/api/v1/process/42/revisions/compare",
            params={"a": "7", "b": "9"},
            headers={"Authorization": "Bearer via_mcp_test-token"},
            timeout=(20, 120),
        )

    def test_returns_the_serialized_comparison_payload(self):
        client = self._client()
        payload = {
            "paths": [
                {"path": "process.yaml", "change": "modified", "diff": "--- a\n+++ b\n"},
            ]
        }
        response = self._response(payload=payload)
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            result = server.compare_process_revisions(process_id="42", revision_a="7", revision_b="9")

        assert json.loads(result) == payload

    def test_http_error_from_the_backend_is_returned_as_an_error_payload(self):
        client = self._client()
        response = self._response(status_code=404)
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            result = server.compare_process_revisions(process_id="42", revision_a="7", revision_b="9")

        parsed = json.loads(result)
        assert "error" in parsed

    def test_missing_credentials_is_returned_as_an_error_payload_not_raised(self):
        with patch.object(server, "get_client", side_effect=ValueError("Missing credentials")):
            result = server.compare_process_revisions(process_id="42", revision_a="7", revision_b="9")

        parsed = json.loads(result)
        assert "Missing credentials" in parsed["error"]
