"""
Tests for configuration management.
"""

import os
import pytest
from pathlib import Path
from src.viafoundry_mcp.config import (
    get_env_file_paths,
    validate_credentials,
    save_credentials,
)


def test_get_env_file_paths():
    """Test that env file paths are returned in correct order."""
    paths = get_env_file_paths()

    assert len(paths) > 0
    assert all(isinstance(p, Path) for p in paths)

    # First path should be XDG config
    assert paths[0] == Path.home() / '.config' / 'viafoundry-mcp' / '.env'


def test_validate_credentials_valid():
    """Test validation of valid credentials."""
    assert validate_credentials(
        "https://viafoundry.com",
        "user",
        "pass"
    ) is True


def test_validate_credentials_missing_hostname():
    """Test validation fails with missing hostname."""
    assert validate_credentials("", "user", "pass") is False


def test_validate_credentials_missing_username():
    """Test validation fails with missing username."""
    assert validate_credentials("https://viafoundry.com", "", "pass") is False


def test_validate_credentials_missing_password():
    """Test validation fails with missing password."""
    assert validate_credentials("https://viafoundry.com", "user", "") is False


def test_validate_credentials_invalid_hostname():
    """Test validation fails with invalid hostname format."""
    assert validate_credentials("viafoundry.com", "user", "pass") is False


def test_save_credentials(tmp_path):
    """Test saving credentials to file."""
    # Use temporary directory
    os.environ['HOME'] = str(tmp_path)

    env_file = save_credentials(
        "https://test.com",
        "testuser",
        "testpass"
    )

    assert env_file.exists()

    # Read and verify contents
    content = env_file.read_text()
    assert "VIAFOUNDRY_HOSTNAME=https://test.com" in content
    assert "VIAFOUNDRY_USERNAME=testuser" in content
    assert "VIAFOUNDRY_PASSWORD=testpass" in content
