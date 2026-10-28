#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-contained smoke tests for bridge.py on Linux.

Scenario 1 (regression): one stdio server (upstream vscode_mcp.py) behind
the bridge - greeting, ping, list_tools and real tool calls verified on
disk in a throwaway workspace.

Scenario 2 (multi-server): the same VS Code server plus two fake JetBrains
MCP servers - one Streamable HTTP, one legacy SSE - served simultaneously.
Verifies the merged catalogue, the "server/name" prefixing on collision,
bare-name and prefixed routing (including an IDE-style run-configuration
tool), and runtime add_server / remove_server with config persistence.

Run with a Python that has `websockets` installed, e.g.:
    python3 -m venv .venv && .venv/bin/pip install websockets
    .venv/bin/python test_linux_bridge.py
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent

FAILURES = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({extra})" if extra else ""))
    if not cond:
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# Fake JetBrains MCP server (Streamable HTTP or legacy SSE).
# ---------------------------------------------------------------------------

JB_TOOLS = [
    {"name": "execute_run_configuration",
     "description": "Execute a run configuration in the IDE",
     "inputSchema": {"type": "object", "properties": {"configurationName": {"type": "string"}}}},
    {"name": "get_file_problems",
     "description": "Get problems for a file from the IDE inspection engine",
     "inputSchema": {"type": "object", "properties": {"filePath": {"type": "string"}}}},
    {"name": "list_dir",
     "description": "Fake JetBrains list_dir (collides with the VS Code one on purpose)",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
]


class FakeJetBrains:
    def __init__(self, mode, marker):
        self.mode = mode            # "stream" | "sse"
        self.marker = marker
        self.sse_writers = {}

    def dispatch(self, msg):
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": f"fake-jetbrains-{self.mode}", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": JB_TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "execute_run_configuration":
                text = (f"Run configuration '{args.get('configurationName', 'app')}' "
                        f"finished: BUILD SUCCESSFUL [{self.marker}]")
            elif name == "get_file_problems":
                text = f"No problems found in {args.get('filePath', '?')} [{self.marker}]"
            elif name == "list_dir":
                text = f"MOCK-JETBRAINS list_dir [{self.marker}]"
            else:
                text = f"unknown fake tool {name}"
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}

    async def start(self, port):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        return self._server

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            writer.close()
            return
        lines = head.decode("iso-8859-1").split("\r\n")
        method, path = lines[0].split(" ", 2)[0], lines[0].split(" ", 2)[1]
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.lower()] = v
        if method == "GET" and path.startswith("/sse"):
            session = "sess-" + self.mode
            self.sse_writers[session] = writer
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\n\r\n")
            writer.write(f"event: endpoint\ndata: /messages/?session_id={session}\n\n".encode())
            await writer.drain()
            return  # keep the stream open; writer stays in the dict
        if method == "POST":
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                msg = {}
            is_notification = "id" not in msg
            if self.mode == "sse":
                session = path.split("session_id=")[-1]
                if not is_notification:
                    response = self.dispatch(msg)
                    stream = self.sse_writers.get(session)
                    if stream is not None:
                        stream.write(f"event: message\ndata: {json.dumps(response)}\n\n".encode())
                        await stream.drain()
                writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
            else:
                if is_notification:
                    writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
                else:
                    response = self.dispatch(msg)
                    payload = json.dumps(response).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Mcp-Session-Id: sess-stream\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()


# ---------------------------------------------------------------------------
# Bridge process harness + extension-side client.
# ---------------------------------------------------------------------------

