#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 5: structured diagnosis output + LLM adapter.

Sprint Day 5 (plan Ch.6 module 4 / Ch.9 口径) wants the diagnose node to
emit machine-checkable structured output instead of free text, so that
Top 1 / Top 3 定位准确率 and evidence completeness can be scored on the
15-scenario eval (Days 11–12) instead of eyeballed.

Two parts, deliberately separable:

1. Diagnosis — the output contract. One dataclass + one JSON Schema,
   validated on every construction. `diagnose()` in nodes.py builds it
   from the same rule-scored hypotheses Days 1–4 already produced, so
   the shape lands now and the reasoning underneath can be swapped
   later without changing any downstream consumer (make_plan, report,
   the eval harness).

2. LLM adapter — a pluggable OpenAI-compatible client, wired into
   diagnose() behind diagnose_with_engine() with a loud recorded
   fallback: when OPS_DIAGNOSIS_ENGINE selects the LLM engine and the
   call fails for any reason (no key, timeout, HTTP error, bad JSON,
   schema violation), the failure is recorded on the state and the
   rule engine answers instead — never silently, and the stored
   Diagnosis always says which engine produced it (source/model).
   With no key configured the rule engine runs directly. When a key is
   available, diagnose_with_llm() builds the same Diagnosis from the
   model's JSON and validates it against the same rules — a model
   answer that cites non-existent evidence or returns an out-of-range
   confidence is rejected, never trusted.

Stdlib only, like the rest of the skeleton: urllib for HTTP, no SDK.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

# Plan Ch.4 / Ch.6 module 4: below this confidence the diagnosis must
# say it needs a human instead of guessing a root cause.
DIAGNOSE_CONFIDENCE_THRESHOLD = 0.5
UNKNOWN_CAUSE = "未知型故障，需转人工"
# Cause names the validator accepts. Duplicated from the rule tables in
# nodes.py on purpose: the contract owns its vocabulary, so a new cause
# added to the rules but not here fails loudly instead of slipping
# through as an unvalidated string.
KNOWN_CAUSES = frozenset({"上游实例宕机", "内存 OOM 被杀", "CPU 打满",
                          "磁盘写满", "数据库慢查询",
                          # Day 11-12: scenarios 6-13 (must match
                          # nodes._HYPOTHESES / _PLANS exactly).
                          "发布后错误率上升", "下游超时雪崩", "连接池耗尽",
                          "配置变更引发异常", "证书过期", "消息队列积压",
                          "缓存击穿", "单实例网络抖动"})
DIAGNOSIS_VERSION = "diagnosis/v1"

# The JSON Schema the LLM is asked to conform to (response_format).
# Kept as a plain dict so it serialises to JSON for any OpenAI-compatible
# /chat/completions call and can be reused by the eval harness.
DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Diagnosis",
    "type": "object",
    "required": ["category", "causes", "summary", "checks_performed",
                 "recommended_actions", "needs_human", "is_unknown",
                 "source", "model"],
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string",
                     "description": "告警分类，如 upstream_down / cpu_saturated"},
        "causes": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["cause", "confidence", "evidence_refs"],
                "additionalProperties": False,
                "properties": {
                    "cause": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0,
                                   "maximum": 1},
                    "evidence_refs": {"type": "array",
                                      "items": {"type": "string"}},
                },
            },
        },
        "summary": {"type": "string", "minLength": 1,
                    "description": "一句话推理摘要，必须能从证据推出"},
        "checks_performed": {"type": "array", "minItems": 1,
                             "items": {"type": "string"},
                             "description": "实际执行过的检查，一条一个"},
        "recommended_actions": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["action", "risk"],
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "high"]},
                    "rollback": {"type": "string",
                                 "description": "高危操作必填回滚预案"},
                },
            },
        },
        "is_unknown": {"type": "boolean",
                       "description": "证据不支持任何根因时为 true，必须转人工"},
        "needs_human": {"type": "boolean"},
        "source": {"type": "string", "enum": ["rules", "llm"]},
        "model": {"type": "string",
                  "description": "规则推理时为空字符串"},
    },
}


class DiagnosisError(ValueError):
    """A Diagnosis failed validation — never trust it, never guess."""


class LLMConfigError(RuntimeError):
    """No LLM key / endpoint configured. Loud on purpose: the caller
    must decide explicitly (provide a key, or stay on rules) instead of
    silently getting rule output dressed up as an LLM answer."""


