#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
bridge.py - Snapgent bridge for Linux (community reimplementation), v0.2.

The upstream snapgent-connector repo ships only a Windows bridge.exe; the
bridge.py source referenced by its MacOS_Start.command is not published
anywhere (repo tree, release tags, release assets, or the docs site).
This file re-implements the bridge side of the protocol exactly as the
upstream browser extension expects it (see snapgent-extension/background.js
in the upstream repo):

  extension  <-- WebSocket ws://127.0.0.1:17613, JSON {type, id, ...} -->
  this bridge <-- MCP (stdio / SSE / Streamable HTTP) -->
  every server configured in config.json

config.json uses the same shape the Windows setup-config.ps1 writes, and
every entry under "mcpServers" is served simultaneously:

  {"mcpServers": {
      "vscode":         {"command": "python3", "args": ["/path/vscode_mcp.py"],
                         "env": {"APPDATA": "/home/you/.config"}},
      "android-studio": {"url": "http://127.0.0.1:64342/sse"}
  }}

- stdio entries ("command") are spawned as subprocesses (newline-delimited
  JSON-RPC MCP over stdio).
- URL entries ("url") are spoken to over HTTP: legacy SSE transport when
  the URL path ends with /sse, Streamable HTTP otherwise. This covers the
  MCP Server built into JetBrains IDEs (Android Studio, IntelliJ, ...)
  since 2025.2: in the IDE, Settings | Tools | MCP Server, enable the
  server, then use "Copy SSE Config" / "Copy HTTP Stream Config" and paste
  the URL here.

Tool routing: tools are advertised merged, each tagged with its server id.
When two servers expose the same tool name, the later server's copy is
advertised prefixed as "server/name" (the upstream convention); calls may
use the bare name (routed to the first owner) or the prefixed name.

Linux note for the VS Code server: upstream vscode_mcp.py locates the
active VS Code workspace via %APPDATA%/Code/User/globalStorage/storage.json.
Linux VS Code stores the very same file at
~/.config/Code/User/globalStorage/storage.json, so setting
APPDATA=$HOME/.config in the server env makes workspace detection work
unmodified.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import websockets

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SNAPGENT_CONFIG", str(HERE / "config.json")))
HOST = "127.0.0.1"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
CLIENT_INFO = {"name": "snapgent-bridge-linux", "version": "0.2.0"}


def read_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mcpServers": {}}


def write_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Minimal HTTP client (asyncio streams) - enough for MCP HTTP transports.
# ---------------------------------------------------------------------------

async def _open_http(url):
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise RuntimeError(f"only http:// MCP URLs are supported (got {parsed.scheme}://)")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    reader, writer = await asyncio.open_connection(host, port)
    return parsed, reader, writer


def _request_head(method, parsed, headers):
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    lines = [f"{method} {path} HTTP/1.1", f"Host: {parsed.netloc}"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _read_head(reader):
    raw = await reader.readuntil(b"\r\n\r\n")
    head = raw.decode("iso-8859-1")
    lines = head.split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key.lower()] = value
    return status, headers


async def _read_body(reader, headers):
    if "chunked" in headers.get("transfer-encoding", ""):
        out = b""
        while True:
            size_line = await reader.readline()
            size = int(size_line.strip().split(b";")[0], 16)
            if size == 0:
                await reader.readline()
                return out
            out += await reader.readexactly(size)
            await reader.readline()
    length = headers.get("content-length")
    if length is not None:
        return await reader.readexactly(int(length))
    return await reader.read()  # until EOF (we send Connection: close)


async def http_post_json(url, payload, extra_headers=None):
    """POST one JSON-RPC message; returns (status, headers, body bytes)."""
    parsed, reader, writer = await _open_http(url)
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    writer.write(_request_head("POST", parsed, headers) + body)
    await writer.drain()
    status, resp_headers = await _read_head(reader)
    resp_body = await _read_body(reader, resp_headers)
    writer.close()
    return status, resp_headers, resp_body


def parse_sse_events(text):
    """Yield (event, data) pairs from an SSE document or chunk."""
    event, data_lines = "message", []
    for line in text.splitlines():
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        yield event, "\n".join(data_lines)


# ---------------------------------------------------------------------------
# MCP server transports.
# ---------------------------------------------------------------------------

