# Wallarm MCP Server

A Model Context Protocol (MCP) server that exposes Wallarm API endpoints as MCP tools based on Swagger 2.0 specification.

## Prerequisites

### Get Wallarm Token

To use this MCP server, you need a Wallarm API token:

1. Log in to your Wallarm account at [https://my.wallarm.com](https://my.wallarm.com)
2. Navigate to **Settings** → **API tokens**
3. Create a new API token or use an existing one
4. Copy the token value (you'll need it for the MCP server configuration)

> **Note:** Keep your API token secure and never commit it to version control.

## Build Image

Build the Docker image for the Wallarm MCP Server:

```bash
docker build -t wallarm-mcp-server:latest .
```

Alternatively, you can use docker-compose:

```bash
docker-compose build
```

## Add MCP Server Config

### Cursor Configuration

Add the following configuration to your Cursor MCP settings file (typically located at `~/Library/Application Support/Cursor/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` or in your Cursor settings):

```json
{
  "mcpServers": {
    "wallarm": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e",
        "WALLARM_API_TOKEN=your_wallarm_api_token_here",
        "wallarm-mcp-server:latest"
      ]
    }
  }
}
```

**Important:** Replace `your_wallarm_api_token_here` with your actual Wallarm API token.

### Alternative: Using docker-compose

If you prefer using docker-compose, you can configure it as:

```json
{
  "mcpServers": {
    "wallarm": {
      "command": "docker-compose",
      "args": [
        "-f",
        "/path/to/wallarm-mcp-server/docker-compose.yml",
        "run",
        "--rm",
        "wallarm-mcp"
      ],
      "env": {
        "WALLARM_API_TOKEN": "your_wallarm_api_token_here"
      }
    }
  }
}
```

After adding the configuration, restart Cursor to load the MCP server.