def wait_for_port(proc, port, seconds=25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bridge exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("bridge did not start listening in time")


def start_bridge(config_path, port):
    env = dict(os.environ, SNAPGENT_CONFIG=str(config_path), ZS_BRIDGE_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "bridge.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_for_port(proc, port)
    return proc


def stop_bridge(proc):
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out


class ExtClient:
    def __init__(self, port):
        self.port = port
        self._next = 0
        self.ws = None
        self.greeting = None

    async def __aenter__(self):
        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.port}")
        self.greeting = json.loads(await asyncio.wait_for(self.ws.recv(), 15))
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def send(self, obj, timeout=20):
        self._next += 1
        await self.ws.send(json.dumps({**obj, "id": self._next}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if msg.get("id") == self._next:
                return msg


def make_fake_vscode(base, workspace):
    storage = base / "fakeconfig" / "Code" / "User" / "globalStorage"
    storage.mkdir(parents=True)
    (storage / "storage.json").write_text(json.dumps({
        "windowsState": {"lastActiveWindow": {"folder": workspace.as_uri()},
                         "openedWindows": []}}))
    return {"command": sys.executable, "args": [str(HERE / "vscode_mcp.py")],
            "env": {"APPDATA": str(base / "fakeconfig")}}


# ---------------------------------------------------------------------------
# Scenario 1: single stdio server (regression).
# ---------------------------------------------------------------------------

async def scenario1(tmp):
    workspace = tmp / "ws1"
    workspace.mkdir()
    config = {"mcpServers": {"vscode": make_fake_vscode(tmp / "s1", workspace)}}
    config_path = tmp / "s1-config.json"
    config_path.write_text(json.dumps(config))
    proc = start_bridge(config_path, 17614)
    try:
        async with ExtClient(17614) as ext:
            g = ext.greeting
            check("S1 greeting type=connected", g.get("type") == "connected")
            check("S1 greeting mcp_alive", g.get("mcp_alive") is True)
            check("S1 greeting carries 24 tools", len(g.get("tools") or []) == 24,
                  f"got {len(g.get('tools') or [])}")
            check("S1 tools tagged server=vscode",
                  all(t.get("server") == "vscode" for t in g.get("tools") or []))
            pong = await ext.send({"type": "ping"})
            check("S1 ping -> pong", pong.get("type") == "pong")
            r = await ext.send({"type": "call_tool", "name": "create_file",
                                "arguments": {"path": "hello.py",
                                              "content": "print('halo dari snapgent linux')\n"}})
            check("S1 create_file ok", r.get("ok") is True)
            on_disk = workspace / "hello.py"
            check("S1 file on disk", on_disk.is_file() and "snapgent linux" in on_disk.read_text())
            r = await ext.send({"type": "call_tool", "name": "run_command",
                                "arguments": {"command": "python3", "args": ["hello.py"]}})
            check("S1 run_command executes", r.get("ok") is True
                  and "halo dari snapgent linux" in (r.get("text") or ""))
    finally:
        out = stop_bridge(proc)
    print("--- bridge S1 output ---")
    print(out.strip())


# ---------------------------------------------------------------------------
# Scenario 2: vscode (stdio) + JetBrains stream + JetBrains SSE.
# ---------------------------------------------------------------------------

async def scenario2(tmp):
    workspace = tmp / "ws2"
    workspace.mkdir()
    (workspace / "penanda-vscode.txt").write_text("milik workspace vscode\n")
    workspace_extra = tmp / "ws2-extra"
    workspace_extra.mkdir()

    stream_mock = FakeJetBrains("stream", "STREAM")
    sse_mock = FakeJetBrains("sse", "SSE")
    servers = await asyncio.gather(stream_mock.start(18711), sse_mock.start(18712))

    config = {"mcpServers": {
        "vscode": make_fake_vscode(tmp / "s2", workspace),
        "android-studio": {"url": "http://127.0.0.1:18711/stream"},
        "android-sse": {"url": "http://127.0.0.1:18712/sse"},
    }}
    config_path = tmp / "s2-config.json"
    config_path.write_text(json.dumps(config))
    # The fake servers live in this event loop, so the blocking port-wait
    # must run in a thread or the mocks would starve during bridge startup.
    proc = await asyncio.to_thread(start_bridge, config_path, 17615)
    try:
        async with ExtClient(17615) as ext:
            g = ext.greeting
            payload = {s["id"]: s for s in g.get("servers") or []}
            check("S2 three servers in payload", set(payload) == {"vscode", "android-studio", "android-sse"},
                  f"got {sorted(payload)}")
            check("S2 all servers alive", all(s.get("alive") for s in payload.values()))
            check("S2 vscode reports 24 tools", payload.get("vscode", {}).get("tools") == 24)
            check("S2 jetbrains servers report 3 tools each",
                  payload.get("android-studio", {}).get("tools") == 3
                  and payload.get("android-sse", {}).get("tools") == 3)
            names = [t["name"] for t in g.get("tools") or []]
            check("S2 merged catalogue has 30 tools", len(names) == 30, f"got {len(names)}")
            check("S2 collision advertised prefixed",
                  "list_dir" in names and "android-studio/list_dir" in names
                  and "android-sse/list_dir" in names)

            r = await ext.send({"type": "call_tool", "name": "list_dir",
                                "arguments": {"path": "."}})
            check("S2 bare list_dir routes to vscode",
                  r.get("ok") is True and "penanda-vscode.txt" in (r.get("text") or ""),
                  (r.get("text") or r.get("error") or "")[:100])
            r = await ext.send({"type": "call_tool", "name": "android-studio/list_dir",
                                "arguments": {"path": "."}})
            check("S2 prefixed list_dir routes to stream mock",
                  r.get("ok") is True and "MOCK-JETBRAINS" in (r.get("text") or "")
                  and "STREAM" in (r.get("text") or ""))
            r = await ext.send({"type": "call_tool", "name": "execute_run_configuration",
                                "arguments": {"configurationName": "app"}})
            check("S2 bare run-configuration routes to jetbrains (run action)",
                  r.get("ok") is True and "BUILD SUCCESSFUL" in (r.get("text") or ""),
                  (r.get("text") or "")[:100])
            r = await ext.send({"type": "call_tool", "name": "android-sse/get_file_problems",
                                "arguments": {"filePath": "Main.kt"}})
            check("S2 prefixed get_file_problems routes to SSE mock",
                  r.get("ok") is True and "SSE" in (r.get("text") or "")
                  and "Main.kt" in (r.get("text") or ""))

            entry = make_fake_vscode(tmp / "s2x", workspace_extra)
            r = await ext.send({"type": "add_server", "server_id": "extra",
                                "command": entry["command"], "args": entry["args"],
                                "env": entry["env"]})
            check("S2 add_server ok", r.get("type") == "server_changed" and r.get("ok") is True,
                  str(r.get("error", "")))
            lt = await ext.send({"type": "list_tools"})
            names2 = [t["name"] for t in lt.get("tools") or []]
            check("S2 catalogue grows to 54 after add_server", len(names2) == 54,
                  f"got {len(names2)}")
            check("S2 added server tools advertised prefixed",
                  "extra/create_file" in names2)
            saved = json.loads(config_path.read_text())
            check("S2 add_server persisted to config.json", "extra" in saved.get("mcpServers", {}))
            r = await ext.send({"type": "call_tool", "name": "extra/create_file",
                                "arguments": {"path": "dari-extra.txt", "content": "halo\n"}})
            check("S2 call via added server works", r.get("ok") is True
                  and (workspace_extra / "dari-extra.txt").is_file())

            r = await ext.send({"type": "remove_server", "server_id": "extra"})
            check("S2 remove_server ok", r.get("type") == "server_changed" and r.get("ok") is True)
            lt = await ext.send({"type": "list_tools"})
            check("S2 catalogue back to 30 after remove_server",
                  len(lt.get("tools") or []) == 30)
            saved = json.loads(config_path.read_text())
            check("S2 remove_server persisted", "extra" not in saved.get("mcpServers", {}))

            r = await ext.send({"type": "restart_mcp"})
            alive = {s["id"]: s["alive"] for s in r.get("servers") or []}
            check("S2 restart_mcp ok, all alive again",
                  r.get("type") == "mcp_status" and r.get("ok") is True and all(alive.values()),
                  str(alive))
    finally:
        out = await asyncio.to_thread(stop_bridge, proc)
        for s in servers:
            s.close()
    print("--- bridge S2 output ---")
    print(out.strip())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="snapgent-test2-"))
    asyncio.run(scenario1(tmp))
    print()
    asyncio.run(scenario2(tmp))
    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURES: {FAILURES}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