class BaseServer:
    def __init__(self, server_id, entry):
        self.id = server_id
        self.entry = entry
        self.tools = []
        self.last_error = None

    @property
    def alive(self):
        return False

    async def start(self):
        raise NotImplementedError

    async def stop(self):
        self.tools = []

    async def restart(self):
        await self.stop()
        await self.start()

    async def refresh_tools(self):
        result = await self.request("tools/list", {})
        self.tools = list(result.get("tools", []))
        return self.tools

    async def request(self, method, params, timeout=120):
        raise NotImplementedError

    async def notify(self, method, params):
        raise NotImplementedError

    async def handshake(self):
        await self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        await self.notify("notifications/initialized", {})
        await self.refresh_tools()

    async def call_tool(self, name, arguments, timeout=120):
        result = await self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        content = result.get("content") or []
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


class StdioServer(BaseServer):
    def __init__(self, server_id, entry):
        super().__init__(server_id, entry)
        self.proc = None
        self._pending = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._reader_task = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        env = dict(os.environ)
        env.update(self.entry.get("env") or {})
        self.proc = await asyncio.create_subprocess_exec(
            self.entry["command"], *(self.entry.get("args") or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        await self.handshake()

    async def _drain_stderr(self):
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                sys.stderr.write(f"[mcp {self.id} stderr] " + line.decode("utf-8", "replace"))
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
                fut.set_exception(RuntimeError(f"MCP server '{self.id}' process exited"))
        self._pending.clear()

    async def request(self, method, params, timeout=120):
        if not self.alive:
            raise RuntimeError(f"MCP server '{self.id}' is not running")
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


class StreamableHttpServer(BaseServer):
    """MCP Streamable HTTP transport: one POST endpoint, JSON or SSE replies."""

    def __init__(self, server_id, entry):
        super().__init__(server_id, entry)
        self.url = entry["url"]
        self.headers = dict(entry.get("headers") or {})
        self.session_id = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def alive(self):
        return self._started

    async def start(self):
        self._started = True
        try:
            await self.handshake()
        except Exception:
            self._started = False
            raise

    async def stop(self):
        self._started = False
        self.session_id = None
        self.tools = []

    async def request(self, method, params, timeout=120):
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            msg = await asyncio.wait_for(self._send(payload, rid), timeout)
        if msg is None:
            raise RuntimeError(f"no response from MCP server '{self.id}'")
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "MCP error"))
        return msg.get("result") or {}

    async def _send(self, payload, rid):
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, resp_headers, body = await http_post_json(self.url, payload, headers)
        if resp_headers.get("mcp-session-id"):
            self.session_id = resp_headers["mcp-session-id"]
        if status >= 400:
            raise RuntimeError(f"MCP server '{self.id}' answered HTTP {status}")
        if not body:
            return None
        ctype = resp_headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for _event, data in parse_sse_events(body.decode("utf-8", "replace")):
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == rid:
                    return msg
            return None
        return json.loads(body.decode("utf-8", "replace"))

    async def notify(self, method, params):
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await http_post_json(self.url, payload, headers)


