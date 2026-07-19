#!/usr/bin/env python3
"""
Centralized logging configuration for Foundry Connect MCP.

Usage:
    from .log import get_logger, mask_token
    
    logger = get_logger(__name__)
    logger.info("Something happened")
    logger.info(f"Auth with token {mask_token(token)}")

All loggers share the same configuration and format.
"""

import sys
import json
import logging
from datetime import datetime, timezone
from typing import Optional


# ============================================================================
# Configuration
# ============================================================================

LOG_LEVEL = logging.INFO
LOG_FORMAT = "text"  # "text" or "json"
# Format includes token placeholder (filled by filter)
LOG_FORMAT_TEXT = "%(asctime)s - %(name)s - %(levelname)s - [%(token)s] %(message)s"

# Root logger name - all loggers will be children of this
ROOT_LOGGER_NAME = "foundry-mcp"

# Track if logging has been initialized
_initialized = False


# ============================================================================
# Utilities
# ============================================================================

def mask_token(token: Optional[str], visible_suffix: int = 4) -> str:
    """
    Mask a token for safe logging, showing only prefix and last N characters.
    
    Args:
        token: The token to mask
        visible_suffix: Number of characters to show at the end (default: 4)
    
    Returns:
        Masked token string, e.g., "via_mcp_****ab12"
    
    Example:
        mask_token("via_mcp_secret1234") -> "via_mcp_****1234"
    """
    if not token:
        return "none"
    
    # Find the prefix (via_mcp_)
    prefix = "via_mcp_"
    if token.startswith(prefix):
        secret_part = token[len(prefix):]
        if len(secret_part) <= visible_suffix:
            return f"{prefix}****"
        return f"{prefix}****{secret_part[-visible_suffix:]}"
    
    # For non-standard tokens, just mask most of it
    if len(token) <= visible_suffix:
        return "****"
    return f"****{token[-visible_suffix:]}"


class CredentialsFilter(logging.Filter):
    """
    Logging filter that injects hostname and masked token into every log record.
    Reads from context variables set by the middleware.
    """
    
    def filter(self, record: logging.LogRecord) -> bool:
        # Import here to avoid circular imports
        try:
            from .config import get_credentials
            hostname, token = get_credentials()
            record.hostname = hostname or "-"
            record.token = mask_token(token) if token else "-"
        except Exception:
            record.hostname = "-"
            record.token = "-"
        return True


# ============================================================================
# Formatters
# ============================================================================

class AccessLogFormatter(logging.Formatter):
    """Formatter for uvicorn access logs that strips the phantom ':0' port.

    When proxy_headers is enabled, uvicorn resolves the client IP from
    X-Forwarded-For but sets port to 0 (since the header has no port info),
    producing lines like '203.0.113.42:0 - "POST ..."'. This formatter
    strips the ':0' for cleaner output.
    """

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        return result.replace(":0 - \"", " - \"", 1)


class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured logging in production/containers."""
    
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # Strip phantom ':0' port from uvicorn access logs (see AccessLogFormatter)
        if record.name == "uvicorn.access":
            message = message.replace(":0 - \"", " - \"", 1)
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        
        # Add extra fields if present (e.g., hostname, client_ip, token)
        for key in ('hostname', 'client_ip', 'token', 'report_id', 'process_id'):
            if hasattr(record, key):
                log_data[key] = getattr(record, key)
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)


# ============================================================================
# Setup Functions
# ============================================================================

def _init_logging() -> None:
    """Initialize the root logger with proper configuration."""
    global _initialized
    
    if _initialized:
        return
    
    # Get or create the root logger for our application
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    root_logger.setLevel(LOG_LEVEL)
    
    # Remove existing handlers to prevent duplicates
    root_logger.handlers.clear()
    
    # Create handler with stdout (works well with containers)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    
    # Add filter to inject credentials into every log record
    handler.addFilter(CredentialsFilter())
    
    # Set formatter based on format
    if LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(LOG_FORMAT_TEXT))
    
    root_logger.addHandler(handler)
    
    # Don't propagate to Python's root logger (prevents duplicate logs)
    root_logger.propagate = False
    
    _initialized = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get a logger instance for the given module name.
    
    Args:
        name: Module name (typically __name__). If None, returns the root logger.
    
    Returns:
        Configured logger instance
    
    Usage:
        from .log import get_logger
        logger = get_logger(__name__)
    """
    # Ensure logging is initialized
    _init_logging()
    
    if name is None:
        return logging.getLogger(ROOT_LOGGER_NAME)
    
    # Create child logger under our root
    # e.g., "foundry_mcp.server" becomes "foundry-mcp.server"
    short_name = name.replace("foundry_mcp.", "").replace("foundry_mcp", "")
    if short_name:
        logger_name = f"{ROOT_LOGGER_NAME}.{short_name}"
    else:
        logger_name = ROOT_LOGGER_NAME
    
    return logging.getLogger(logger_name)


def get_uvicorn_log_config() -> dict:
    """
    Generate Uvicorn logging config to match application log format.
    
    Returns:
        Uvicorn-compatible logging config dict
    """
    if LOG_FORMAT == "json":
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": JsonFormatter,
                },
            },
            "handlers": {
                "default": {
                    "formatter": "json",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
            },
        }
    else:
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "uvicorn_server": {
                    "format": f"%(asctime)s - uvicorn.server - %(levelname)s - %(message)s",
                },
                "uvicorn_access": {
                    "()": AccessLogFormatter,
                    "fmt": "%(asctime)s - uvicorn.access - %(levelname)s - %(message)s",
                },
            },
            "handlers": {
                "uvicorn_server": {
                    "formatter": "uvicorn_server",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
                "uvicorn_access": {
                    "formatter": "uvicorn_access",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["uvicorn_server"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["uvicorn_server"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["uvicorn_access"], "level": "INFO", "propagate": False},
            },
        }
