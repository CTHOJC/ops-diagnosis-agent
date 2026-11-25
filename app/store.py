#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: file checkpointer + incident store.

Plan Ch.4 wants a Checkpointer so a restart resumes from human_approval;
Day 1 implements it as JSON files (PostgreSQL lands later in the sprint).
Layout: data/incidents/<incident_id>/state.json (+ report.md when done).
Every node boundary saves the state, so an interrupt never loses work.
"""
from __future__ import annotations

import json
from pathlib import Path

from .state import AgentState

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "incidents"


def incident_dir(incident_id: str) -> Path:
    d = DATA_DIR / incident_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(state: AgentState) -> Path:
    p = incident_dir(state.incident_id) / "state.json"
    p.write_text(state.to_json(), encoding="utf-8")
    return p


def load(incident_id: str) -> AgentState:
    p = incident_dir(incident_id) / "state.json"
    return AgentState.from_dict(json.loads(p.read_text(encoding="utf-8")))


def exists(incident_id: str) -> bool:
    return (DATA_DIR / incident_id / "state.json").exists()


def save_report(state: AgentState, markdown: str) -> Path:
    p = incident_dir(state.incident_id) / "report.md"
    p.write_text(markdown, encoding="utf-8")
    return p
