"""最小 MCP stdio server（mcp 2.x），供 MCP 桥接的离线测试使用。"""
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("toy")


@mcp.tool()
def echo(text: str) -> str:
    """回显文本。"""
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
