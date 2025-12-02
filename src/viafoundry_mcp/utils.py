#!/usr/bin/env python3
"""
Utility functions for ViaFoundry MCP.
"""


def serialize_response(obj):
    """
    Recursively serialize response objects to JSON-compatible dicts.
    Handles Pydantic models, lists, dicts, and other common types.
    """
    if obj is None:
        return None
    
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Pydantic v2 models
    if hasattr(obj, 'model_dump'):
        return serialize_response(obj.model_dump())
    
    if isinstance(obj, dict):
        return {k: serialize_response(v) for k, v in obj.items()}
    
    if isinstance(obj, (list, tuple)):
        return [serialize_response(item) for item in obj]
    
    # Dataclasses, custom objects
    if hasattr(obj, '__dict__'):
        return {k: serialize_response(v) for k, v in obj.__dict__.items() if not k.startswith('_')}
    
    return str(obj)

