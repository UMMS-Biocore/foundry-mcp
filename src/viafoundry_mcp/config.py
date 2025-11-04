#!/usr/bin/env python3
"""
Configuration and credential management for ViaFoundry MCP.
"""

import os
import sys
import logging
from pathlib import Path
from typing import Optional, Tuple
from dotenv import load_dotenv

logger = logging.getLogger('viafoundry-mcp')


def get_env_file_paths() -> list[Path]:
    """Get list of possible .env file locations in priority order."""
    return [
        # 1. XDG config directory (standard for user configs)
        Path.home() / '.config' / 'viafoundry-mcp' / '.env',
        # 2. Current directory
        Path.cwd() / '.env',
        # 3. src/viafoundry_mcp directory (when running from package root)
        Path.cwd() / 'src' / 'viafoundry_mcp' / '.env',
        # 4. Parent directory's src/viafoundry_mcp (when running from src/viafoundry_mcp)
        Path.cwd().parent / 'src' / 'viafoundry_mcp' / '.env',
        # 5. Script's directory
        Path(__file__).parent / '.env',
        # 6. User's home directory (legacy)
        Path.home() / '.viafoundry-mcp.env',
    ]


def load_env_file() -> bool:
    """Load .env file from multiple possible locations."""
    possible_locations = get_env_file_paths()

    for env_path in possible_locations:
        if env_path.exists():
            logger.info(f"Loading .env from: {env_path}")
            load_dotenv(env_path)
            return True

    logger.warning("No .env file found in any standard location")
    logger.info("Checked locations:")
    for loc in possible_locations:
        logger.info(f"  - {loc}")
    return False


def prompt_for_credentials() -> Tuple[str, str, str]:
    """
    Interactively prompt user for ViaFoundry credentials.

    Returns:
        Tuple of (hostname, username, password)
    """
    print("\n" + "="*60)
    print("ViaFoundry MCP Server - Initial Setup")
    print("="*60)
    print("\nNo credentials found. Let's set up your ViaFoundry connection.")
    print("\nYou'll need:")
    print("  1. Your ViaFoundry instance URL (e.g., https://viafoundry.umassmed.edu)")
    print("  2. Your ViaFoundry username")
    print("  3. Your ViaFoundry password")
    print()

    # Get hostname
    while True:
        hostname = input("ViaFoundry Hostname (with https://): ").strip()
        if hostname.startswith("http://") or hostname.startswith("https://"):
            break
        print("❌ Hostname must start with http:// or https://")

    # Get username
    username = input("ViaFoundry Username: ").strip()

    # Get password (note: in stdio mode, we can't hide input)
    import getpass
    try:
        password = getpass.getpass("ViaFoundry Password: ")
    except (KeyboardInterrupt, EOFError):
        password = input("ViaFoundry Password: ").strip()

    return hostname, username, password


def save_credentials(hostname: str, username: str, password: str) -> Path:
    """
    Save credentials to the recommended .env location.

    Args:
        hostname: ViaFoundry instance URL
        username: ViaFoundry username
        password: ViaFoundry password

    Returns:
        Path to the saved .env file
    """
    # Use recommended location
    config_dir = Path.home() / '.config' / 'viafoundry-mcp'
    config_dir.mkdir(parents=True, exist_ok=True)

    env_file = config_dir / '.env'

    with open(env_file, 'w') as f:
        f.write(f"# ViaFoundry MCP Configuration\n")
        f.write(f"# Created automatically\n\n")
        f.write(f"VIAFOUNDRY_HOSTNAME={hostname}\n")
        f.write(f"VIAFOUNDRY_USERNAME={username}\n")
        f.write(f"VIAFOUNDRY_PASSWORD={password}\n")

    # Set restrictive permissions (Unix-like systems)
    try:
        os.chmod(env_file, 0o600)
    except Exception:
        pass  # Windows doesn't support chmod

    return env_file


def get_credentials(interactive: bool = True) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Get ViaFoundry credentials from environment or prompt user.

    Args:
        interactive: If True, prompt user when credentials are missing

    Returns:
        Tuple of (hostname, username, password) or (None, None, None) if not found
    """
    # First, try to load from .env file
    load_env_file()

    # Get credentials from environment
    hostname = os.getenv("VIAFOUNDRY_HOSTNAME")
    username = os.getenv("VIAFOUNDRY_USERNAME")
    password = os.getenv("VIAFOUNDRY_PASSWORD")

    # If any credential is missing and interactive mode is enabled
    if not all([hostname, username, password]) and interactive:
        logger.info("Credentials not found in environment, prompting user...")

        try:
            hostname, username, password = prompt_for_credentials()

            # Ask if user wants to save
            print("\n" + "-"*60)
            save_choice = input("Save credentials for future use? (y/n): ").strip().lower()

            if save_choice in ['y', 'yes']:
                env_file = save_credentials(hostname, username, password)
                print(f"\n✓ Credentials saved to: {env_file}")
                print("  You won't need to enter them again.")
            else:
                print("\n⚠️  Credentials not saved. You'll need to enter them next time.")

            print("-"*60 + "\n")

        except (KeyboardInterrupt, EOFError):
            print("\n\n❌ Setup cancelled by user.")
            return None, None, None

    return hostname, username, password


def setup_command():
    """
    Standalone setup command for configuring credentials.
    """
    print("\n" + "="*60)
    print("ViaFoundry MCP - Configuration Setup")
    print("="*60 + "\n")

    # Check if credentials already exist
    load_env_file()
    existing_hostname = os.getenv("VIAFOUNDRY_HOSTNAME")
    existing_username = os.getenv("VIAFOUNDRY_USERNAME")

    if existing_hostname and existing_username:
        print(f"✓ Found existing configuration:")
        print(f"  Hostname: {existing_hostname}")
        print(f"  Username: {existing_username}")
        print()

        reconfigure = input("Reconfigure? (y/n): ").strip().lower()
        if reconfigure not in ['y', 'yes']:
            print("\nSetup cancelled. Existing configuration kept.")
            return

    # Get new credentials
    try:
        hostname, username, password = prompt_for_credentials()
        env_file = save_credentials(hostname, username, password)

        print("\n" + "="*60)
        print("✓ Configuration saved successfully!")
        print("="*60)
        print(f"\nCredentials file: {env_file}")
        print("\nYou can now use ViaFoundry MCP in your IDE:")
        print('  {"viafoundry": {"command": "viafoundry-mcp"}}')
        print()

    except (KeyboardInterrupt, EOFError):
        print("\n\n❌ Setup cancelled by user.\n")
        sys.exit(1)


def validate_credentials(hostname: str, username: str, password: str) -> bool:
    """
    Validate that credentials are non-empty and properly formatted.

    Args:
        hostname: ViaFoundry instance URL
        username: ViaFoundry username
        password: ViaFoundry password

    Returns:
        True if credentials are valid, False otherwise
    """
    if not hostname or not username or not password:
        return False

    if not (hostname.startswith("http://") or hostname.startswith("https://")):
        logger.error(f"Invalid hostname: {hostname} (must start with http:// or https://)")
        return False

    return True
