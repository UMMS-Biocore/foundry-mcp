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


class TestDownloadFile:
    """Tests for the download_file tool.

    These mock the SDK client and the outbound HTTP call. They prove the tool
    returns small files inline and hands back a link for large ones, without
    reading a large body into memory. They do not exercise a real deployment.
    """

    def _client(self, route_path="/report-resources/run1/pubweb/out/file.txt"):
        import pandas as pd
        client = MagicMock()
        client.auth.hostname = "https://foundry.example.com"
        client.auth.get_headers.return_value = {"Authorization": "Bearer via_mcp_test-token"}
        client.reports.get_all_files.return_value = pd.DataFrame(
            [{"file_path": "out/file.txt", "routePath": route_path}]
        )
        return client

    def _response(self, chunks):
        response = MagicMock()
        response.status_code = 200
        response.iter_content.return_value = iter(chunks)
        return response

    def test_small_file_comes_back_inline_as_base64(self):
        import base64
        client = self._client()
        response = self._response([b"hello ", b"world"])
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response) as mock_get:
            result = json.loads(server.download_file(report_id="1", file_path="out/file.txt"))

        assert mock_get.call_args.kwargs["stream"] is True
        assert result["success"] is True
        assert result["delivery"] == "inline"
        assert base64.b64decode(result["file_content_base64"]) == b"hello world"
        assert result["file_size"] == 11

    UUID = "0123456789abcdef0123456789abcdef"
    DASHED = "01234567-89ab-cdef-0123-456789abcdef"
    MINT_PATH = "/api/v1/report-download/v1.abc.def"

    def _large(self, client, mint=None, file_path="out/file.txt", limit=10):
        """Run download_file against a body larger than the inline limit."""
        if isinstance(mint, Exception):
            client.call.side_effect = mint
        elif mint is not None:
            client.call.return_value = mint
        response = self._response([b"x" * 20])
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response), \
                patch.object(server, "INLINE_DOWNLOAD_MAX_BYTES", limit):
            return server.download_file(report_id="77", file_path=file_path)

    def _route(self, uuid_text=None):
        return f"/report-resources/{uuid_text or self.UUID}/pubweb/out/file.txt"

    def _mint_ok(self, **extra):
        body = {"path": self.MINT_PATH, "expiresAt": "2026-10-17T12:15:00Z",
                "fileName": "file.txt", "size": 20}
        body.update(extra)
        return body

    def _with_file(self, client, file_path):
        import pandas as pd
        client.reports.get_all_files.return_value = pd.DataFrame(
            [{"file_path": file_path, "routePath": self._route()}]
        )

    def test_large_file_returns_a_signed_link_and_stops_reading(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        chunk = b"x" * (1024 * 1024)
        consumed = []

        def chunks():
            for _ in range(50):
                consumed.append(1)
                yield chunk

        response = self._response(chunks())
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response), \
                patch.object(server, "INLINE_DOWNLOAD_MAX_BYTES", 5 * 1024 * 1024):
            result = json.loads(server.download_file(report_id="77", file_path="out/file.txt"))

        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/run/77/download-link",
            data={"attemptUuid": self.UUID, "filePath": "out/file.txt"},
        )
        assert result["success"] is True
        assert result["delivery"] == "url"
        assert "file_content_base64" not in result
        assert result["file_name"] == "file.txt"
        assert result["download_url"] == "https://foundry.example.com" + self.MINT_PATH
        assert result["download_command"] == (
            "curl -fL -o ./file.txt https://foundry.example.com" + self.MINT_PATH
        )
        assert result["expires_at"] == "2026-10-17T12:15:00Z"
        assert "report-resources" not in json.dumps(result)
        assert len(consumed) <= 6
        response.close.assert_called()

    def test_dashed_uuid_is_passed_as_found(self):
        client = self._client(route_path=self._route(self.DASHED.upper()))
        client.call.return_value = self._mint_ok()
        result = json.loads(self._large(client))

        assert result["delivery"] == "url"
        assert client.call.call_args.kwargs["data"]["attemptUuid"] == self.DASHED.upper()

    def test_nonempty_base_path_is_kept_in_the_link(self):
        client = self._client(route_path=self._route())
        client.auth.hostname = "https://h/base"
        client.call.return_value = self._mint_ok(path="/api/v1/report-download/v1.x.y")
        result = json.loads(self._large(client))

        assert result["download_url"] == "https://h/base/api/v1/report-download/v1.x.y"

    def test_hostile_file_name_is_quoted_with_a_dot_slash_prefix(self):
        client = self._client(route_path=self._route())
        self._with_file(client, "out/-rf x'.tsv")
        client.call.return_value = self._mint_ok()
        result = json.loads(self._large(client, file_path="out/-rf x'.tsv"))

        assert result["file_name"] == "-rf x'.tsv"
        assert result["download_command"].startswith("curl -fL -o './-rf x'\"'\"'.tsv' ")

    def test_file_name_loses_separators_and_control_characters(self):
        client = self._client(route_path=self._route())
        name = "a\\b\x00c\nd.txt"
        self._with_file(client, "out/" + name)
        client.call.return_value = self._mint_ok()
        result = json.loads(self._large(client, file_path="out/" + name))

        assert result["file_name"] == "abcd.txt"

    def test_mint_404_says_no_link_was_issued_for_the_file(self):
        client = self._client(route_path=self._route())
        raw = self._large(client, mint=RuntimeError("Error 305: Not Found: no route"))
        result = json.loads(raw)

        assert "larger than" in result["error"]
        assert "too large to return inline" in result["error"]
        assert "did not issue a download link for it" in result["error"]
        assert "may not support download links" in result["error"]
        assert "cannot be linked" in result["error"]
        assert "report-resources" not in raw
        assert "download_url" not in result

    def test_mint_405_says_the_server_issues_no_links(self):
        client = self._client(route_path=self._route())
        raw = self._large(
            client,
            mint=RuntimeError("Error 307: Unexpected HTTP error occurred. Status code: 405."),
        )
        result = json.loads(raw)

        assert "does not issue download links" in result["error"]
        assert "report-resources" not in raw

    def test_other_mint_failure_is_a_link_error_without_fallback(self):
        client = self._client(route_path=self._route())
        raw = self._large(client, mint=RuntimeError("Error 500: boom"))
        result = json.loads(raw)

        assert "download link could not be created" in result["error"]
        assert "boom" in result["error"]
        assert "report-resources" not in raw

    def test_malformed_mint_response_is_an_error(self):
        for bad in ({"path": "/report-resources/x"}, {"path": 5}, {}, "nope", None):
            client = self._client(route_path=self._route())
            client.call.return_value = bad
            raw = self._large(client)
            result = json.loads(raw)
            assert "error" in result
            assert "download_url" not in result
            assert "report-resources" not in raw

    def test_mint_path_with_traversal_or_space_is_rejected(self):
        for bad in ("/api/v1/report-download/../x", "/api/v1/report-download/a b",
                    "/api/v1/report-download/", "/api/v1/report-download/a/b"):
            client = self._client(route_path=self._route())
            raw = self._large(client, mint={"path": bad})
            result = json.loads(raw)
            assert "error" in result
            assert "download_url" not in result
            assert "report-download" not in raw

    def test_file_name_loses_bidi_and_c1_control_characters(self):
        client = self._client(route_path=self._route())
        name = "a‮b\u0085c.txt"
        self._with_file(client, "out/" + name)
        client.call.return_value = self._mint_ok()
        result = json.loads(self._large(client, file_path="out/" + name))

        assert result["file_name"] == "abc.txt"

    def test_unparseable_route_path_is_an_error_and_mint_is_not_called(self):
        for bad in ("/report-resources/not-a-uuid/pubweb/out/file.txt",
                    "/report-resources/" + self.UUID + "/other/file.txt",
                    "/elsewhere/" + self.UUID + "/pubweb/file.txt"):
            client = self._client(route_path=bad)
            raw = self._large(client)
            assert "error" in json.loads(raw)
            client.call.assert_not_called()
            assert "download_url" not in json.loads(raw)

    def test_non_200_probe_error_has_no_plain_url(self):
        client = self._client(route_path=self._route())
        response = MagicMock()
        response.status_code = 403
        response.reason = "Forbidden"
        response.text = "denied " + "https://foundry.example.com" + self._route()
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            raw = server.download_file(report_id="1", file_path="out/file.txt")

        result = json.loads(raw)
        assert result["status_code"] == 403
        assert "url" not in result
        assert "report-resources" not in raw
        client.call.assert_not_called()

    def test_probe_exception_has_no_plain_url(self):
        client = self._client(route_path=self._route())
        boom = server.requests.exceptions.ConnectionError(
            "Max retries exceeded with url: https://foundry.example.com" + self._route()
        )
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", side_effect=boom):
            raw = server.download_file(report_id="1", file_path="out/file.txt")

        result = json.loads(raw)
        assert "Could not reach the server to measure the file" in result["error"]
        assert "ConnectionError" in result["error"]
        assert "url" not in result
        assert "report-resources" not in raw

    def test_outer_error_never_echoes_the_plain_url(self):
        client = self._client(route_path=self._route())
        client.reports.fetch_report_data.side_effect = RuntimeError("bad " + self._route())
        with patch.object(server, "get_client", return_value=client):
            raw = server.download_file(report_id="1", file_path="out/file.txt")

        assert "error" in json.loads(raw)
        assert "report-resources" not in raw

    def test_small_file_does_not_mint_a_link(self):
        client = self._client(route_path=self._route())
        response = self._response([b"hi"])
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            result = json.loads(server.download_file(report_id="1", file_path="out/file.txt"))

        assert result["delivery"] == "inline"
        client.call.assert_not_called()

    def test_link_answer_never_carries_the_credential(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        raw = self._large(client)

        assert "via_mcp_test" not in raw
        assert "Authorization" not in raw

    # delivery: "auto" (default), "url" (a link at any size), "inline" (bytes or a refusal)

    def test_delivery_url_returns_a_link_for_a_small_file_without_reading_it(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get") as mock_get:
            raw = server.download_file(report_id="77", file_path="out/file.txt", delivery="url")
        result = json.loads(raw)

        mock_get.assert_not_called()
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/run/77/download-link",
            data={"attemptUuid": self.UUID, "filePath": "out/file.txt"},
        )
        assert result["success"] is True
        assert result["delivery"] == "url"
        assert "file_content_base64" not in result
        assert result["download_url"] == "https://foundry.example.com" + self.MINT_PATH
        assert result["download_command"] == (
            "curl -fL -o ./file.txt https://foundry.example.com" + self.MINT_PATH
        )
        assert "larger than" not in result["message"]
        assert "report-resources" not in raw
        assert "via_mcp_test" not in raw

    def test_delivery_is_case_and_space_tolerant(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get") as mock_get:
            result = json.loads(
                server.download_file(report_id="77", file_path="out/file.txt", delivery=" URL ")
            )

        mock_get.assert_not_called()
        assert result["delivery"] == "url"

    def test_delivery_url_mint_404_does_not_claim_the_file_is_large(self):
        client = self._client(route_path=self._route())
        client.call.side_effect = RuntimeError("Error 305: Not Found: no route")
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get"):
            raw = server.download_file(report_id="77", file_path="out/file.txt", delivery="url")
        result = json.loads(raw)

        assert "larger than" not in result["error"]
        assert "did not issue a download link" in result["error"]
        assert "delivery='inline'" in result["hint"]
        assert "download_url" not in result
        assert "report-resources" not in raw

    def test_delivery_url_mint_405_says_the_server_issues_no_links(self):
        client = self._client(route_path=self._route())
        client.call.side_effect = RuntimeError(
            "Error 307: Unexpected HTTP error occurred. Status code: 405."
        )
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get"):
            result = json.loads(
                server.download_file(report_id="77", file_path="out/file.txt", delivery="url")
            )

        assert "does not issue download links" in result["error"]
        assert "larger than" not in result["error"]
        assert "delivery='inline'" in result["hint"]

    def test_delivery_url_still_reports_a_missing_file(self):
        client = self._client(route_path=self._route())
        with patch.object(server, "get_client", return_value=client):
            result = json.loads(
                server.download_file(report_id="77", file_path="nope.txt", delivery="url")
            )

        assert "not found" in result["error"]
        client.call.assert_not_called()

    def test_delivery_inline_returns_a_small_file_inline(self):
        import base64
        client = self._client(route_path=self._route())
        response = self._response([b"hi"])
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            result = json.loads(
                server.download_file(report_id="1", file_path="out/file.txt", delivery="inline")
            )

        assert result["delivery"] == "inline"
        assert base64.b64decode(result["file_content_base64"]) == b"hi"
        client.call.assert_not_called()

    def test_delivery_inline_refuses_an_oversize_file_without_a_link(self):
        client = self._client(route_path=self._route())
        response = self._response([b"x" * 20])
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response), \
                patch.object(server, "INLINE_DOWNLOAD_MAX_BYTES", 10):
            raw = server.download_file(report_id="77", file_path="out/file.txt", delivery="inline")
        result = json.loads(raw)

        client.call.assert_not_called()
        assert "too large to return inline" in result["error"]
        assert "delivery='url'" in result["hint"]
        assert "download_url" not in result
        assert "file_content_base64" not in result
        response.close.assert_called()

    def test_default_delivery_is_auto_and_unchanged(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        result = json.loads(self._large(client))

        assert result["delivery"] == "url"
        assert "larger than" in result["message"]

    def test_unknown_delivery_is_rejected_before_any_call(self):
        client = self._client(route_path=self._route())
        with patch.object(server, "get_client", return_value=client) as mock_client, \
                patch.object(server.requests, "get") as mock_get:
            result = json.loads(
                server.download_file(report_id="77", file_path="out/file.txt", delivery="link")
            )

        assert "delivery" in result["error"]
        for value in ("auto", "url", "inline"):
            assert value in result["error"]
        mock_client.assert_not_called()
        mock_get.assert_not_called()

    # Auto mode inlines only small files; anything larger is a link for a sandbox.

    def _auto(self, client, body, chunk=4096, delivery="auto"):
        chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)]
        response = self._response(chunks)
        with patch.object(server, "get_client", return_value=client), \
                patch.object(server.requests, "get", return_value=response):
            raw = server.download_file(report_id="77", file_path="out/file.txt", delivery=delivery)
        return raw, response

    def test_auto_links_a_49_kib_file(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        raw, response = self._auto(client, b"x" * (49 * 1024))
        result = json.loads(raw)

        assert result["delivery"] == "url"
        assert "file_content_base64" not in result
        client.call.assert_called_once()
        response.close.assert_called()

    def test_auto_inlines_a_47_kib_file(self):
        import base64
        client = self._client(route_path=self._route())
        body = b"y" * (47 * 1024)
        raw, _ = self._auto(client, body)
        result = json.loads(raw)

        assert result["delivery"] == "inline"
        assert base64.b64decode(result["file_content_base64"]) == body
        client.call.assert_not_called()

    def test_auto_link_reply_leads_with_the_sandbox_action(self):
        client = self._client(route_path=self._route())
        client.call.return_value = self._mint_ok()
        raw, _ = self._auto(client, b"x" * (60 * 1024))
        result = json.loads(raw)

        assert list(result)[0] == "action"
        assert result["action"] == "fetch_in_sandbox"
        assert isinstance(result["file_size"], int)
        assert result["file_size"] > 48 * 1024
        message = result["message"]
        assert "download_command" in message
        assert "sandbox" in message
        assert "delivery='inline'" in message
        assert "2026-10-17T12:15:00Z" in message
        assert "again" in message

    def test_auto_link_file_size_comes_from_report_metadata(self):
        import pandas as pd
        client = self._client(route_path=self._route())
        client.reports.get_all_files.return_value = pd.DataFrame(
            [{"file_path": "out/file.txt", "routePath": self._route(), "fileSize": 7340032}]
        )
        client.call.return_value = self._mint_ok()
        raw, _ = self._auto(client, b"x" * (60 * 1024))

        assert json.loads(raw)["file_size"] == 7340032

    def test_explicit_inline_still_returns_a_file_just_under_5_mb(self):
        import base64
        client = self._client(route_path=self._route())
        body = b"z" * (5 * 1024 * 1024 - 1)
        raw, _ = self._auto(client, body, chunk=1024 * 1024, delivery="inline")
        result = json.loads(raw)

        assert result["delivery"] == "inline"
        assert len(base64.b64decode(result["file_content_base64"])) == len(body)
        client.call.assert_not_called()

    def test_explicit_inline_refuses_a_file_over_5_mb(self):
        client = self._client(route_path=self._route())
        raw, _ = self._auto(client, b"z" * (5 * 1024 * 1024 + 1), chunk=1024 * 1024, delivery="inline")
        result = json.loads(raw)

        assert "too large to return inline" in result["error"]
        client.call.assert_not_called()

    def test_auto_mint_failure_points_at_explicit_inline(self):
        client = self._client(route_path=self._route())
        client.call.side_effect = RuntimeError("Error 305: Not Found: no route")
        raw, _ = self._auto(client, b"x" * (60 * 1024))
        result = json.loads(raw)

        assert "did not issue a download link" in result["error"]
        assert "delivery='inline'" in result["hint"]

