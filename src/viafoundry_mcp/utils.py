#!/usr/bin/env python3
"""
Utility functions for ViaFoundry MCP.
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
