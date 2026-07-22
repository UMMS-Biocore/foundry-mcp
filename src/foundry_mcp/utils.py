#!/usr/bin/env python3
"""
Utility functions for Foundry Connect MCP.
"""

from typing import Optional

# Maximum recursion depth to prevent stack overflow
MAX_SERIALIZATION_DEPTH = 50

# MCP token prefix - required for all MCP tokens
MCP_TOKEN_PREFIX = "via_mcp_"


def is_valid_mcp_token(token: Optional[str]) -> bool:
    """Check if token starts with 'via_mcp_' prefix and has content after it."""
    return bool(token and token.startswith(MCP_TOKEN_PREFIX) and len(token) > len(MCP_TOKEN_PREFIX))


def serialize_response(obj, _visited=None, _depth: int = 0):
    """
    Recursively serialize response objects to JSON-compatible dicts.
    Handles Pydantic models, lists, dicts, and other common types.
    
    Includes protection against circular references and excessive recursion.
    
    Args:
        obj: The object to serialize
        _visited: Internal set tracking visited object IDs (for circular reference detection)
        _depth: Internal counter for recursion depth
    
    Returns:
        JSON-serializable representation of the object
    """
    # Check recursion depth
    if _depth > MAX_SERIALIZATION_DEPTH:
        return f"<max depth exceeded: {type(obj).__name__}>"
    
    # Initialize visited set on first call
    if _visited is None:
        _visited = set()
    
    # Handle None and primitives (no circular reference possible)
    if obj is None:
        return None
    
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # For mutable objects, check for circular references using object ID
    obj_id = id(obj)
    if obj_id in _visited:
        return f"<circular reference: {type(obj).__name__}>"
    
    # Pydantic v2 models
    if hasattr(obj, 'model_dump'):
        _visited.add(obj_id)
        try:
            return serialize_response(obj.model_dump(), _visited, _depth + 1)
        finally:
            _visited.discard(obj_id)
    
    if isinstance(obj, dict):
        _visited.add(obj_id)
        try:
            return {k: serialize_response(v, _visited, _depth + 1) for k, v in obj.items()}
        finally:
            _visited.discard(obj_id)
    
    if isinstance(obj, (list, tuple)):
        _visited.add(obj_id)
        try:
            return [serialize_response(item, _visited, _depth + 1) for item in obj]
        finally:
            _visited.discard(obj_id)
    
    # Dataclasses, custom objects
    if hasattr(obj, '__dict__'):
        _visited.add(obj_id)
        try:
            return {
                k: serialize_response(v, _visited, _depth + 1) 
                for k, v in obj.__dict__.items() 
                if not k.startswith('_')
            }
        finally:
            _visited.discard(obj_id)
    
    return str(obj)


def remove_none(obj):
    """Recursively remove None values from nested dicts and lists."""
    if isinstance(obj, dict):
        return {k: remove_none(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [remove_none(item) for item in obj]
    return obj


def tail_text(text, max_lines: int = 200, max_chars: int = 12000) -> str:
    """Return the last `max_lines` lines of `text`, capped at `max_chars`
    characters (keeping the tail — the end of a log is where errors live).

    A non-positive `max_lines`/`max_chars` means "no lines"/"no characters" and
    returns "". Guarding this explicitly matters because Python's `seq[-0:]` is
    `seq[0:]`, which would otherwise return the WHOLE text for a caller asking
    for none of it."""
    if not text or max_lines <= 0 or max_chars <= 0:
        return ""
    tail = "\n".join(text.splitlines()[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def envelope(summary: str, data=None, next_steps=None) -> dict:
    """Wrap a tool result in the standard bench-scientist response shape:
    a plain-language `summary`, optional `next_steps` suggestions, and the
    structured `data` payload. Key order is summary, next_steps, data."""
    result = {"summary": summary}
    if next_steps:
        result["next_steps"] = next_steps
    result["data"] = data if data is not None else {}
    return result
