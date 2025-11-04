"""
Tests for ViaFoundry client management.
"""

import os
import pytest
from unittest.mock import patch, MagicMock
from src.viafoundry_mcp.client import get_client, reset_client


@pytest.fixture(autouse=True)
def reset_client_before_test():
    """Reset client before each test."""
    reset_client()
    yield
    reset_client()


@pytest.fixture
def mock_credentials():
    """Mock environment variables with credentials."""
    with patch.dict(os.environ, {
        'VIAFOUNDRY_HOSTNAME': 'https://test.viafoundry.com',
        'VIAFOUNDRY_USERNAME': 'testuser',
        'VIAFOUNDRY_PASSWORD': 'testpass'
    }):
        yield


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_get_client_initializes_once(mock_client_class, mock_credentials):
    """Test that client is initialized only once."""
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
def test_get_client_configures_auth(mock_client_class, mock_credentials):
    """Test that client authentication is configured."""
    mock_instance = MagicMock()
    mock_client_class.return_value = mock_instance

    get_client()

    # Verify configure_auth was called with correct parameters
    mock_instance.configure_auth.assert_called_once_with(
        hostname='https://test.viafoundry.com',
        username='testuser',
        password='testpass'
    )


def test_get_client_missing_credentials():
    """Test that get_client raises error when credentials are missing."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="Missing required credentials"):
            get_client()


@patch('src.viafoundry_mcp.client.ViaFoundryClient')
def test_reset_client_clears_instance(mock_client_class, mock_credentials):
    """Test that reset_client clears the global instance."""
    # Create distinct mock instances for each call
    mock_instance1 = MagicMock(name='client1')
    mock_instance2 = MagicMock(name='client2')
    mock_client_class.side_effect = [mock_instance1, mock_instance2]

    # Initialize client
    client1 = get_client()

    # Reset
    reset_client()

    # Get client again - should create new instance
    client2 = get_client()

    # Should create new instance
    assert client1 is not client2
    assert client1 is mock_instance1
    assert client2 is mock_instance2
    assert mock_client_class.call_count == 2
