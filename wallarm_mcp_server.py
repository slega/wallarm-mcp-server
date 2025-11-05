#!/usr/bin/env python3
"""
Wallarm MCP Server
Exposes Wallarm API endpoints as MCP tools based on Swagger 2.0 specification.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from pathlib import Path
import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wallarm-mcp-server")

class WallarmMCPServer:
    """MCP Server for Wallarm API based on Swagger 2.0 spec."""
    
    def __init__(self, swagger_file: str = "wallarm-swagger-documented-fixed.json"):
        self.swagger_file = swagger_file
        self.swagger_spec: Dict[str, Any] = {}
        self.base_url = ""
        self.server = Server("wallarm-api-server")
        self._load_swagger()
        self._register_handlers()
        
    def _load_swagger(self) -> None:
        """Load and parse the Swagger 2.0 specification."""
        swagger_path = Path(self.swagger_file)
        if not swagger_path.exists():
            raise FileNotFoundError(f"Swagger file not found: {self.swagger_file}")
        
        logger.info(f"Loading Swagger spec from {self.swagger_file}")
        with open(swagger_path, 'r') as f:
            self.swagger_spec = json.load(f)
        
        # Extract base URL from swagger spec
        scheme = self.swagger_spec.get("schemes", ["https"])[0]
        host = self.swagger_spec.get("host", "us1.api.wallarm.com")
        base_path = self.swagger_spec.get("basePath", "/")
        self.base_url = f"{scheme}://{host}{base_path}".rstrip("/")
        
        logger.info(f"Loaded Swagger {self.swagger_spec.get('swagger')} spec")
        logger.info(f"API: {self.swagger_spec.get('info', {}).get('title')}")
        logger.info(f"Version: {self.swagger_spec.get('info', {}).get('version')}")
        logger.info(f"Base URL: {self.base_url}")
        
    def _convert_swagger_type_to_json_schema(self, swagger_param: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Swagger 2.0 parameter to JSON schema."""
        schema: Dict[str, Any] = {}
        
        # Handle schema reference
        if "schema" in swagger_param:
            param_schema = swagger_param["schema"]
            if "$ref" in param_schema:
                # For simplicity, we'll accept any object for refs
                return {"type": "object"}
            return param_schema
        
        # Direct type mapping
        param_type = swagger_param.get("type", "string")
        schema["type"] = param_type
        
        if "description" in swagger_param:
            schema["description"] = swagger_param["description"]
        
        if "enum" in swagger_param:
            schema["enum"] = swagger_param["enum"]
        
        if param_type == "array" and "items" in swagger_param:
            schema["items"] = swagger_param["items"]
        
        if "default" in swagger_param:
            schema["default"] = swagger_param["default"]
            
        return schema
    
    def _create_tool_from_endpoint(
        self, 
        path: str, 
        method: str, 
        operation: Dict[str, Any]
    ) -> Tool:
        """Create an MCP Tool from a Swagger endpoint."""
        import re
        
        # Create a unique tool name
        operation_id = operation.get("operationId", "")
        if not operation_id:
            # Generate operation ID from path and method
            operation_id = f"{method}_{path.replace('/', '_').replace('{', '').replace('}', '').strip('_')}"
        
        # Sanitize tool name: only alphanumeric, underscore, and hyphen allowed
        # Replace any invalid characters with underscore
        operation_id = re.sub(r'[^a-zA-Z0-9_-]', '_', operation_id)
        
        # Remove consecutive underscores/hyphens
        operation_id = re.sub(r'[_-]+', '_', operation_id)
        
        # Ensure it starts with a letter or number
        operation_id = re.sub(r'^[^a-zA-Z0-9]+', '', operation_id)
        
        # Ensure it doesn't end with underscore or hyphen
        operation_id = operation_id.rstrip('_-')
        
        # Ensure tool name is 64 characters or less
        if len(operation_id) > 64:
            operation_id = operation_id[:64].rstrip('_-')
        
        # Fallback if empty after sanitization
        if not operation_id:
            operation_id = f"tool_{abs(hash(path + method)) % 100000}"
        
        # Get summary and description
        summary = operation.get("summary", "")
        description = operation.get("description", summary)
        
        # Build input schema from parameters
        properties: Dict[str, Any] = {}
        required: List[str] = []
        
        # Process parameters
        parameters = operation.get("parameters", [])
        for param in parameters:
            param_name = param.get("name", "")
            param_in = param.get("in", "")
            
            # Create a prefixed parameter name to avoid conflicts
            if param_in == "path":
                prefixed_name = f"path_{param_name}"
            elif param_in == "query":
                prefixed_name = f"query_{param_name}"
            elif param_in == "header":
                prefixed_name = f"header_{param_name}"
            elif param_in == "body":
                prefixed_name = "body"
            else:
                prefixed_name = param_name
            
            # Convert parameter to JSON schema
            param_schema = self._convert_swagger_type_to_json_schema(param)
            
            # Add description with location info
            param_desc = param.get("description", "")
            if param_in:
                param_desc = f"[{param_in}] {param_desc}".strip()
            
            if param_desc:
                param_schema["description"] = param_desc
            
            properties[prefixed_name] = param_schema
            
            # Check if required
            if param.get("required", False):
                required.append(prefixed_name)
        
        # Build the tool
        input_schema = {
            "type": "object",
            "properties": properties,
        }
        
        if required:
            input_schema["required"] = required
        
        # Create full description
        full_description = f"{summary}\n\nEndpoint: {method.upper()} {path}"
        if description and description != summary:
            full_description += f"\n\n{description}"
        
        return Tool(
            name=operation_id,
            description=full_description,
            inputSchema=input_schema,
        )
    
    def _get_all_tools(self) -> List[Tool]:
        """Generate all tools from Swagger paths."""
        tools: List[Tool] = []
        paths = self.swagger_spec.get("paths", {})
        
        for path, path_item in paths.items():
            for method in ["get", "post", "put", "delete", "patch", "head", "options"]:
                if method in path_item:
                    operation = path_item[method]
                    if isinstance(operation, dict):
                        tool = self._create_tool_from_endpoint(path, method, operation)
                        tools.append(tool)
                        logger.debug(f"Registered tool: {tool.name}")
        
        logger.info(f"Generated {len(tools)} tools from Swagger spec")
        return tools
    
    async def _execute_api_call(
        self,
        path: str,
        method: str,
        parameters: Dict[str, Any],
        api_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Execute an API call to Wallarm."""
        # Build the URL with path parameters
        url = self.base_url + path
        
        # Separate parameters by type
        path_params = {}
        query_params = {}
        headers = {}
        body = None
        
        for key, value in parameters.items():
            if key.startswith("path_"):
                param_name = key[5:]  # Remove "path_" prefix
                path_params[param_name] = value
            elif key.startswith("query_"):
                param_name = key[6:]  # Remove "query_" prefix
                query_params[param_name] = value
            elif key.startswith("header_"):
                param_name = key[7:]  # Remove "header_" prefix
                headers[param_name] = value
            elif key == "body":
                body = value
        
        # Replace path parameters
        for param_name, param_value in path_params.items():
            url = url.replace(f"{{{param_name}}}", str(param_value))
        
        # Add authorization header if token provided
        if api_token:
            headers["X-WallarmApi-Token"] = api_token
        
        # Set content type for body requests
        if body is not None:
            headers["Content-Type"] = "application/json"
        
        # Make the API call
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.request(
                    method=method.upper(),
                    url=url,
                    params=query_params,
                    headers=headers,
                    json=body if body is not None else None,
                )
                
                # Parse response
                result = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                }
                
                # Try to parse JSON response
                try:
                    result["body"] = response.json()
                except:
                    result["body"] = response.text
                
                return result
                
            except Exception as e:
                logger.error(f"API call failed: {e}")
                return {
                    "error": str(e),
                    "status_code": 0,
                }
    
    def _register_handlers(self) -> None:
        """Register MCP server handlers."""
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List all available tools."""
            return self._get_all_tools()
        
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> List[TextContent]:
            """Execute a tool (API call)."""
            logger.info(f"Calling tool: {name}")
            logger.debug(f"Arguments: {arguments}")
            
            # Find the matching endpoint
            paths = self.swagger_spec.get("paths", {})
            for path, path_item in paths.items():
                for method in ["get", "post", "put", "delete", "patch", "head", "options"]:
                    if method in path_item:
                        operation = path_item[method]
                        if isinstance(operation, dict):
                            operation_id = operation.get("operationId")
                            if not operation_id:
                                operation_id = f"{method}_{path.replace('/', '_').replace('{', '').replace('}', '').strip('_')}"
                            
                            if operation_id == name:
                                # Execute the API call
                                # Read API token from environment variable
                                api_token = os.getenv("WALLARM_API_TOKEN")
                                result = await self._execute_api_call(
                                    path,
                                    method,
                                    arguments or {},
                                    api_token=api_token
                                )
                                
                                return [
                                    TextContent(
                                        type="text",
                                        text=json.dumps(result, indent=2)
                                    )
                                ]
            
            # Tool not found
            return [
                TextContent(
                    type="text",
                    text=f"Error: Tool '{name}' not found"
                )
            ]
    
    async def run(self) -> None:
        """Run the MCP server."""
        logger.info("Starting Wallarm MCP Server...")
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


async def main():
    """Main entry point."""
    server = WallarmMCPServer()
    await server.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

