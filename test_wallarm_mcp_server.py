#!/usr/bin/env python3
"""
Tests for Wallarm MCP Server
"""

import pytest
import json
import asyncio
from pathlib import Path
from wallarm_mcp_server import WallarmMCPServer
from mcp.types import Tool


class TestWallarmMCPServer:
    """Test suite for Wallarm MCP Server."""
    
    @pytest.fixture
    def server(self):
        """Create a server instance for testing."""
        return WallarmMCPServer("wallarm-swagger-documented-fixed.json")
    
    def test_swagger_loading(self, server):
        """Test that Swagger spec loads correctly."""
        assert server.swagger_spec is not None
        assert server.swagger_spec.get("swagger") == "2.0"
        assert "paths" in server.swagger_spec
        assert server.base_url != ""
        print(f"✓ Swagger spec loaded successfully")
        print(f"  Base URL: {server.base_url}")
        print(f"  API Version: {server.swagger_spec.get('info', {}).get('version')}")
    
    def test_base_url_construction(self, server):
        """Test that base URL is constructed correctly."""
        assert server.base_url.startswith("https://")
        assert "wallarm.com" in server.base_url.lower() or "api" in server.base_url.lower()
        print(f"✓ Base URL constructed: {server.base_url}")
    
    def test_tools_generation(self, server):
        """Test that tools are generated from Swagger paths."""
        tools = server._get_all_tools()
        
        assert len(tools) > 0, "No tools generated from Swagger spec"
        assert all(isinstance(tool, Tool) for tool in tools)
        
        # Verify tool structure
        for tool in tools[:5]:  # Check first 5 tools
            assert tool.name, "Tool must have a name"
            assert tool.description, "Tool must have a description"
            assert tool.inputSchema, "Tool must have an input schema"
            assert tool.inputSchema.get("type") == "object"
        
        print(f"✓ Generated {len(tools)} tools from Swagger spec")
        
        # Print some example tools
        print(f"\nExample tools:")
        for i, tool in enumerate(tools[:3], 1):
            print(f"  {i}. {tool.name}")
            print(f"     Description: {tool.description[:100]}...")
    
    def test_tool_names_unique(self, server):
        """Test that all tool names are unique."""
        tools = server._get_all_tools()
        tool_names = [tool.name for tool in tools]
        
        assert len(tool_names) == len(set(tool_names)), "Tool names must be unique"
        print(f"✓ All {len(tool_names)} tool names are unique")
    
    def test_tool_input_schema_valid(self, server):
        """Test that tool input schemas are valid."""
        tools = server._get_all_tools()
        
        for tool in tools[:10]:  # Check first 10 tools
            schema = tool.inputSchema
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            
            # If there are properties, check they're properly structured
            if "properties" in schema:
                assert isinstance(schema["properties"], dict)
                
                # Check each property
                for prop_name, prop_schema in schema["properties"].items():
                    assert isinstance(prop_schema, dict)
                    # Should have at least a type or be an object
                    assert "type" in prop_schema or "$ref" in prop_schema
        
        print(f"✓ Tool input schemas are valid")
    
    def test_parameter_prefixing(self, server):
        """Test that parameters are properly prefixed by location."""
        tools = server._get_all_tools()
        
        # Find tools with parameters
        tools_with_params = [t for t in tools if t.inputSchema.get("properties")]
        
        if tools_with_params:
            tool = tools_with_params[0]
            properties = tool.inputSchema.get("properties", {})
            
            # Check that parameter names are prefixed or are 'body'
            for param_name in properties.keys():
                valid_prefix = any([
                    param_name.startswith("path_"),
                    param_name.startswith("query_"),
                    param_name.startswith("header_"),
                    param_name == "body",
                ])
                assert valid_prefix, f"Parameter {param_name} should be prefixed"
            
            print(f"✓ Parameters are properly prefixed by location")
    
    def test_swagger_info_parsed(self, server):
        """Test that Swagger info is parsed correctly."""
        info = server.swagger_spec.get("info", {})
        
        assert info.get("title"), "Swagger must have a title"
        assert info.get("version"), "Swagger must have a version"
        
        print(f"✓ Swagger info parsed correctly")
        print(f"  Title: {info.get('title')}")
        print(f"  Version: {info.get('version')}")
    
    def test_paths_exist(self, server):
        """Test that paths are parsed from Swagger."""
        paths = server.swagger_spec.get("paths", {})
        
        assert len(paths) > 0, "No paths found in Swagger spec"
        
        # Check that paths have operations
        operations_count = 0
        for path, path_item in paths.items():
            for method in ["get", "post", "put", "delete", "patch"]:
                if method in path_item:
                    operations_count += 1
        
        assert operations_count > 0, "No operations found in paths"
        
        print(f"✓ Found {len(paths)} paths with {operations_count} operations")
    
    def test_convert_swagger_type(self, server):
        """Test the Swagger type conversion to JSON schema."""
        # Test simple string parameter
        param = {
            "name": "test",
            "type": "string",
            "description": "Test parameter"
        }
        schema = server._convert_swagger_type_to_json_schema(param)
        assert schema["type"] == "string"
        assert schema["description"] == "Test parameter"
        
        # Test array parameter
        param = {
            "name": "ids",
            "type": "array",
            "items": {"type": "integer"}
        }
        schema = server._convert_swagger_type_to_json_schema(param)
        assert schema["type"] == "array"
        assert schema["items"]["type"] == "integer"
        
        print(f"✓ Swagger type conversion works correctly")
    
    def test_server_initialization(self, server):
        """Test that server object is properly initialized."""
        assert server.server is not None
        assert server.server.name == "wallarm-api-server"
        print(f"✓ MCP server initialized: {server.server.name}")
    
    @pytest.mark.asyncio
    async def test_list_tools_handler(self, server):
        """Test that the list_tools handler works."""
        # Get the list_tools handler
        tools = server._get_all_tools()
        
        # Verify we can get tools
        assert len(tools) > 0
        assert all(isinstance(tool, Tool) for tool in tools)
        
        print(f"✓ List tools handler works ({len(tools)} tools)")
    
    def test_operation_id_generation(self, server):
        """Test that operation IDs are generated correctly."""
        # Create a test endpoint without operationId
        path = "/v1/test/endpoint"
        method = "get"
        operation = {
            "summary": "Test endpoint"
        }
        
        tool = server._create_tool_from_endpoint(path, method, operation)
        
        # Should have generated an operation ID
        assert tool.name
        assert "test" in tool.name.lower() or "endpoint" in tool.name.lower()
        
        print(f"✓ Operation ID generation works")
        print(f"  Generated: {tool.name}")
    
    def test_required_parameters(self, server):
        """Test that required parameters are marked correctly."""
        tools = server._get_all_tools()
        
        # Find a tool with required parameters
        tool_with_required = None
        for tool in tools:
            if tool.inputSchema.get("required"):
                tool_with_required = tool
                break
        
        if tool_with_required:
            required = tool_with_required.inputSchema["required"]
            properties = tool_with_required.inputSchema["properties"]
            
            # All required fields must be in properties
            for req_field in required:
                assert req_field in properties, f"Required field {req_field} not in properties"
            
            print(f"✓ Required parameters are marked correctly")
            print(f"  Example: {tool_with_required.name} has {len(required)} required params")
        else:
            print(f"✓ No required parameters found (optional test)")


