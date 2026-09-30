"""A streamable-HTTP MCP server with one tool, `echo`, for the real-CLI tests.

    python fake_mcp_server.py <port> <auth_log>

Appends each request's Authorization header to <auth_log>, so a test can check that
the CLI presented the turn token (X-Turn-Token) to the per-turn MCP server.
"""

import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

PORT, AUTH_LOG = int(sys.argv[1]), sys.argv[2]

server = MCPServer("fake-tools")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


mcp_app = server.streamable_http_app(stateless_http=True, json_response=True)


async def app(scope, receive, send):
    if scope["type"] == "http":
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        with open(AUTH_LOG, "a") as f:
            f.write(auth + "\n")
    await mcp_app(scope, receive, send)


uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
