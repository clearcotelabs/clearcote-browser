# Clearcote MCP bundle (.mcpb)

An [MCPB](https://github.com/modelcontextprotocol/mcpb) bundle of the Clearcote MCP server, for one-click installs
(Claude Desktop and other MCPB hosts). It uses the `uv` server type: the bundle only pins `clearcote-mcp` from PyPI and the host
installs it with uv, so the bundle stays small and runs on Windows, Linux and macOS.

Build it (bump `version` in manifest.json and the pin in pyproject.toml with each clearcote-mcp release):

```bash
npx @anthropic-ai/mcpb pack mcp/mcpb clearcote-mcp.mcpb
```