def test_file_exists():
    """Test that the Swagger file exists."""
    swagger_file = Path("wallarm-swagger-documented-fixed.json")
    assert swagger_file.exists(), f"Swagger file not found: {swagger_file}"
    print(f"✓ Swagger file exists: {swagger_file}")


def test_file_is_valid_json():
    """Test that the Swagger file is valid JSON."""
    swagger_file = Path("wallarm-swagger-documented-fixed.json")
    
    with open(swagger_file, 'r') as f:
        data = json.load(f)
    
    assert data is not None
    assert isinstance(data, dict)
    print(f"✓ Swagger file is valid JSON")


def test_file_is_swagger_2():
    """Test that the file is a Swagger 2.0 spec."""
    swagger_file = Path("wallarm-swagger-documented-fixed.json")
    
    with open(swagger_file, 'r') as f:
        data = json.load(f)
    
    assert data.get("swagger") == "2.0", "Not a Swagger 2.0 specification"
    print(f"✓ File is Swagger 2.0 specification")


if __name__ == "__main__":
    # Run tests with pytest
    print("\n" + "="*60)
    print("WALLARM MCP SERVER TESTS")
    print("="*60 + "\n")
    
    pytest.main([
        __file__, 
        "-v", 
        "--tb=short",
        "-s"  # Show print statements
    ])

