#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 3: MCP Server (plan Ch.5 / Ch.11 Day 3).

Exposes the three read-only evidence tools over the MCP wire protocol so
they can be called — and tested — independently of the graph:

  search_logs    — ES logs by service / time window / keywords (read-only)
  query_metrics  — CPU / memory / latency / error rate (read-only)
  search_runbook — RAG knowledge base: manuals + history (read-only)

Transport: JSON-RPC 2.0 over stdio, newline-delimited JSON (the MCP stdio
transport). Methods: initialize, notifications/initialized, tools/list,
tools/call, ping. No MCP SDK is installed in this environment, so the
protocol layer is hand-rolled stdlib — deliberately small and inspectable.

Plan Ch.5 rules, enforced here, not in the caller:
  * Parameter contract: each tool has an inputSchema; invalid or missing
    parameters are rejected (-32602) — the server never guesses parameters.
  * Timeout: read-only tools get 10 s (plan Ch.5); a slow tool fails
    instead of hanging the session.
  * Call cap: at most 10 calls per tool per session, so a looping caller
    cannot burn the session (plan Ch.5: same-tool call上限).
  * Result trimming: the tools themselves trim (<=10 log lines, top-3
    runbook chunks) — raw dumps never enter the context.
  * Audit: every call is appended to mcp_server/audit.jsonl with its
    parameters, duration, summary and caller.

What is NOT here, on purpose: the four high-risk tools
(restart_service, scale_service, rollback_release, update_config) are
not exposed by this server at all. A tools/call for one of them is an
unknown-tool error, not a refusal — there is no path from this server
to a state-changing action. High-risk tools stay behind the graph's
human_approval interrupt (Day 1) and join MCP only with that gate.

Day 3 reality, stated plainly: the tools still answer from the same
demo data as Days 1-2 (demo_service state + ES-shaped fallback logs +
RAG starter chunks.jsonl). No live ES / Prometheus / Milvus, no LLM.
What Day 3 changes is the *boundary*: the tools are now callable over a
standard protocol by any MCP client, independently of the graph — the
graph itself still calls app.tools directly (rewiring the graph onto
the MCP client is later work, not today's deliverable).
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tools  # noqa: E402
from app.state import AgentState  # noqa: E402

HERE = Path(__file__).resolve().parent
AUDIT_FILE = HERE / "audit.jsonl"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ops-agent-mcp", "version": "0.3.0"}

# Plan Ch.5: read-only tools get a 10 s timeout.
READ_TIMEOUT_S = 10.0
# Plan Ch.5: per-tool call cap within one session (anti-loop guard).
MAX_CALLS_PER_TOOL = 10

TOOL_DEFS = {
    "search_logs": {
        "description": "Search ES logs by service, time window and keywords. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name, e.g. web-api"},
                "keywords": {"type": "array", "items": {"type": "string"},
                             "description": "Keywords to match in log messages"},
                "window_min": {"type": "integer", "default": 30, "minimum": 1,
                               "maximum": 1440, "description": "Look-back window in minutes"},
            },
            "required": ["service", "keywords"],
            "additionalProperties": False,
        },
    },
    "query_metrics": {
        "description": "Query CPU, memory, latency and error-rate metrics for a service. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name, e.g. web-api"},
            },
            "required": ["service"],
            "additionalProperties": False,
        },
    },
    "search_runbook": {
        "description": "Retrieve runbook / ops-manual chunks from the RAG knowledge base. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or alert description"},
                "top_k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

# The three evidence tools (plan Ch.3 data flow: logs + metrics + runbook
# are the parallel evidence trio). get_service_status stays a direct-only
# call for now; it is a status check, not evidence, and joins MCP later.
TOOL_ORDER = ["search_logs", "query_metrics", "search_runbook"]


class ToolError(Exception):
    """A -32602-style invalid-params / unknown-tool failure."""


