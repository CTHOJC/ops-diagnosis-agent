#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: graph definition + runner.

build_graph() returns a real LangGraph StateGraph when `langgraph` is
installed, and the same node order / conditional edges via a small
sequential runner otherwise (same pattern as the RAG project's
generate.py). The runner is the source of truth for Day 1 verification:
every node boundary saves the state through the file checkpointer, so an
interrupt at human_approval survives a process restart.

Edges (plan Ch.4):
  classify -> collect_evidence -> diagnose
  diagnose -> collect_evidence (low confidence, < 2 rounds) | make_plan
  make_plan -> human_approval (high risk) | execute (low risk)
  human_approval -> execute (approved) | report (rejected)
  execute -> verify
  verify -> diagnose (not recovered, < 1 retry) | report
"""
from __future__ import annotations

import time

from . import nodes, store
from .state import MAX_EVIDENCE_ROUNDS, MAX_VERIFY_RETRIES, AgentState


def _run_node(state, name):
    t0 = time.time()
    fn = nodes.NODE_FUNCS[name]
    fn(state)
    ms = round((time.time() - t0) * 1000, 2)
    state.trace.append({"node": name, "ms": ms,
                        "note": f"status={state.status}"})
    store.save(state)  # checkpointer: persist at every node boundary
    return state


def _route_after_diagnose(state):
    if nodes.needs_more_evidence(state):
        return "collect_evidence"
    return "make_plan"


def _route_after_plan(state):
    return "human_approval" if state.risk_level == "high" else "execute"


def _route_after_approval(state):
    return "execute" if state.approval.get("approved") else "report"


def _route_after_verify(state):
    if not nodes.is_recovered(state) and state.verify_retries < MAX_VERIFY_RETRIES:
        state.verify_retries += 1
        return "diagnose"
    return "report"


def run_from(state: AgentState, start: str = "classify") -> AgentState:
    """Run the graph from `start` until done / closed / awaiting approval."""
    node = start
    guard = 0
    while node and guard < 50:
        guard += 1
        state = _run_node(state, node)
        if node == "classify":
            node = "collect_evidence"
        elif node == "collect_evidence":
            node = "diagnose"
        elif node == "diagnose":
            node = _route_after_diagnose(state)
        elif node == "make_plan":
            node = _route_after_plan(state)
        elif node == "human_approval":
            if state.status == "awaiting_approval":
                return state  # interrupt: state already persisted
            node = _route_after_approval(state)
        elif node == "execute":
            if state.status == "closed":
                node = "report"
            else:
                node = "verify"
        elif node == "verify":
            node = _route_after_verify(state)
        elif node == "report":
            return state
    return state


def build_graph():
    """Return a LangGraph StateGraph when available, else the local runner.

    The LangGraph branch wires the same nodes and conditional edges; it is
    the Day 1 skeleton's forward-compatible path (real LangGraph wiring
    lands with the RAG Week 2 Wednesday work). Returns None when langgraph
    is not installed — callers then use run_from().
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        return None
    graph = StateGraph(AgentState)
    for name in nodes.NODES:
        graph.add_node(name, nodes.NODE_FUNCS[name])
    graph.set_entry_point("classify")
    graph.add_edge("classify", "collect_evidence")
    graph.add_edge("collect_evidence", "diagnose")
    graph.add_conditional_edges("diagnose", _route_after_diagnose)
    graph.add_conditional_edges("make_plan", _route_after_plan)
    # NOTE (Day 1): the interrupt at human_approval needs a Checkpointer
    # passed to compile() (plan Ch.4); the sequential runner in run_from()
    # is the verified path until the real LangGraph wiring lands.
    graph.add_conditional_edges("human_approval", _route_after_approval)
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges("verify", _route_after_verify)
    graph.add_edge("report", END)
    return graph.compile()


def resume(state: AgentState) -> AgentState:
    """Continue a persisted incident from where it stopped.

    From human_approval when approval was recorded; from report when the
    run was rejected; otherwise from the start.
    """
    if state.status == "awaiting_approval" and state.approval:
        state.status = "approved"
        return run_from(state, start="human_approval")
    if state.status == "awaiting_approval":
        return state
    if state.status == "closed":
        return run_from(state, start="report")
    return run_from(state, start="classify")