class SseServer(BaseServer):
    """MCP legacy SSE transport: GET opens the event stream and yields the
    POST endpoint; responses return as 'message' events on that stream."""

    def __init__(self, server_id, entry):
        super().__init__(server_id, entry)
        self.url = entry["url"]
        self.headers = dict(entry.get("headers") or {})
        self._next_id = 0
        self._pending = {}
        self._lock = asyncio.Lock()
        self._writer = None
        self._reader_task = None
        self._endpoint = None
        self._endpoint_ready = None

    @property
    def alive(self):
        return self._writer is not None and self._endpoint is not None

    async def start(self):
        self._endpoint_ready = asyncio.Event()
        parsed, reader, writer = await _open_http(self.url)
        self._writer = writer
        headers = {"Accept": "text/event-stream", **self.headers}
        writer.write(_request_head("GET", parsed, headers))
        await writer.drain()
        status, _resp_headers = await _read_head(reader)
        if status != 200:
            writer.close()
            self._writer = None
            raise RuntimeError(f"SSE connect to '{self.id}' answered HTTP {status}")
        self._reader_task = asyncio.create_task(self._read_loop(reader))
        await asyncio.wait_for(self._endpoint_ready.wait(), 10)
        await self.handshake()

    async def _read_loop(self, reader):
        buffer = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    idx_nn = buffer.find(b"\n\n")
                    idx_rr = buffer.find(b"\r\n\r\n")
                    candidates = [i for i in (idx_nn, idx_rr) if i >= 0]
                    if not candidates:
                        break
                    idx = min(candidates)
                    sep_len = 4 if buffer[idx:idx + 4] == b"\r\n\r\n" else 2
                    raw, buffer = buffer[:idx], buffer[idx + sep_len:]
                    for event, data in parse_sse_events(raw.decode("utf-8", "replace")):
                        if event == "endpoint":
                            self._endpoint = urljoin(self.url, data.strip())
                            self._endpoint_ready.set()
                        elif event == "message":
                            try:
                                msg = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            fut = self._pending.pop(msg.get("id"), None)
                            if fut is not None and not fut.done():
                                fut.set_result(msg)
        except Exception:
            pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"SSE stream from '{self.id}' closed"))
        self._pending.clear()

    async def request(self, method, params, timeout=120):
        if not self.alive:
            raise RuntimeError(f"MCP server '{self.id}' is not connected")
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            status, _h, _b = await http_post_json(self._endpoint, payload, self.headers)
            if status >= 300:
                self._pending.pop(rid, None)
                raise RuntimeError(f"MCP server '{self.id}' POST answered HTTP {status}")
            msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "MCP error"))
        return msg.get("result") or {}

    async def notify(self, method, params):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await http_post_json(self._endpoint, payload, self.headers)

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()
        self._writer = None
        self._endpoint = None
        self.tools = []


def make_server(server_id, entry):
    if entry.get("url"):
        url = entry["url"]
        transport = entry.get("transport") or (
            "sse" if urlparse(url).path.endswith("/sse") else "stream")
        if transport == "sse":
            return SseServer(server_id, entry)
        return StreamableHttpServer(server_id, entry)
    return StdioServer(server_id, entry)


# ---------------------------------------------------------------------------
# Registry: every configured server, one merged tool catalogue.
# ---------------------------------------------------------------------------

class Registry:
    def __init__(self):
        self.servers = {}

    def load_from_config(self):
        cfg = read_config()
        for server_id, entry in (cfg.get("mcpServers") or {}).items():
            if server_id not in self.servers:
                self.servers[server_id] = make_server(server_id, entry)

    async def start_server(self, server):
        try:
            await server.start()
            server.last_error = None
            print(f"[bridge] MCP server '{server.id}' up with {len(server.tools)} tools", flush=True)
        except Exception as error:
            server.last_error = str(error)
            print(f"[bridge] MCP server '{server.id}' failed to start: {error}", flush=True)

    async def start_all(self):
        self.load_from_config()
        await asyncio.gather(*(self.start_server(s) for s in self.servers.values()))

    async def ensure_started(self):
        self.load_from_config()
        down = [s for s in self.servers.values() if not s.alive]
        if down:
            await asyncio.gather(*(self.start_server(s) for s in down))

    @property
    def any_alive(self):
        return any(s.alive for s in self.servers.values())

    def servers_payload(self):
        return [{"id": s.id, "alive": s.alive, "tools": len(s.tools)}
                for s in self.servers.values()]

    def catalog(self):
        """Merged tools tagged per server; collisions advertised 'server/name'
        for every owner after the first (upstream convention)."""
        owners = {}
        for s in self.servers.values():
            if not s.alive:
                continue
            for tool in s.tools:
                owners.setdefault(tool["name"], []).append(s.id)
        merged = []
        seen_first = set()
        for s in self.servers.values():
            if not s.alive:
                continue
            for tool in s.tools:
                entry = dict(tool)
                entry["server"] = s.id
                bare = tool["name"]
                if len(owners.get(bare, [])) > 1:
                    if bare in seen_first:
                        entry["name"] = f"{s.id}/{bare}"
                    else:
                        seen_first.add(bare)
                merged.append(entry)
        return merged

    def route(self, name):
        """Resolve a (possibly prefixed) tool name to (server, bare name)."""
        for sep in ("/", "."):
            if sep in name:
                prefix, bare = name.split(sep, 1)
                server = self.servers.get(prefix)
                if server is not None and server.alive:
                    return server, bare
        for s in self.servers.values():
            if s.alive and any(t["name"] == name for t in s.tools):
                return s, name
        raise RuntimeError(f"unknown tool: {name}")

    async def call_tool(self, name, arguments, timeout=120):
        server, bare = self.route(name)
        return await server.call_tool(bare, arguments, timeout=timeout)

    async def refresh_all(self):
        async def _refresh(s):
            try:
                await s.refresh_tools()
            except Exception:
                pass
        await asyncio.gather(*(_refresh(s) for s in self.servers.values() if s.alive))

    async def restart_all(self):
        async def _restart(s):
            try:
                await s.restart()
                s.last_error = None
            except Exception as error:
                s.last_error = str(error)
        await asyncio.gather(*(_restart(s) for s in self.servers.values()))

    def persist(self):
        cfg = read_config()
        cfg["mcpServers"] = {sid: s.entry for sid, s in self.servers.items()}
        write_config(cfg)


