"""
Tests for ViaFoundry client management.
"""

import pytest
from unittest.mock import patch, MagicMock
from src.viafoundry_mcp.client import get_client, reset_clients
from src.viafoundry_mcp.config import set_credentials


@pytest.fixture(autouse=True)
def reset_client_before_test():
    """Reset client before each test."""
    reset_clients()
    yield
    reset_clients()


@pytest.fixture
def mock_credentials():
    """Set credentials in context."""
    set_credentials(
        hostname='https://test.viafoundry.com',
        token='test-token-12345'
    )
    yield
    set_credentials(None, None)


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_get_client_initializes_once(mock_client_class, mock_credentials):
    """Test that client is initialized only once for same credentials."""
    mock_instance = MagicMock()
    mock_client_class.return_value = mock_instance

    # First call
    client1 = get_client()

    # Second call
    client2 = get_client()

    # Should return same instance
    assert client1 is client2

    # Client class should be instantiated only once
    assert mock_client_class.call_count == 1


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_get_client_configures_auth_token(mock_client_class, mock_credentials):
    """Test that client authentication is configured with token."""
    mock_instance = MagicMock()
    mock_client_class.return_value = mock_instance

    get_client()

    # Verify configure_auth_token was called with correct parameters
    mock_instance.configure_auth_token.assert_called_once_with(
        hostname='https://test.viafoundry.com',
        token='test-token-12345'
    )


@patch('src.viafoundry_mcp.config.load_env_file')
def test_get_client_missing_credentials(mock_load_env):
    """Test that get_client raises error when credentials are missing."""
    set_credentials(None, None)
    with pytest.raises(ValueError, match="Missing credentials"):
        get_client()


def test_get_client_missing_token():
    """Test that get_client raises error when token is missing."""
    set_credentials('https://test.viafoundry.com', None)
    with pytest.raises(ValueError, match="Missing credentials"):
        get_client()


def test_get_client_missing_hostname():
    """Test that get_client raises error when hostname is missing."""
    set_credentials(None, 'test-token')
    with pytest.raises(ValueError, match="Missing credentials"):
        get_client()


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_reset_clients_clears_cache(mock_client_class, mock_credentials):
    """Test that reset_clients clears the cached instances."""
    # Create distinct mock instances for each call
    mock_instance1 = MagicMock(name='client1')
    mock_instance2 = MagicMock(name='client2')
    mock_client_class.side_effect = [mock_instance1, mock_instance2]

    # Initialize client
    client1 = get_client()

    # Reset
    reset_clients()

    # Get client again - should create new instance
    client2 = get_client()

    # Should create new instance
    assert client1 is not client2
    assert client1 is mock_instance1
    assert client2 is mock_instance2
    assert mock_client_class.call_count == 2


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_get_client_caches_by_credentials(mock_client_class):
    """Test that different credentials get different clients."""
    mock_instance1 = MagicMock(name='client1')
    mock_instance2 = MagicMock(name='client2')
    mock_client_class.side_effect = [mock_instance1, mock_instance2]

    # First set of credentials
    set_credentials('https://host1.viafoundry.com', 'token-1111')
    client1 = get_client()

    # Different credentials
    set_credentials('https://host2.viafoundry.com', 'token-2222')
    client2 = get_client()

    # Should be different instances
    assert client1 is not client2
    assert mock_client_class.call_count == 2

    # Cleanup
    set_credentials(None, None)