class LLMError(RuntimeError):
    """The LLM endpoint answered badly (HTTP error, bad JSON, timeout)."""


@dataclass
class Cause:
    cause: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class Action:
    """One recommended action from the diagnosis. High-risk actions must
    carry a rollback plan — the same rule make_plan enforces."""
    action: str
    risk: str              # "low" | "high"
    rollback: str = ""


@dataclass
class Diagnosis:
    """The structured output of the diagnose node (diagnosis/v1).

    `causes` is sorted by confidence descending; `causes[0]` is the Top 1
    root cause and drives make_plan. Every cited ref must name an evidence
    item that was actually collected, and every cause with confidence
    above 0 must cite at least one — a diagnosis that claims a cause it
    saw no evidence for is invalid. Zero-confidence also-rans may cite
    nothing (they matched no keyword); the Top 1 cause always has
    evidence behind it when it clears the threshold. `source` records
    which reasoning produced it, so a report or resume line can never
    mistake rule output for LLM output. `checks_performed` lists what
    was actually checked (derived from the evidence, never invented);
    `recommended_actions` is what the reasoning proposes, each with a
    risk tag. `is_unknown` is True exactly when the evidence supports
    no root cause — then `causes[0]` is the unknown placeholder, no
    high-risk action is proposed, and the only next step is handing
    the evidence to a human.
    """
    category: str
    causes: list[Cause] = field(default_factory=list)
    summary: str = ""
    checks_performed: list[str] = field(default_factory=list)
    recommended_actions: list[Action] = field(default_factory=list)
    needs_human: bool = True
    is_unknown: bool = False
    source: str = "rules"            # "rules" | "llm"
    model: str = ""                  # "" for rules; model id for llm
    version: str = DIAGNOSIS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Diagnosis":
        d = dict(d)
        d["causes"] = [Cause(**c) for c in d.get("causes", [])]
        # Dicts written before checks/recommended_actions existed load
        # with empty lists; new outputs always carry them (validation
        # enforces it at creation time).
        d["checks_performed"] = list(d.get("checks_performed", []))
        d["recommended_actions"] = [Action(**a)
                                    for a in d.get("recommended_actions", [])]
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def validate_diagnosis(d: Diagnosis, known_refs: set[str],
                       threshold: float = DIAGNOSE_CONFIDENCE_THRESHOLD
                       ) -> Diagnosis:
    """Reject any Diagnosis that breaks the contract; return it unchanged
    when it holds. Checks, in order: non-empty causes, confidences in
    [0, 1] and sorted descending, every cited ref was actually collected,
    a non-empty summary, a declared source, and needs_human set whenever
    the Top 1 confidence is below threshold or the cause is unknown."""
    if not d.causes:
        raise DiagnosisError("diagnosis has no causes")
    confs = [c.confidence for c in d.causes]
    if any((not isinstance(c, (int, float))) or c < 0 or c > 1 for c in confs):
        raise DiagnosisError(f"confidence out of [0, 1]: {confs}")
    if confs != sorted(confs, reverse=True):
        raise DiagnosisError(f"causes not sorted by confidence desc: {confs}")
    for c in d.causes:
        unknown_refs = sorted(set(c.evidence_refs) - known_refs)
        if unknown_refs:
            raise DiagnosisError(
                f"cause {c.cause!r} cites evidence never collected: "
                f"{unknown_refs}")
        # A cause the evidence actually supports must say which evidence;
        # a zero-confidence also-ran matched no keyword, so it has none
        # to cite (the Top 1 cause always clears this when it is real).
        if not c.evidence_refs and c.confidence > 0 \
                and c.cause != UNKNOWN_CAUSE:
            raise DiagnosisError(
                f"cause {c.cause!r} cites no evidence")
    if not d.summary.strip():
        raise DiagnosisError("diagnosis summary is empty")
    if d.source not in ("rules", "llm"):
        raise DiagnosisError(f"unknown diagnosis source: {d.source!r}")
    if d.source == "rules" and d.model:
        raise DiagnosisError("rule-based diagnosis must not name a model")
    top = d.causes[0]
    if (top.confidence < threshold or top.cause == UNKNOWN_CAUSE) \
            and not d.needs_human:
        raise DiagnosisError(
            "low-confidence / unknown diagnosis must set needs_human")
    # Checks and recommended actions are part of the contract, not
    # decoration: a diagnosis that cannot say what it checked or what
    # it proposes is not usable by the report or the eval harness.
    if not d.checks_performed or any(
            not str(c).strip() for c in d.checks_performed):
        raise DiagnosisError(
            "checks_performed must list what was actually checked")
    if not d.recommended_actions:
        raise DiagnosisError("recommended_actions must not be empty")
    for a in d.recommended_actions:
        if not a.action.strip():
            raise DiagnosisError("recommended action text must not be empty")
        if a.risk not in ("low", "high"):
            raise DiagnosisError(f"action risk must be low/high: {a.risk!r}")
        if a.risk == "high" and not a.rollback.strip():
            raise DiagnosisError(
                f"high-risk action {a.action!r} has no rollback plan")
    # The unknown state must be explicit and consistent: is_unknown is
    # True exactly when the Top 1 cause is the unknown placeholder, and
    # a supported cause must never be hidden as unknown (nor an
    # unsupported one claimed as known).
    if d.is_unknown != (top.cause == UNKNOWN_CAUSE):
        raise DiagnosisError(
            "is_unknown must match whether the Top 1 cause is the "
            "unknown placeholder")
    if d.is_unknown:
        if top.confidence >= threshold:
            raise DiagnosisError(
                "unknown diagnosis hides a supported root cause "
                f"(top confidence {top.confidence})")
        if any(a.risk == "high" for a in d.recommended_actions):
            raise DiagnosisError(
                "unknown diagnosis must not propose high-risk actions")
        if not any("转人工" in a.action for a in d.recommended_actions):
            raise DiagnosisError(
                "unknown diagnosis must hand off to a human")
    else:
        if top.confidence < threshold:
            raise DiagnosisError(
                "known diagnosis claims an unsupported root cause "
                f"(top confidence {top.confidence} < {threshold})")
    if any(a.risk == "high" for a in d.recommended_actions) \
            and not d.needs_human:
        raise DiagnosisError(
            "diagnosis proposing high-risk actions must set needs_human")
    for c in d.causes:
        if c.cause not in KNOWN_CAUSES and c.cause != UNKNOWN_CAUSE:
            raise DiagnosisError(f"unknown cause name: {c.cause!r}")
    return d


