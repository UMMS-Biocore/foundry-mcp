"""
Tests for configuration management.
"""

import pytest
from src.foundry_mcp.config import (
    get_credentials,
    set_credentials,
    validate_credentials,
)


@pytest.fixture(autouse=True)
def reset_credentials():
    """Reset credentials before and after each test."""
    set_credentials(None, None)
    yield
    set_credentials(None, None)


def test_set_and_get_credentials():
    """Test setting and getting credentials from context."""
    set_credentials("https://viafoundry.com", "via_mcp_my-token")
    hostname, token = get_credentials()
    
    assert hostname == "https://viafoundry.com"
    assert token == "via_mcp_my-token"


def test_get_credentials_default_none():
    """Test that credentials default to None."""
    hostname, token = get_credentials()
    
    assert hostname is None
    assert token is None


def test_validate_credentials_valid():
    """Test validation of valid credentials."""
    assert validate_credentials(
        "https://viafoundry.com",
        "via_mcp_valid-token"
    ) is True


def test_validate_credentials_valid_http():
    """Test validation accepts http:// hostname."""
    assert validate_credentials(
        "http://localhost:8080",
        "via_mcp_valid-token"
    ) is True


def test_validate_credentials_missing_hostname():
    """Test validation fails with missing hostname."""
    assert validate_credentials("", "via_mcp_token") is False
    assert validate_credentials(None, "via_mcp_token") is False


def test_validate_credentials_missing_token():
    """Test validation fails with missing token."""
    assert validate_credentials("https://viafoundry.com", "") is False
    assert validate_credentials("https://viafoundry.com", None) is False


def test_validate_credentials_invalid_hostname():
    """Test validation fails with invalid hostname format."""
    assert validate_credentials("viafoundry.com", "via_mcp_token") is False
    assert validate_credentials("ftp://viafoundry.com", "via_mcp_token") is False


def test_validate_credentials_empty_host():
    """Test validation fails with empty host (just protocol)."""
    assert validate_credentials("https://", "via_mcp_token") is False
    assert validate_credentials("http://", "via_mcp_token") is False
    assert validate_credentials("https:///beta", "via_mcp_token") is False  # Empty host with path


def test_validate_credentials_invalid_token_prefix():
    """Test validation fails with invalid token prefix."""
    assert validate_credentials("https://viafoundry.com", "invalid-token") is False
    assert validate_credentials("https://viafoundry.com", "mcp_token") is False


def test_credentials_are_context_scoped():
    """Test that credentials are stored in context variables."""
    # Set credentials
    set_credentials("https://test.com", "via_mcp_test-token")
    
    # Verify they persist
    hostname1, token1 = get_credentials()
    hostname2, token2 = get_credentials()
    
    assert hostname1 == hostname2 == "https://test.com"
    assert token1 == token2 == "via_mcp_test-token"


def test_credentials_can_be_overwritten():
    """Test that credentials can be updated."""
    set_credentials("https://first.com", "via_mcp_first-token")
    hostname1, token1 = get_credentials()
    
    set_credentials("https://second.com", "via_mcp_second-token")
    hostname2, token2 = get_credentials()
    
    assert hostname1 == "https://first.com"
    assert token1 == "via_mcp_first-token"
    assert hostname2 == "https://second.com"
    assert token2 == "via_mcp_second-token"