REGISTRY = Registry()


# ---------------------------------------------------------------------------
# Extension-facing WebSocket protocol.
# ---------------------------------------------------------------------------

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
        await REGISTRY.refresh_all()
        await ws.send(json.dumps({
            "type": "tools", "id": mid, "tools": REGISTRY.catalog(),
            "servers": REGISTRY.servers_payload(), "mcp_alive": REGISTRY.any_alive,
        }))
    elif mtype == "call_tool":
        timeout = (msg.get("timeout") or 120000) / 1000 + 10
        try:
            text = await REGISTRY.call_tool(msg.get("name", ""), msg.get("arguments"), timeout=timeout)
            await ws.send(json.dumps({
                "type": "tool_result", "id": mid, "ok": True, "text": text, "images": [],
            }))
        except Exception as error:
            await ws.send(json.dumps({
                "type": "tool_result", "id": mid, "ok": False,
                "kind": "mcp_error", "error": str(error),
            }))
    elif mtype == "restart_mcp":
        await REGISTRY.restart_all()
        await ws.send(json.dumps({
            "type": "mcp_status", "id": mid, "ok": True, "alive": REGISTRY.any_alive,
            "tools": REGISTRY.catalog(), "servers": REGISTRY.servers_payload(),
        }))
    elif mtype == "add_server":
        server_id = msg.get("server_id") or ""
        if msg.get("url"):
            entry = {"url": msg["url"]}
        else:
            entry = {"command": msg.get("command"), "args": msg.get("args") or [],
                     "env": msg.get("env") or {}}
        if not server_id or (not entry.get("url") and not entry.get("command")):
            await ws.send(json.dumps({
                "type": "server_changed", "id": mid, "ok": False, "restarting": False,
                "error": "add_server needs a server_id and a command or url",
            }))
            return
        old = REGISTRY.servers.get(server_id)
        if old is not None:
            await old.stop()
        server = make_server(server_id, entry)
        REGISTRY.servers[server_id] = server
        await REGISTRY.start_server(server)
        if server.alive:
            REGISTRY.persist()
            await ws.send(json.dumps({
                "type": "server_changed", "id": mid, "ok": True, "restarting": False,
            }))
        else:
            await ws.send(json.dumps({
                "type": "server_changed", "id": mid, "ok": False, "restarting": False,
                "error": server.last_error or "server failed to start",
            }))
    elif mtype == "remove_server":
        server_id = msg.get("server_id") or ""
        server = REGISTRY.servers.pop(server_id, None)
        if server is None:
            await ws.send(json.dumps({
                "type": "server_changed", "id": mid, "ok": False, "restarting": False,
                "error": f"no such server: {server_id}",
            }))
            return
        await server.stop()
        REGISTRY.persist()
        await ws.send(json.dumps({
            "type": "server_changed", "id": mid, "ok": True, "restarting": False,
        }))
    else:
        await ws.send(json.dumps({
            "type": "error", "id": mid, "error": f"unknown message type: {mtype}",
        }))


async def handle_connection(ws):
    print(f"[bridge] extension connected from {ws.remote_address}", flush=True)
    await REGISTRY.ensure_started()
    await ws.send(json.dumps({
        "type": "connected", "mcp_alive": REGISTRY.any_alive,
        "tools": REGISTRY.catalog(), "servers": REGISTRY.servers_payload(),
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
    await REGISTRY.start_all()
    if not REGISTRY.servers:
        print("[bridge] no MCP servers configured yet (config.json); "
              "the bridge will still answer the extension.", flush=True)
    async with websockets.serve(handle_connection, HOST, PORT):
        print(f"[bridge] listening on ws://{HOST}:{PORT} - keep this terminal open", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