def build_diagnosis(category: str, hypotheses, known_refs: set[str],
                    checks_performed: list[str],
                    recommended_actions: list[Action],
                    source: str = "rules", model: str = "",
                    threshold: float = DIAGNOSE_CONFIDENCE_THRESHOLD
                    ) -> Diagnosis:
    """Build a validated Diagnosis from already-scored hypotheses
    (the rule tables in nodes.diagnose, or an LLM's parsed causes).
    The summary is derived from the Top 1 cause only, so it can never
    claim more than the evidence supports. checks_performed and
    recommended_actions are required inputs, not defaults: inventing
    them here would hide what was actually checked and proposed."""
    causes = [Cause(cause=h.cause, confidence=round(float(h.confidence), 4),
                    evidence_refs=sorted(set(h.evidence_refs)))
              for h in hypotheses]
    top = causes[0] if causes else None
    is_unknown = top is None or top.cause == UNKNOWN_CAUSE
    if is_unknown:
        summary = "证据不足，无法确定根因，转人工处理"
        needs_human = True
    else:
        summary = (f"最可能的根因是{top.cause}（置信度 {top.confidence:.2f}），"
                   f"依据 {len(top.evidence_refs)} 条证据")
        needs_human = top.confidence < threshold or any(
            a.risk == "high" for a in recommended_actions)
    d = Diagnosis(category=category, causes=causes, summary=summary,
                  checks_performed=list(checks_performed),
                  recommended_actions=list(recommended_actions),
                  needs_human=needs_human, is_unknown=is_unknown,
                  source=source, model=model)
    return validate_diagnosis(d, known_refs, threshold)