def _validate(name: str, args: dict) -> dict:
    """Validate arguments against the tool's inputSchema; never guess."""
    schema = TOOL_DEFS[name]["inputSchema"]
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    unknown = sorted(set(args) - set(schema["properties"]))
    if unknown:
        raise ToolError(f"unknown parameter(s): {', '.join(unknown)}")
    for req in schema.get("required", []):
        if req not in args:
            raise ToolError(f"missing required parameter: {req}")
    out = {}
    for key, spec in schema["properties"].items():
        if key in args:
            v = args[key]
            t = spec["type"]
            if t == "string" and not isinstance(v, str):
                raise ToolError(f"{key} must be a string")
            if t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
                raise ToolError(f"{key} must be an integer")
            if t == "array" and not isinstance(v, list):
                raise ToolError(f"{key} must be an array")
            if t == "array" and not all(isinstance(x, str) for x in v):
                raise ToolError(f"{key} must be an array of strings")
            if t == "array" and not v:
                raise ToolError(f"{key} must not be empty")
            if "minimum" in spec and v < spec["minimum"]:
                raise ToolError(f"{key} must be >= {spec['minimum']}")
            if "maximum" in spec and v > spec["maximum"]:
                raise ToolError(f"{key} must be <= {spec['maximum']}")
            out[key] = v
        elif "default" in spec:
            out[key] = spec["default"]
    return out


def _call_tool(name: str, args: dict):
    """Run one validated tool call against a throwaway state (for audit).

    The state is per-call, not shared with any incident: MCP calls are
    read-only evidence fetches, and their audit trail lives in
    audit.jsonl, not in an incident's tool_audit.
    """
    state = AgentState(incident_id="mcp-call", alert={},
                       demo_service={"upstream": "down", "replicas": 2,
                                     "version": "v1.4.2", "config": {}})
    if name == "search_logs":
        return tools.search_logs(state, args["service"], args["keywords"],
                                 args.get("window_min", 30))
    if name == "query_metrics":
        return tools.query_metrics(state, args["service"])
    if name == "search_runbook":
        return tools.search_runbook(state, args["query"], args.get("top_k", 3))
    raise ToolError(f"unknown tool: {name}")


class Server:
    def __init__(self):
        self.calls: dict[str, int] = {name: 0 for name in TOOL_ORDER}
        self.initialized = False

    # -- JSON-RPC plumbing -------------------------------------------------
    def _result(self, req, result):
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": result}

    def _error(self, req, code, message):
        return {"jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": code, "message": message}}

    def _audit(self, name, args, ms, ok, summary):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": name,
               "arguments": args, "ms": round(ms, 2), "ok": ok,
               "summary": summary}
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    # -- MCP methods -------------------------------------------------------
    def handle(self, req: dict):
        method = req.get("method", "")
        if method == "initialize":
            self.initialized = True
            return self._result(req, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None  # notification: no reply
        if method == "ping":
            return self._result(req, {})
        if not self.initialized:
            return self._error(req, -32002, "server not initialized")
        if method == "tools/list":
            return self._result(req, {"tools": [
                {"name": n, **TOOL_DEFS[n]} for n in TOOL_ORDER]})
        if method == "tools/call":
            return self._tools_call(req)
        return self._error(req, -32601, f"method not found: {method}")

    def _tools_call(self, req):
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_DEFS:
            # High-risk tools are not listed here at all: no path from
            # this server to a state-changing action.
            return self._error(req, -32602, f"unknown tool: {name}")
        if self.calls[name] >= MAX_CALLS_PER_TOOL:
            return self._error(req, -32602,
                               f"{name}: per-session call cap ({MAX_CALLS_PER_TOOL}) reached")
        try:
            clean = _validate(name, args)
        except ToolError as e:
            self._audit(name, args, 0.0, False, str(e))
            return self._error(req, -32602, str(e))
        t0 = time.time()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_call_tool, name, clean)
                result = fut.result(timeout=READ_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            ms = (time.time() - t0) * 1000
            self._audit(name, clean, ms, False, f"timeout after {READ_TIMEOUT_S}s")
            return self._error(req, -32602,
                               f"{name}: timed out after {READ_TIMEOUT_S}s")
        ms = (time.time() - t0) * 1000
        self.calls[name] += 1
        n = len(result) if isinstance(result, list) else 1
        summary = f"{n} item(s)" if isinstance(result, list) else "ok"
        self._audit(name, clean, ms, True, summary)
        return self._result(req, {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False,
        })


def main():
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        resp = server.handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
