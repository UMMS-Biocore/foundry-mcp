"""
Tests for utility functions.
"""

from src.foundry_mcp.utils import serialize_response, is_valid_mcp_token, MCP_TOKEN_PREFIX, MAX_SERIALIZATION_DEPTH


class TestIsValidMcpToken:
    """Tests for the is_valid_mcp_token function."""

    def test_valid_mcp_tokens(self):
        """Tokens with via_mcp_ prefix and content after it should be valid."""
        assert is_valid_mcp_token(f"{MCP_TOKEN_PREFIX}abc123") is True
        assert is_valid_mcp_token(f"{MCP_TOKEN_PREFIX}x") is True
        assert is_valid_mcp_token(f"{MCP_TOKEN_PREFIX}longtoken12345") is True

    def test_prefix_only_is_invalid(self):
        """Token with only the prefix (no content after) should be invalid."""
        assert is_valid_mcp_token(MCP_TOKEN_PREFIX) is False

    def test_invalid_tokens(self):
        """Tokens without via_mcp_ prefix should be invalid."""
        assert is_valid_mcp_token("regular_token_12345") is False
        assert is_valid_mcp_token("mcp_token") is False
        assert is_valid_mcp_token("abc123") is False

    def test_empty_and_none(self):
        """Empty and None tokens should be invalid."""
        assert is_valid_mcp_token("") is False
        assert is_valid_mcp_token(None) is False


class TestSerializeResponse:
    """Tests for the serialize_response function."""

    def test_serialize_none(self):
        """Test serializing None."""
        assert serialize_response(None) is None

    def test_serialize_primitives(self):
        """Test serializing primitive types."""
        assert serialize_response("hello") == "hello"
        assert serialize_response(42) == 42
        assert serialize_response(3.14) == 3.14
        assert serialize_response(True) is True
        assert serialize_response(False) is False

    def test_serialize_dict(self):
        """Test serializing dictionaries."""
        data = {"name": "test", "value": 123}
        result = serialize_response(data)
        assert result == {"name": "test", "value": 123}

    def test_serialize_nested_dict(self):
        """Test serializing nested dictionaries."""
        data = {"outer": {"inner": {"value": 42}}}
        result = serialize_response(data)
        assert result == {"outer": {"inner": {"value": 42}}}

    def test_serialize_list(self):
        """Test serializing lists."""
        data = [1, 2, 3, "test"]
        result = serialize_response(data)
        assert result == [1, 2, 3, "test"]

    def test_serialize_tuple(self):
        """Test serializing tuples."""
        data = (1, 2, 3)
        result = serialize_response(data)
        assert result == [1, 2, 3]

    def test_serialize_object_with_dict(self):
        """Test serializing objects with __dict__."""
        class MyObject:
            def __init__(self):
                self.name = "test"
                self.value = 123
                self._private = "hidden"

        obj = MyObject()
        result = serialize_response(obj)
        assert result == {"name": "test", "value": 123}
        assert "_private" not in result

    def test_serialize_circular_reference_dict(self):
        """Test handling circular reference in dict."""
        data = {"name": "test"}
        data["self"] = data  # Circular reference
        
        result = serialize_response(data)
        
        assert result["name"] == "test"
        assert result["self"] == "<circular reference: dict>"

    def test_serialize_circular_reference_list(self):
        """Test handling circular reference in list."""
        data = [1, 2, 3]
        data.append(data)  # Circular reference
        
        result = serialize_response(data)
        
        assert result[0] == 1
        assert result[1] == 2
        assert result[2] == 3
        assert result[3] == "<circular reference: list>"

    def test_serialize_circular_reference_object(self):
        """Test handling circular reference in custom object."""
        class Node:
            def __init__(self, value):
                self.value = value
                self.next = None

        node1 = Node(1)
        node2 = Node(2)
        node1.next = node2
        node2.next = node1  # Circular reference
        
        result = serialize_response(node1)
        
        assert result["value"] == 1
        assert result["next"]["value"] == 2
        assert result["next"]["next"] == "<circular reference: Node>"

    def test_serialize_mutual_circular_reference(self):
        """Test handling mutual circular references."""
        a = {"name": "a"}
        b = {"name": "b"}
        a["ref"] = b
        b["ref"] = a  # Mutual circular reference
        
        result = serialize_response(a)
        
        assert result["name"] == "a"
        assert result["ref"]["name"] == "b"
        assert result["ref"]["ref"] == "<circular reference: dict>"

    def test_serialize_deep_nesting(self):
        """Test that deep nesting is handled up to max depth."""
        # Create deeply nested structure
        data = {"value": 0}
        current = data
        for i in range(MAX_SERIALIZATION_DEPTH + 10):
            current["nested"] = {"value": i + 1}
            current = current["nested"]
        
        result = serialize_response(data)
        
        # Should contain the max depth exceeded marker somewhere
        result_str = str(result)
        assert "max depth exceeded" in result_str

    def test_serialize_non_circular_repeated_object(self):
        """Test that non-circular repeated objects are serialized correctly.
        
        The same object appearing at different (non-circular) positions in the tree
        should serialize correctly each time, because the visited set uses finally
        blocks to discard object IDs after their subtrees complete.
        """
        shared = {"shared": True}
        data = {
            "first": shared,
            "second": shared  # Same object referenced twice, but not circular
        }
        
        result = serialize_response(data)
        
        # Both should be fully serialized - the finally blocks ensure proper cleanup
        assert result["first"] == {"shared": True}
        assert result["second"] == {"shared": True}

    def test_serialize_fallback_to_string(self):
        """Test that unhandled types fall back to string conversion."""
        import datetime
        dt = datetime.datetime(2024, 1, 15, 12, 30, 0)
        
        result = serialize_response(dt)
        
        assert isinstance(result, str)
        assert "2024" in result

