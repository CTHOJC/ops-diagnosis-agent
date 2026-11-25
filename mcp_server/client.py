#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 3: a minimal MCP client for the MCP Server.

Spawns mcp_server/server.py as a subprocess over stdio and speaks the
same newline-delimited JSON-RPC. This is the "independent script" the
sprint table asks for on Day 3: the tools are verified without the graph.

Usage (from the ops-agent/ directory):
  python3 -m mcp_server.client list
      List the tools the server exposes (expect exactly the 3 read-only
      evidence tools — no high-risk tool).
  python3 -m mcp_server.client call --tool search_logs \
      --args '{"service": "web-api", "keywords": ["upstream", "502"]}'
  python3 -m mcp_server.client smoke
      Full check: list + one call per tool + an invalid-params call that
      must be rejected + a high-risk tool call that must be unknown.
      Exit 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server"],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)
        self._next_id = 1

    def _send(self, method, params=None):
        req = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        self._next_id += 1
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed the connection")
        return json.loads(line)

    def _notify(self, method, params=None):
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def initialize(self):
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ops-agent-mcp-client", "version": "0.3.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self._notify("notifications/initialized")
        return resp["result"]

    def list_tools(self):
        resp = self._send("tools/list")
        if "error" in resp:
            raise RuntimeError(f"tools/list failed: {resp['error']}")
        return resp["result"]["tools"]

    def call(self, name, args):
        resp = self._send("tools/call", {"name": name, "arguments": args})
        return resp

    def close(self):
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        self.proc.wait(timeout=5)


def _show_call(resp):
    if "error" in resp:
        print(f"ERROR {resp['error']['code']}: {resp['error']['message']}")
        return None
    for block in resp["result"]["content"]:
        print(block["text"])
    return resp["result"]


def cmd_list(_args):
    c = Client()
    try:
        c.initialize()
        for t in c.list_tools():
            print(f"- {t['name']}: {t['description']}")
    finally:
        c.close()
    return 0


def cmd_call(args):
    c = Client()
    try:
        c.initialize()
        resp = c.call(args.tool, json.loads(args.args))
        _show_call(resp)
        return 0 if "error" not in resp else 1
    finally:
        c.close()


def cmd_smoke(_args):
    """list + one call per tool + two must-fail calls; exit 0 iff all pass."""
    checks = []
    c = Client()
    try:
        c.initialize()
        tools = c.list_tools()
        names = [t["name"] for t in tools]
        checks.append(("tools/list exposes exactly the 3 read-only tools",
                       names == ["search_logs", "query_metrics", "search_runbook"]))
        checks.append(("no high-risk tool exposed",
                       not any(n in names for n in
                               ("restart_service", "scale_service",
                                "rollback_release", "update_config"))))
        r = c.call("search_logs", {"service": "web-api",
                                   "keywords": ["upstream", "502"]})
        hits = json.loads(r["result"]["content"][0]["text"]) if "error" not in r else []
        checks.append(("search_logs finds the 502 upstream lines",
                       len(hits) >= 2 and "upstream" in hits[0]["msg"].lower()))
        r = c.call("query_metrics", {"service": "web-api"})
        m = json.loads(r["result"]["content"][0]["text"]) if "error" not in r else {}
        checks.append(("query_metrics returns error_rate_5m",
                       isinstance(m.get("error_rate_5m"), (int, float))))
        r = c.call("search_runbook", {"query": "Nginx 返回 502 先检查哪三项"})
        chunks = json.loads(r["result"]["content"][0]["text"]) if "error" not in r else []
        checks.append(("search_runbook returns runbook chunks with sources",
                       len(chunks) >= 1 and "chunk_id" in chunks[0]))
        r = c.call("search_logs", {"service": "web-api"})  # missing keywords
        checks.append(("missing required param is rejected (-32602)",
                       r.get("error", {}).get("code") == -32602))
        r = c.call("restart_service", {"service": "web-api"})
        checks.append(("high-risk tool call is unknown (-32602)",
                       r.get("error", {}).get("code") == -32602))
    finally:
        c.close()
    ok = True
    for label, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {label}")
        ok = ok and passed
    print("smoke: " + ("all passed" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mcp_server.client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("call")
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}")
    p.set_defaults(fn=cmd_call)
    p = sub.add_parser("smoke"); p.set_defaults(fn=cmd_smoke)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
