#!/usr/bin/env python3
"""
MCP ViaFoundry Server Integration Tests

This script tests the MCP server by verifying tool registration
and basic server functionality.
"""

import pytest

class TestMCPTools:
    """Test MCP tool registration and availability."""
    
    def test_tools_registered(self):
        """Verify that tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        
        assert tools, "No tools were registered"
    
    def test_report_tools_registered(self):
        """Verify report management tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_report_tools = [
            'fetch_report',
            'list_processes',
            'list_files',
            'download_file',
            'load_file',
            'upload_file',
            'get_report_dirs',
            'get_all_report_paths',
        ]
        
        for tool in expected_report_tools:
            assert tool in tool_names, f"Missing report tool: {tool}"
    
    def test_run_tools_registered(self):
        """Verify run management tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_run_tools = [
            'list_runs',
            'get_run',
            'get_run_details',
            'create_vmeta_dataset',
            'duplicate_run',
            'update_run',
            'initiate_run',
        ]
        
        for tool in expected_run_tools:
            assert tool in tool_names, f"Missing run tool: {tool}"
    
    def test_process_tools_registered(self):
        """Verify process management tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_process_tools = [
            'list_all_processes',
            'get_process_details',
            'get_process_revisions',
            'list_process_parameters',
            'duplicate_process',
            'get_process_parameters',
            'create_process_config',
            'create_process',
            'create_process_parameter',
            'update_process',
        ]
        
        for tool in expected_process_tools:
            assert tool in tool_names, f"Missing process tool: {tool}"
    
    def test_menu_group_tools_registered(self):
        """Verify menu group tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_menu_tools = [
            'create_menu_group',
            'list_menu_groups',
            'get_menu_group_by_name',
        ]
        
        for tool in expected_menu_tools:
            assert tool in tool_names, f"Missing menu group tool: {tool}"
    
    def test_metadata_tools_registered(self):
        """Verify metadata tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_metadata_tools = [
            'search_datasets',
            'search_collections',
            'get_collection_details',
            'create_collection',
            'add_files_to_dataset',
            'get_collection_fields',
            'search_canvas',
            'get_canvas_details',
            'get_canvas_fields',
            'create_canvas',
            'search_metadata_fields',
            'get_field_details',
            'create_metadata_field',
            'search_metadata_records',
            'get_metadata_record',
            'create_metadata_record',
        ]
        
        for tool in expected_metadata_tools:
            assert tool in tool_names, f"Missing metadata tool: {tool}"
    
    def test_app_tools_registered(self):
        """Verify app launch tools are registered."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        tool_names = list(tools.keys())
        
        expected_app_tools = [
            'list_apps',
            'discover_app_endpoints',
            'launch_app',
        ]
        
        for tool in expected_app_tools:
            assert tool in tool_names, f"Missing app tool: {tool}"
    
    def test_all_tools_have_docstrings(self):
        """Verify all tools have documentation."""
        from src.viafoundry_mcp.server import mcp
        
        tools = mcp._tool_manager._tools
        
        for name, tool in tools.items():
            assert tool.fn.__doc__, f"Tool {name} is missing documentation"


class TestUtilities:
    """Test utility functions."""
    
    def test_serialize_response_primitives(self):
        """Test serialization of primitive types."""
        from src.viafoundry_mcp.utils import serialize_response
        
        assert serialize_response(None) is None
        assert serialize_response("test") == "test"
        assert serialize_response(42) == 42
        assert serialize_response(3.14) == 3.14
        assert serialize_response(True) is True
    
    def test_serialize_response_dict(self):
        """Test serialization of dictionaries."""
        from src.viafoundry_mcp.utils import serialize_response
        
        data = {"key": "value", "nested": {"a": 1}}
        result = serialize_response(data)
        
        assert result == data
    
    def test_serialize_response_list(self):
        """Test serialization of lists."""
        from src.viafoundry_mcp.utils import serialize_response
        
        data = [1, 2, {"key": "value"}]
        result = serialize_response(data)
        
        assert result == data
    
    def test_serialize_response_circular_reference(self):
        """Test that circular references are handled."""
        from src.viafoundry_mcp.utils import serialize_response
        
        data = {"key": "value"}
        data["self"] = data  # Create circular reference
        
        result = serialize_response(data)
        
        assert "circular reference" in str(result["self"])


class TestConfig:
    """Test configuration functions."""
    
    def test_validate_credentials_valid(self):
        """Test validation of valid credentials."""
        from src.viafoundry_mcp.config import validate_credentials
        
        assert validate_credentials("https://example.com", "via_mcp_token123")
        assert validate_credentials("http://localhost:8080", "via_mcp_test")
    
    def test_validate_credentials_invalid(self):
        """Test validation of invalid credentials."""
        from src.viafoundry_mcp.config import validate_credentials
        
        # Missing values
        assert not validate_credentials("", "via_mcp_token")
        assert not validate_credentials("https://example.com", "")
        assert not validate_credentials(None, "via_mcp_token")
        
        # Invalid hostname format
        assert not validate_credentials("example.com", "via_mcp_token")
        
        # Invalid token prefix
        assert not validate_credentials("https://example.com", "invalid_token")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