# ---------------------------------------------------------------------------
# LLM adapter — OpenAI-compatible /chat/completions, stdlib only.
# Not wired into diagnose() until a key is confirmed (see module docstring).
# ---------------------------------------------------------------------------
class LLMAdapter:
    """Minimal interface the diagnose node will call: prompt in, parsed
    JSON dict out. Adapters must raise, never return a silent fallback."""

    model: str = ""

    def complete_json(self, prompt: str,
                      schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleAdapter(LLMAdapter):
    """POST {base_url}/chat/completions with response_format json_object.

    api_key defaults to the OPS_LLM_API_KEY environment variable
    (OPS_AGENT_LLM_API_KEY is accepted as an alias). A missing key
    raises LLMConfigError at construction — loudly, before any request
    is attempted — so a misconfigured run fails fast instead of
    producing rule output that looks like an LLM answer.
    """

    def __init__(self, api_key: str | None = None,
                 base_url: str | None = None,
                 model: str | None = None, timeout_s: float | None = None):
        # Key precedence: explicit arg, then OPS_LLM_API_KEY, then the
        # older OPS_AGENT_LLM_API_KEY alias. A missing key raises
        # LLMConfigError at construction — loudly, before any request is
        # attempted — so a misconfigured run fails fast instead of
        # producing rule output that looks like an LLM answer.
        key = api_key if api_key is not None else (
            os.environ.get("OPS_LLM_API_KEY")
            or os.environ.get("OPS_AGENT_LLM_API_KEY", ""))
        if not key:
            raise LLMConfigError(
                "no LLM API key: set OPS_LLM_API_KEY or pass "
                "api_key=... explicitly; refusing to pretend an LLM ran")
        self.api_key = key
        self.base_url = (base_url or os.environ.get("OPS_LLM_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("OPS_LLM_MODEL") or "gpt-4o-mini"
        if timeout_s is None:
            try:
                timeout_s = float(os.environ.get("OPS_LLM_TIMEOUT_S", "30"))
            except ValueError as e:
                raise LLMConfigError(
                    f"invalid OPS_LLM_TIMEOUT_S: {e}") from e
        if timeout_s <= 0:
            raise LLMConfigError("OPS_LLM_TIMEOUT_S must be > 0")
        self.timeout_s = timeout_s

    def complete_json(self, prompt: str,
                      schema: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system",
                 "content": "你是运维诊断助手。只输出 JSON，必须符合给定的 "
                            "JSON Schema；引用的 evidence_ref 只能用提供的证据编号。"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"LLM endpoint HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMError(f"LLM endpoint unreachable: {e}") from e
        try:
            text = payload["choices"][0]["message"]["content"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"LLM returned unparsable content: {e}") from e


def diagnose_with_llm(category: str, evidence, adapter: LLMAdapter,
                      known_refs: set[str],
                      threshold: float = DIAGNOSE_CONFIDENCE_THRESHOLD
                      ) -> Diagnosis:
    """Run diagnose through an LLM and validate the result the same way
    rule output is validated. Any failure (bad JSON, dangling refs,
    out-of-range confidence) raises DiagnosisError / LLMError — the
    caller decides whether to fall back to rules, explicitly."""
    lines = [f"告警分类：{category}", "已收集的证据（只能引用这些编号）："]
    for e in evidence:
        lines.append(f"- {e.ref} [{e.source}] {e.summary}")
    lines.append("只输出 JSON，字段按 Diagnosis schema：category（必须与上方"
                 "告警分类一致）/ causes（按 confidence 降序，每项 cause、"
                 "confidence、evidence_refs）/ summary / checks_performed"
                 "（实际用过的检查）/ recommended_actions（每项 action、"
                 "risk，高危必填 rollback）/ needs_human。")
    raw = adapter.complete_json("\n".join(lines), DIAGNOSIS_SCHEMA)
    try:
        causes = [Cause(cause=c["cause"],
                        confidence=float(c["confidence"]),
                        evidence_refs=list(c.get("evidence_refs", [])))
                  for c in raw["causes"]]
        checks = [str(c) for c in raw["checks_performed"]]
        actions = [Action(action=a["action"], risk=a["risk"],
                          rollback=a.get("rollback", ""))
                   for a in raw["recommended_actions"]]
        if raw["category"] != category:
            raise DiagnosisError(
                f"LLM category {raw['category']!r} != classified "
                f"category {category!r}: the model must not reclassify "
                f"the alert")
        d = Diagnosis(category=category, causes=causes,
                      summary=str(raw.get("summary", "")),
                      checks_performed=checks, recommended_actions=actions,
                      needs_human=bool(raw.get("needs_human", True)),
                      is_unknown=bool(causes)
                      and causes[0].cause == UNKNOWN_CAUSE,
                      source="llm", model=adapter.model)
    except (KeyError, TypeError, ValueError) as e:
        raise DiagnosisError(f"LLM diagnosis JSON missing fields: {e}") from e
    return validate_diagnosis(d, known_refs, threshold)


# ---------------------------------------------------------------------------
# Engine selection + fallback — the diagnose node's entry point.
# OPS_DIAGNOSIS_ENGINE: auto (default) | rule | llm.
#   auto: the LLM engine runs only when a key is configured
#         (OPS_LLM_API_KEY or OPS_AGENT_LLM_API_KEY); otherwise rules.
#   rule: always the rule engine, no LLM attempt.
#   llm:  always try the LLM engine; any failure is recorded and the
#         rule engine answers instead.
# On ANY LLM failure (no key, timeout, HTTP error, bad JSON, schema
# violation) the failure is recorded loudly in the returned failures
# list and the rule engine answers — the run never dies silently and
# never ships an unvalidated diagnosis. The returned Diagnosis is
# always validated against the evidence actually collected; a rule
# output that fails validation raises instead of being stored.
# ---------------------------------------------------------------------------
def diagnose_with_engine(state, *, hypotheses, checks_performed,
                         recommended_actions):
    """Run the selected reasoning engine for one diagnose call.

    Returns (Diagnosis, failures) where failures is a list of
    human-readable failure records (empty when the selected engine
    succeeded). `hypotheses` is the rule-scored ranking the fallback
    builds from; the LLM path scores its own ranking from the evidence.
    """
    choice = os.environ.get("OPS_DIAGNOSIS_ENGINE", "auto").strip().lower()
    failures: list[str] = []
    use_llm = False
    if choice == "llm":
        use_llm = True
    elif choice == "auto":
        use_llm = bool(os.environ.get("OPS_LLM_API_KEY")
                       or os.environ.get("OPS_AGENT_LLM_API_KEY"))
    elif choice == "rule":
        use_llm = False
    else:
        failures.append(
            f"invalid OPS_DIAGNOSIS_ENGINE={choice!r} "
            "(use auto, rule or llm); fell back to rule engine")
    if use_llm:
        try:
            adapter = OpenAICompatibleAdapter()
            return diagnose_with_llm(
                state.category, state.evidence, adapter,
                {e.ref for e in state.evidence}), failures
        except (LLMConfigError, LLMError, DiagnosisError) as e:
            failures.append(f"LLM engine failed: {e}; "
                            "fell back to rule-based engine")
    diagnosis = build_diagnosis(
        state.category, hypotheses, {e.ref for e in state.evidence},
        checks_performed=checks_performed,
        recommended_actions=recommended_actions, source="rules")
    return diagnosis, failures


def _self_test() -> int:
    """Day 5 self-test: contract validation + loud no-key failure.
    Run: python3 -m app.diagnosis  (from ops-agent/)"""
    ok = 0
    _checks = ["查日志：2 条命中", "查指标：error_rate_5m 与 P95 延迟"]
    _acts = [Action("确认 CPU 与负载曲线", "low"),
             Action("重启异常实例", "high", "重启后 CPU 未回落则回滚")]
    good = Diagnosis(category="cpu_saturated",
                     causes=[Cause("CPU 打满", 1.0, ["log:a", "metrics:b"]),
                             Cause("内存 OOM 被杀", 0.33, ["log:c"])],
                     summary="最可能的根因是CPU 打满（置信度 1.00），依据 2 条证据",
                     checks_performed=_checks, recommended_actions=_acts,
                     needs_human=True, source="rules")
    validate_diagnosis(good, {"log:a", "metrics:b", "log:c"})
    ok += 1
    bad_cases = [
        Diagnosis(category="x", causes=[], summary="s", source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.5, ["log:a"])],
                  summary="s", source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("A", 0.2, ["log:a"]), Cause("B", 0.9, ["log:a"])],
                  summary="s", source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:never-seen"])],
                  summary="s", source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, [])],
                  summary="s", source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary=" ", needs_human=False, source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 0.2, ["log:a"])],
                  summary="s", needs_human=False, source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary="s", needs_human=False, source="rules",
                  model="gpt-4o-mini",
                  checks_performed=_checks, recommended_actions=_acts),
        # New-field violations: empty checks, high-risk action without a
        # rollback, unknown proposing a high-risk action, high-risk
        # action without needs_human, is_unknown contradicting the Top 1
        # cause, and a cause name outside the contract vocabulary.
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary="s", needs_human=True, source="rules",
                  checks_performed=[],
                  recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary="s", needs_human=True, source="rules",
                  checks_performed=_checks,
                  recommended_actions=[Action("重启异常实例", "high")]),
        Diagnosis(category="unknown",
                  causes=[Cause(UNKNOWN_CAUSE, 0.2, ["log:a"])],
                  summary="s", needs_human=True, is_unknown=True,
                  source="rules", checks_performed=_checks,
                  recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary="s", needs_human=False, source="rules",
                  checks_performed=_checks,
                  recommended_actions=_acts),
        Diagnosis(category="x",
                  causes=[Cause("CPU 打满", 1.0, ["log:a"])],
                  summary="s", needs_human=True, is_unknown=True,
                  source="rules", checks_performed=_checks,
                  recommended_actions=[Action("整理已有证据并转人工处理",
                                              "low")]),
        Diagnosis(category="x",
                  causes=[Cause("路由器故障", 1.0, ["log:a"])],
                  summary="s", needs_human=True, source="rules",
                  checks_performed=_checks, recommended_actions=_acts),
    ]
    for i, bad in enumerate(bad_cases):
        try:
            validate_diagnosis(bad, {"log:a"})
        except DiagnosisError:
            ok += 1
        else:
            print(f"FAIL: bad case {i} passed validation: {bad}")
            return 1
    # Unknown cause may cite no evidence, but must need a human, carry
    # the handoff action, and say is_unknown.
    validate_diagnosis(
        Diagnosis(category="unknown",
                  causes=[Cause(UNKNOWN_CAUSE, 0.0, [])],
                  summary="证据不足，无法确定根因，转人工处理",
                  checks_performed=["取证未返回任何证据"],
                  recommended_actions=[Action("整理已有证据并转人工处理",
                                              "low")],
                  needs_human=True, is_unknown=True, source="rules"), set())
    ok += 1
    # Adapter without a key fails loudly, before any HTTP attempt.
    try:
        OpenAICompatibleAdapter(api_key="")
    except LLMConfigError:
        ok += 1
    else:
        print("FAIL: adapter accepted an empty key")
        return 1
    # Round-trip through JSON keeps the contract.
    assert Diagnosis.from_dict(json.loads(good.to_json())) == good
    ok += 1
    # Engine selection: forced llm without a key fails loudly, is
    # recorded, and falls back to rules; explicit rule never tries LLM.
    from types import SimpleNamespace
    _saved = {k: os.environ.get(k) for k in
              ("OPS_LLM_API_KEY", "OPS_AGENT_LLM_API_KEY",
               "OPS_DIAGNOSIS_ENGINE")}
    try:
        os.environ.pop("OPS_LLM_API_KEY", None)
        os.environ.pop("OPS_AGENT_LLM_API_KEY", None)
        os.environ["OPS_DIAGNOSIS_ENGINE"] = "llm"
        _fake = SimpleNamespace(
            category="cpu_saturated",
            alert={"title": "t", "description": "d", "service": "web-api"},
            evidence=[SimpleNamespace(ref="log:a", source="logs",
                                      summary="cpu 98%", timestamp="now")],
            evidence_gaps=[])
        _hyps = [SimpleNamespace(cause="CPU 打满", confidence=1.0,
                                 evidence_refs=["log:a"])]
        _d, _fails = diagnose_with_engine(
            _fake, hypotheses=_hyps, checks_performed=_checks,
            recommended_actions=_acts)
        assert _d.source == "rules" and len(_fails) == 1, (_d, _fails)
        ok += 1
        os.environ["OPS_DIAGNOSIS_ENGINE"] = "rule"
        _d, _fails = diagnose_with_engine(
            _fake, hypotheses=_hyps, checks_performed=_checks,
            recommended_actions=_acts)
        assert _d.source == "rules" and not _fails, (_d, _fails)
        ok += 1
    finally:
        for _k, _v in _saved.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
    print(f"diagnosis self-test: {ok}/20 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
