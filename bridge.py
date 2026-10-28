#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
bridge.py - Snapgent bridge for Linux (community reimplementation).

The upstream snapgent-connector repo ships only a Windows bridge.exe; the
bridge.py source referenced by its MacOS_Start.command is not published
anywhere (repo tree, release tags, release assets, or the docs site).
This file re-implements the bridge side of the protocol exactly as the
upstream browser extension expects it (see snapgent-extension/background.js
in the upstream repo):

  extension  <-- WebSocket ws://127.0.0.1:17613, JSON {type, id, ...} -->
  this bridge <-- stdio JSON-RPC (newline-delimited MCP) -->
  vscode_mcp.py (upstream, unmodified)

Server configuration is read from config.json next to this file, in the
same shape the Windows setup-config.ps1 writes:

  {"mcpServers": {"vscode": {"command": "python3",
                             "args": ["/path/to/vscode_mcp.py"],
                             "env": {"APPDATA": "/home/you/.config"}}}}

Linux note: upstream vscode_mcp.py locates the active VS Code workspace via
%APPDATA%/Code/User/globalStorage/storage.json. Linux VS Code stores the
very same file at ~/.config/Code/User/globalStorage/storage.json, so
setting APPDATA=$HOME/.config in the server env makes workspace detection
work unmodified.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SNAPGENT_CONFIG", str(HERE / "config.json")))
HOST = "127.0.0.1"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
SERVER_ID = "vscode"


def load_server_config():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    server = cfg["mcpServers"][SERVER_ID]
    command = server["command"]
    args = list(server.get("args") or [])
    env = dict(os.environ)
    env.update(server.get("env") or {})
    return command, args, env


class McpProcess:
    """One MCP server subprocess spoken to over stdio JSON-RPC."""

    def __init__(self):
        self.proc = None
        self._pending = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._reader_task = None
        self.tools = []

    async def start(self):
        command, args, env = load_server_config()
        self.proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        await self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "snapgent-bridge-linux", "version": "0.1.0"},
        })
        await self.notify("notifications/initialized", {})
        await self.refresh_tools()

    async def _drain_stderr(self):
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                sys.stderr.write("[mcp stderr] " + line.decode("utf-8", "replace"))
        except Exception:
            return

    async def _read_loop(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("MCP server process exited"))
        self._pending.clear()

    async def request(self, method, params, timeout=120):
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError("MCP server is not running")
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self.proc.stdin.drain()
            msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "MCP error"))
        return msg.get("result") or {}

    async def notify(self, method, params):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def refresh_tools(self):
        result = await self.request("tools/list", {})
        self.tools = [dict(t, server=SERVER_ID) for t in result.get("tools", [])]
        return self.tools

    async def call_tool(self, name, arguments, timeout=120):
        result = await self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return text

    async def restart(self):
        await self.stop()
        await self.start()

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.tools = []

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None


MCP = McpProcess()


def servers_payload():
    return [{"id": SERVER_ID, "alive": MCP.alive, "tools": len(MCP.tools)}]


async def handle_message(ws, msg):
    mtype = msg.get("type")
    mid = msg.get("id")
    if mtype == "ping":
        await ws.send(json.dumps({"type": "pong", "id": mid}))
    elif mtype == "studio_status":
        await ws.send(json.dumps({
            "type": "studio_status", "id": mid,
            "studio": False, "studio_app": False, "studio_proc": False,
        }))
    elif mtype == "list_tools":
        if MCP.alive:
            try:
                await MCP.refresh_tools()
            except Exception:
                pass
        await ws.send(json.dumps({
            "type": "tools", "id": mid, "tools": MCP.tools,
            "servers": servers_payload(), "mcp_alive": MCP.alive,
        }))
    elif mtype == "call_tool":
        timeout = (msg.get("timeout") or 120000) / 1000 + 10
        try:
            text = await MCP.call_tool(msg.get("name", ""), msg.get("arguments"), timeout=timeout)
            await ws.send(json.dumps({
                "type": "tool_result", "id": mid, "ok": True, "text": text, "images": [],
            }))
        except Exception as error:
            await ws.send(json.dumps({
                "type": "tool_result", "id": mid, "ok": False,
                "kind": "mcp_error", "error": str(error),
            }))
    elif mtype == "restart_mcp":
        try:
            await MCP.restart()
            await ws.send(json.dumps({
                "type": "mcp_status", "id": mid, "ok": True, "alive": MCP.alive,
                "tools": MCP.tools, "servers": servers_payload(),
            }))
        except Exception as error:
            await ws.send(json.dumps({
                "type": "mcp_status", "id": mid, "ok": False,
                "alive": False, "error": str(error),
            }))
    elif mtype in ("add_server", "remove_server"):
        await ws.send(json.dumps({
            "type": "server_changed", "id": mid, "ok": False, "restarting": False,
            "error": "add/remove server is not supported by the Linux bridge shim; "
                     "edit config.json and restart the bridge instead.",
        }))
    else:
        await ws.send(json.dumps({
            "type": "error", "id": mid, "error": f"unknown message type: {mtype}",
        }))


async def handle_connection(ws):
    print(f"[bridge] extension connected from {ws.remote_address}", flush=True)
    if not MCP.alive:
        try:
            await MCP.start()
        except Exception as error:
            print(f"[bridge] MCP start failed: {error}", flush=True)
    await ws.send(json.dumps({
        "type": "connected", "mcp_alive": MCP.alive,
        "tools": MCP.tools, "servers": servers_payload(),
    }))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_message(ws, msg)
    except websockets.ConnectionClosed:
        pass
    print("[bridge] extension disconnected", flush=True)


async def main():
    try:
        await MCP.start()
        print(f"[bridge] MCP server '{SERVER_ID}' up with {len(MCP.tools)} tools", flush=True)
    except Exception as error:
        print(f"[bridge] MCP server failed to start ({error}); "
              "the bridge will retry when the extension connects.", flush=True)
    async with websockets.serve(handle_connection, HOST, PORT):
        print(f"[bridge] listening on ws://{HOST}:{PORT} - keep this terminal open", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
