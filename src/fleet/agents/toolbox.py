"""Sync access to an MCP server's tools, for the (sync) LangGraph agent.

The MCP client is async. Each toolbox owns a private event loop on a background thread, so the
graph, the Postgres checkpointer and the Anthropic client can all stay synchronous. That also
sidesteps a Windows trap: async psycopg needs a selector loop, subprocesses need a proactor loop.
"""

import asyncio
import json
import sys
import threading
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver import MCPServer

T = TypeVar("T")


class ToolBox(Protocol):
    def specs(self) -> list[dict[str, Any]]:
        """Anthropic tool definitions, sorted by name so the prompt prefix stays cacheable."""
        ...

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (text, is_error)."""
        ...

    def close(self) -> None: ...


def to_anthropic(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in sorted(tools, key=lambda t: t.name)
    ]


def result_text(result: Any) -> tuple[str, bool]:
    """Flatten an MCP CallToolResult. A JSON body with an "error" key counts as an error."""
    text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    is_error = bool(getattr(result, "is_error", False))
    try:
        body = json.loads(text)
        is_error = is_error or (isinstance(body, dict) and "error" in body)
    except json.JSONDecodeError:
        pass
    return text, is_error


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 60) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


class InProcessToolBox:
    """Calls an MCPServer object directly. Same tool definitions, no subprocess (tests)."""

    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self._loop = _LoopThread()
        self._specs = to_anthropic(self._loop.run(server.list_tools()))

    def specs(self) -> list[dict[str, Any]]:
        return self._specs

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            return result_text(self._loop.run(self._server.call_tool(name, arguments)))
        except Exception as e:  # unknown tool or bad arguments: tell the model, don't crash
            return json.dumps({"error": str(e)}), True

    def close(self) -> None:
        self._loop.stop()


class StdioToolBox:
    """Spawns an MCP server as a subprocess and talks to it over stdio."""

    def __init__(self, module: str, env: dict[str, str] | None = None) -> None:
        self._loop = _LoopThread()
        params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
        self._ready: asyncio.Future[ClientSession] = self._loop.loop.create_future()
        self._closing = asyncio.Event()
        # One long-lived task owns the connection: anyio cancel scopes must be exited by the
        # task that entered them, so open and close happen in the same coroutine.
        self._task = asyncio.run_coroutine_threadsafe(self._serve(params), self._loop.loop)
        self._session = self._loop.run(self._wait_ready())
        self._specs = to_anthropic(self._loop.run(self._session.list_tools()).tools)

    async def _wait_ready(self) -> ClientSession:
        return await self._ready

    async def _serve(self, params: StdioServerParameters) -> None:
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
                await s.initialize()
                self._ready.set_result(s)
                await self._closing.wait()
        except Exception as e:
            if not self._ready.done():
                self._ready.set_exception(e)
            raise

    def specs(self) -> list[dict[str, Any]]:
        return self._specs

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            return result_text(self._loop.run(self._session.call_tool(name, arguments)))
        except Exception as e:
            return json.dumps({"error": str(e)}), True

    def close(self) -> None:
        self._loop.loop.call_soon_threadsafe(self._closing.set)
        try:
            self._task.result(timeout=10)
        finally:
            self._loop.stop()
