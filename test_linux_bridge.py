#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-contained smoke test for bridge.py on Linux.

Builds a throwaway fake VS Code storage + demo workspace, starts the bridge
on a scratch port with a scratch config, then plays the role of the browser
extension over WebSocket: greeting, ping, list_tools, and real tool calls
(create/read/edit/run) whose effects are verified on disk.

Run with a Python that has `websockets` installed, e.g.:
    python3 -m venv .venv && .venv/bin/pip install websockets
    .venv/bin/python test_linux_bridge.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
PORT = 17614
WS = f"ws://127.0.0.1:{PORT}"
_next = 0


async def send(ws, obj, timeout=15):
    global _next
    _next += 1
    await ws.send(json.dumps({**obj, "id": _next}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg.get("id") == _next:
            return msg


async def run_checks(workspace):
    failures = []

    def check(name, cond, extra=""):
        print(("PASS " if cond else "FAIL ") + name + (f" ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    async with websockets.connect(WS) as ws:
        greeting = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("greeting type=connected", greeting.get("type") == "connected")
        check("greeting mcp_alive", greeting.get("mcp_alive") is True)
        tools = greeting.get("tools") or []
        check("greeting carries 24 tools", len(tools) == 24, f"got {len(tools)}")
        check("all tools tagged server=vscode",
              all(t.get("server") == "vscode" for t in tools))

        pong = await send(ws, {"type": "ping"})
        check("ping -> pong", pong.get("type") == "pong")

        lt = await send(ws, {"type": "list_tools"})
        check("list_tools -> 24 tools",
              lt.get("type") == "tools" and len(lt.get("tools") or []) == 24)

        r = await send(ws, {"type": "call_tool", "name": "create_file",
                            "arguments": {"path": "hello.py",
                                          "content": "print('halo dari snapgent linux')\n"}})
        check("call_tool create_file ok", r.get("type") == "tool_result" and r.get("ok") is True)
        on_disk = workspace / "hello.py"
        check("file exists on disk in workspace",
              on_disk.is_file() and "snapgent linux" in on_disk.read_text())

        r = await send(ws, {"type": "call_tool", "name": "run_command",
                            "arguments": {"command": "python3", "args": ["hello.py"]}})
        check("call_tool run_command executes the file",
              r.get("ok") is True and "halo dari snapgent linux" in (r.get("text") or ""))

        r = await send(ws, {"type": "call_tool", "name": "edit_file",
                            "arguments": {"path": "hello.py", "old_text": "halo",
                                          "new_text": "halo-halo"}})
        check("call_tool edit_file ok", r.get("ok") is True)
        check("edit landed on disk", "halo-halo" in on_disk.read_text())
    return failures


def wait_for_port(proc, seconds=20):
    import socket
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bridge exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("bridge did not start listening in time")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="snapgent-test-"))
    workspace = tmp / "demo-workspace"
    workspace.mkdir()
    storage = tmp / "fakeconfig" / "Code" / "User" / "globalStorage"
    storage.mkdir(parents=True)
    (storage / "storage.json").write_text(json.dumps({
        "windowsState": {
            "lastActiveWindow": {"folder": workspace.as_uri()},
            "openedWindows": [],
        }
    }))
    config = {
        "mcpServers": {"vscode": {
            "command": sys.executable,
            "args": [str(HERE / "vscode_mcp.py")],
            "env": {"APPDATA": str(tmp / "fakeconfig")},
        }}
    }
    config_path = tmp / "config.json"
    config_path.write_text(json.dumps(config))

    env = dict(os.environ, SNAPGENT_CONFIG=str(config_path), ZS_BRIDGE_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "bridge.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for_port(proc)
        failures = asyncio.run(run_checks(workspace))
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    print()
    print("bridge output:")
    print(out.strip())
    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
