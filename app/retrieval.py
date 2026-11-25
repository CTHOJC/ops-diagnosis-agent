#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 6: RAG knowledge-base retrieval.

Sprint Day 6 (plan Ch.11): the diagnose node can cite the runbook, so
search_runbook now runs the real three-stage retrieval instead of the
Day 1 single-token-overlap scan:

  1. retrieve — BM25 + TF-IDF-cosine vector path, fused with RRF (k=60);
  2. rerank   — cross-encoder when available, local lexical rescoring
     otherwise (see _rerank);
  3. gate     — the caller (tools.search_runbook) keeps the Day 5
     MIN_RUNBOOK_OVERLAP floor, so a chunk is evidence only if it
     shares at least that many whole-word tokens with the query.

What is real here, and what is a stand-in (read before quoting any of
this on a resume):
  * BM25 is the real algorithm (copied from rag-week1-starter/
    bm25_baseline.py — the same scorer ES would run, only local).
  * The "vector" path is TF-IDF cosine (copied from
    hybrid_retrieval.py): same query-vs-chunk-vectors shape a Milvus
    ANN search will take, but lexical, not a neural embedding. It
    cannot do true semantic matching.
  * Rerank tries BAAI/bge-reranker-base via sentence-transformers
    first; this machine does not have it installed, so the run falls
    back to local lexical reranking (BM25 rescore + word overlap).
    The returned info always names which reranker actually ran —
    a local rerank is never reported as a cross-encoder.
  * ES / Milvus are probed, not assumed: ES is tried over HTTP at
    OPS_ES_URL (default http://127.0.0.1:9200, index `rag_chunks` —
    the index index_chunks.py defines); Milvus is tried via pymilvus
    (collection `rag_chunks`, http://localhost:19530). This machine
    has no Docker, so both probes fail and retrieval degrades to the
    local implementation; the reason for each downgrade is recorded
    in the returned info and in the tool audit, exactly like
    demo_service/es_writer.py falls back to a local file. When eng
    brings the Docker stack up on their own machine, the probes will
    succeed and the same code will use the live backends.

Corpus: the 7 sample ops manuals in rag-week1-starter/corpus/*.md
(including cpu-high.md, added 2026-11-18). They are chunked on the
spot with chunk.py's heading-split logic (stable sha256 chunk_ids)
into this project's own index, app/runbook_chunks.jsonl — 35 chunks
expected (7 docs x 5 sections). rag-week1-starter/chunks.jsonl is
never touched (still 30 chunks; the RAG Week 1 eval numbers must not
move).

Stdlib + numpy (1.26.4, already installed). No torch, no
sentence-transformers install — that is out of scope for Day 6.
No LLM endpoint is called anywhere in this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# The RAG project's corpus (read-only). Only corpus/*.md is read;
# nothing under rag-week1-starter is ever written by this module.
CORPUS_DIR = ROOT.parent / "rag-week1-starter" / "corpus"
INDEX_FILE = HERE / "runbook_chunks.jsonl"

RRF_K = 60
RERANK_CANDIDATES = 15
RERANKER_MODEL = "BAAI/bge-reranker-base"
ES_INDEX = "rag_chunks"
MILVUS_COLLECTION = "rag_chunks"
MILVUS_URI = "http://localhost:19530"

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


# ---------------------------------------------------------------------------
# Tokenizers — both copied from the RAG project, unchanged.
# tokenize: BM25 / TF-IDF (bm25_baseline.py + hybrid_retrieval.py):
#   latin/digit words + CJK single chars + CJK bigrams.
# word_tokens: whole words (app/tools.py _tokens) — the Day 5 evidence
#   gate counts these, because a single shared CJK char or bigram is
#   noise while a shared whole word is a real topical match.
# ---------------------------------------------------------------------------
def tokenize(text):
    text = text.lower()
    toks = []
    toks += re.findall(r"[a-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    toks += cjk
    toks += ["".join(p) for p in zip(cjk, cjk[1:])]
    return toks


def word_tokens(text):
    return [t for t in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text.lower()) if t]


def word_overlap(query, text):
    return len(set(word_tokens(query)) & set(word_tokens(text)))


# Day 11-12: tokens that must not count toward the evidence gate.
# Single-digit numbers ("2", "4") match across unrelated manuals —
# the release alert shares only {"2", "4"} with five different
# manuals (fragments of "v1.4.2") — and "日志出现" is boilerplate in
# almost every manual's 现象 section (the disk alert shares
# {"space", "日志出现"} with the java-oom manual). Counting them
# fabricates runbook evidence exactly the way the Day 5 single-token
# overlap did. Multi-digit numbers stay: "502" is a real error code
# (the MCP smoke query "Nginx 返回 502 …" shares only {"nginx",
# "502"} with the nginx manual — excluding all numbers would drop
# it to 1 and fail the Day 3 smoke test). The gate counts only the
# remaining shared whole words. Verified on all 14 demo alerts (see
# /tmp/shared_tokens.py): 502 keeps its 3 nginx chunks, cpu keeps
# its 3, oom keeps its intro chunk, disk keeps only disk-full, the
# release/jitter junk chunks drop to 0, unknown still gets none.
GENERIC_OVERLAP_TOKENS = {"日志出现", "错误日志出现"}


def content_overlap(query, text):
    """Shared whole-word tokens minus single-digit numbers and
    generic boilerplate — the overlap that actually evidences
    relevance."""
    shared = set(word_tokens(query)) & set(word_tokens(text))
    return len({t for t in shared
                if not (len(t) == 1 and t.isdigit())
                and t not in GENERIC_OVERLAP_TOKENS})


# ---------------------------------------------------------------------------
# Chunking — chunk.py's heading-split logic, copied (not imported: the
# RAG project is a sibling, not a dependency of this package).
# chunk_id is a stable hash of doc_id + section + text, so rebuilding
# the index yields identical ids.
# ---------------------------------------------------------------------------
def chunk_doc(path: Path) -> list[dict]:
    doc_id = path.stem
    chunks: list[dict] = []
    section = "intro"
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        raw = f"{doc_id}|{section}|{text}"
        chunk_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        chunks.append({"doc_id": doc_id, "chunk_id": chunk_id,
                       "source": path.name, "section": section,
                       "text": text, "version": 1})

    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            section = m.group(2).strip()
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return chunks


def build_index() -> list[dict]:
    """Chunk all 7 manuals and write this project's own index file."""
    if not CORPUS_DIR.exists():
        raise FileNotFoundError(
            f"runbook corpus not found: {CORPUS_DIR} "
            "(and no index file to fall back to)")
    chunks: list[dict] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        chunks.extend(chunk_doc(path))
    with INDEX_FILE.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return chunks


def load_chunks() -> list[dict]:
    """This project's index if it exists, else build it from the corpus.

    Raises FileNotFoundError when neither exists — the caller
    (tools.search_runbook) then falls back to the built-in runbook.
    """
    if INDEX_FILE.exists():
        return [json.loads(line) for line in
                INDEX_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    return build_index()


# ---------------------------------------------------------------------------
# Hybrid retriever — BM25 + TF-IDF cosine + RRF, copied from
# hybrid_retrieval.py (same formulas, same constants). The indexed text
# is "doc_id / section\ntext", the same string index_chunks.py would
# embed for Milvus, so local and real numbers stay comparable.
# ---------------------------------------------------------------------------
class HybridRetriever:
    def __init__(self, chunks):
        self.chunks = chunks
        self.docs = [f"{c['doc_id']} / {c['section']}\n{c['text']}"
                     for c in chunks]
        self.tokenized = [tokenize(d) for d in self.docs]
        self.N = len(self.docs)
        self.avgdl = sum(len(t) for t in self.tokenized) / self.N
        self.df = Counter()
        for t in self.tokenized:
            self.df.update(set(t))
        # TF-IDF vectors — the vector-path stand-in (lexical, not a
        # neural embedding; see the module docstring).
        vocab = sorted(self.df)
        self.vocab_index = {t: i for i, t in enumerate(vocab)}
        idf = np.array([math.log(1 + (self.N - self.df[t] + 0.5)
                                 / (self.df[t] + 0.5))
                        for t in vocab], dtype=float)
        mat = np.zeros((self.N, len(vocab)), dtype=float)
        for i, toks in enumerate(self.tokenized):
            for term, tf in Counter(toks).items():
                mat[i, self.vocab_index[term]] = (
                    tf * idf[self.vocab_index[term]])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        self.tfidf = mat / np.where(norms == 0, 1, norms)
        self.idf = idf

    def _idf(self, term):
        return math.log(1 + (self.N - self.df[term] + 0.5)
                        / (self.df[term] + 0.5))

    def bm25_scores(self, query):
        k1, b = 1.5, 0.75
        q = set(tokenize(query))
        scores = []
        for toks in self.tokenized:
            tf = Counter(toks)
            dl = len(toks)
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                s += self._idf(term) * tf[term] * (k1 + 1) / (
                    tf[term] + k1 * (1 - b + b * dl / self.avgdl))
            scores.append(s)
        return scores

    def bm25_rank(self, query):
        return list(np.argsort([-s for s in self.bm25_scores(query)]))

    def tfidf_rank(self, query):
        qvec = np.zeros(len(self.vocab_index), dtype=float)
        for term, tf in Counter(tokenize(query)).items():
            if term in self.vocab_index:
                qvec[self.vocab_index[term]] = (
                    tf * self.idf[self.vocab_index[term]])
        n = np.linalg.norm(qvec)
        if n == 0:
            return list(range(self.N))  # no shared tokens: arbitrary order
        qvec = qvec / n
        return list(np.argsort(-(self.tfidf @ qvec)))

    def rrf_rank(self, query):
        fused = Counter()
        for rank_list in (self.bm25_rank(query), self.tfidf_rank(query)):
            for rank, idx in enumerate(rank_list):
                fused[idx] += 1.0 / (RRF_K + rank + 1)
        return [idx for idx, _ in fused.most_common()]

    def label(self, idx):
        c = self.chunks[idx]
        return f"{c['doc_id']} / {c['section']}"


# Retriever cache: 35 chunks build in milliseconds, but search_runbook
# runs in a ThreadPoolExecutor and can be called twice per incident —
# rebuild only when the index file changes.
_retriever_lock = threading.Lock()
_retriever_cache: dict = {}


def get_retriever(chunks=None) -> HybridRetriever:
    key = None
    if chunks is None:
        try:
            st = INDEX_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
    with _retriever_lock:
        if key is not None and key in _retriever_cache:
            return _retriever_cache[key]
        r = HybridRetriever(chunks if chunks is not None else load_chunks())
        if key is not None:
            _retriever_cache.clear()
            _retriever_cache[key] = r
        return r


# ---------------------------------------------------------------------------
# Backend probes — ES / Milvus are tried, never assumed. Each returns a
# status dict that is recorded in the search info and the tool audit.
# ---------------------------------------------------------------------------
def es_url() -> str:
    return os.environ.get("OPS_ES_URL", "http://127.0.0.1:9200").rstrip("/")


def probe_es(timeout: float = 2.0) -> dict:
    """Try the live ES BM25 path (index `rag_chunks`, see
    index_chunks.py). Returns reachable only if a real search answers;
    anything else degrades to the local BM25 with the reason recorded."""
    try:
        with urllib.request.urlopen(es_url(), timeout=timeout) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001 — any failure is a downgrade
        return {"status": "unreachable",
                "reason": f"ES at {es_url()} did not answer: {e}"}
    try:
        body = json.dumps({"size": 1, "query": {"match": {"text": "test"}}}
                          ).encode("utf-8")
        req = urllib.request.Request(
            f"{es_url()}/{ES_INDEX}/_search", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.loads(resp.read().decode("utf-8"))
        return {"status": "reachable", "index": ES_INDEX}
    except Exception as e:  # noqa: BLE001 — reachable host, unusable index
        return {"status": "degraded",
                "reason": f"ES reachable but index {ES_INDEX!r} search "
                          f"failed: {e}"}


def probe_milvus() -> dict:
    """Try the live Milvus vector path (collection `rag_chunks`, see
    index_chunks.py). pymilvus is not installed on this machine, so the
    probe fails at the import and the TF-IDF stand-in is used instead."""
    try:
        from pymilvus import MilvusClient
    except ImportError as e:
        return {"status": "unavailable",
                "reason": f"pymilvus not installed: {e}"}
    try:
        client = MilvusClient(uri=MILVUS_URI)
        if client.has_collection(MILVUS_COLLECTION):
            return {"status": "reachable",
                    "collection": MILVUS_COLLECTION}
        return {"status": "degraded",
                "reason": f"Milvus reachable but collection "
                          f"{MILVUS_COLLECTION!r} does not exist yet "
                          "(run index_chunks.py first)"}
    except Exception as e:  # noqa: BLE001 — any failure is a downgrade
        return {"status": "unreachable",
                "reason": f"Milvus at {MILVUS_URI} did not answer: {e}"}


# ---------------------------------------------------------------------------
# Rerank — cross-encoder first, local lexical fallback. The reranker
# actually used is always reported; a local rerank is never dressed
# up as a cross-encoder score.
# ---------------------------------------------------------------------------
_reranker_lock = threading.Lock()
_reranker_state: dict | None = None


def _load_cross_encoder():
    """(reranker, name, reason). The import + model load are attempted
    once per process and the outcome (including the failure) is cached,
    so 23 eval questions do not retry a download 23 times."""
    global _reranker_state
    with _reranker_lock:
        if _reranker_state is not None:
            return _reranker_state
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            _reranker_state = (None, "local-lexical",
                               f"sentence-transformers not installed: {e}")
            return _reranker_state
        try:
            model = CrossEncoder(RERANKER_MODEL)
        except Exception as e:  # noqa: BLE001 — download / load failure
            _reranker_state = (
                None, "local-lexical",
                f"cross-encoder {RERANKER_MODEL} failed to load: {e}")
            return _reranker_state
        _reranker_state = (model, "cross-encoder", "")
        return _reranker_state


def _rerank(retriever, query, candidates):
    """Reorder RRF candidates. Returns (ordered_idx, reranker, reason)."""
    model, name, reason = _load_cross_encoder()
    if model is not None:
        pairs = [(query, retriever.docs[i]) for i in candidates]
        try:
            scores = model.predict(pairs)
        except Exception as e:  # noqa: BLE001 — score failure: lexical
            name, reason = ("local-lexical",
                            f"cross-encoder predict failed: {e}; "
                            "fell back to local lexical rerank")
            model = None
        else:
            order = [idx for _, idx in
                     sorted(zip(scores, candidates), reverse=True)]
            rest = [i for i in retriever.rrf_rank(query)
                    if i not in set(order)]
            return order + rest, name, reason
    # Local lexical rerank: BM25 rescore + whole-word overlap bonus.
    # Lexical only — it reorders, it cannot add semantic signal.
    bm25 = retriever.bm25_scores(query)
    scored = sorted(
        candidates,
        key=lambda i: -(bm25[i] + word_overlap(query, retriever.docs[i])))
    rest = [i for i in retriever.rrf_rank(query) if i not in set(scored)]
    return scored + rest, name, reason


# ---------------------------------------------------------------------------
# Search — the three-stage pipeline. Returns (results, info):
#   results: [{chunk_id, source ("doc / section"), text}] (top_k max,
#     after the optional min_overlap evidence gate);
#   info: backends actually used, chunk counts, gate counts — so the
#     caller and the eval harness can report honestly what ran.
# ---------------------------------------------------------------------------
def search(query, top_k=3, candidates=RERANK_CANDIDATES,
           min_overlap=None, chunks=None):
    retriever = get_retriever(chunks)
    rrf = retriever.rrf_rank(query)[:candidates]
    ranked, reranker, reranker_reason = _rerank(retriever, query, rrf)
    gated_out = 0
    picked = []
    for i in ranked:
        c = retriever.chunks[i]
        if min_overlap is not None:
            # Day 11-12: the gate counts content-bearing overlap only
            # (see content_overlap) — bare numbers and "日志出现"
            # boilerplate no longer fabricate runbook evidence.
            ov = content_overlap(query, c["text"])
            if ov < min_overlap:
                gated_out += 1
                continue
        picked.append({"chunk_id": c["chunk_id"],
                       "source": f"{c['doc_id']} / {c['section']}",
                       "text": c["text"][:200]})
        if len(picked) >= top_k:
            break
    info = {
        "chunks": len(retriever.chunks),
        "docs": sorted({c["doc_id"] for c in retriever.chunks}),
        "index_file": str(INDEX_FILE),
        "es": probe_es(),
        "milvus": probe_milvus(),
        "reranker": reranker,
        "reranker_reason": reranker_reason,
        "candidates": len(rrf),
        "gated_out": gated_out,
        "returned": len(picked),
    }
    return picked, info


# ---------------------------------------------------------------------------
# History cases — Day 10 (plan Ch.6 module 8: archived cases feed back
# into retrieval). This is a SEPARATE entry point from search(): search()
# above keeps doing manual-chunk retrieval only, unchanged; the caller
# (tools.search_runbook) appends at most one case after the manual
# Top 3. Cases are matched lexically, like the manuals — no embedding,
# no LLM — on the case's "根因 + 处置摘要" text.
#
# Why this matcher, and not plain word overlap (the Day 5 gate)?
# Chinese has no spaces, so whole-word tokens are long runs:
# "持续打满" (query) never equals "打满" (case root cause) as a whole
# word, and a 502 case's root cause shares no whole word at all with
# its alert. So case matching reuses this module's tokenize() —
# latin words + CJK single chars + CJK bigrams — and counts shared
# terms of length >= 2 (single chars and bare digits are noise; the
# Day 5 lesson was that a bare "5" fabricates evidence). The gate
# is the SAME threshold as the manual one, MIN_RUNBOOK_OVERLAP = 2,
# passed in by the caller.
#
# Two extra guards, both structural, not tuned to any one case:
#   * the root cause itself must contribute at least one NON-GENERIC
#     shared term. Generic terms ("实例", "服务") appear in unrelated
#     symptoms ("服务实例掉线" vs "上游实例宕机") and must not be
#     the only link between a symptom and a case. Without this, an
#     unknown symptom scores 2 on a CPU/OOM case purely on the
#     generic bigrams 错误 / 异常 / 日志 and would cite a precedent
#     for a fault nobody diagnosed.
#   * at most one case is returned (max_cases=1): cases supplement
#     the manual Top 3, they never displace them.
# Verified on the demo alerts (see README Day 10): CPU->CPU 3,
# 502->502 2, OOM->OOM 4; unknown->CPU 2 total but 0 from the root,
# unknown->OOM 2 total but 0 from the root, unknown->502 1 — all
# three unknown pairings blocked; cross-type pairings (CPU->OOM,
# OOM->502, 502->CPU) score 0 on the root-cause gate.
# ---------------------------------------------------------------------------
CASES_DIR = ROOT / "data" / "cases"

# Terms so generic they cannot be the only link between a symptom and
# a case (see above): "实例" is in almost every ops sentence.
# Day 11-12: "异常" / "错误" / "日志" / "指标" join the list — the
# unknown symptom ("页面字体突然变大…无错误日志、无指标异常…")
# shares the bigram "异常" with the root cause "配置变更引发异常"
# and was citing a config-change precedent for a font complaint.
# With these generic, that pairing is blocked (root contribution 0);
# every real root still contributes a specific term (CPU 打满→"打满",
# 配置变更引发异常→"配置"/"变更", 上游实例宕机→"宕机", …), so
# legitimate same-cause matches are unaffected.
GENERIC_ROOT_TERMS = {"实例", "服务", "异常", "错误", "日志", "指标"}


def _case_terms(text: str) -> set[str]:
    """Shared-term vocabulary for case matching: tokenize() minus
    length-1 tokens (single CJK chars, bare digits — noise)."""
    return {t for t in tokenize(text) if len(t) >= 2}


def search_cases(query, max_cases=1, min_overlap=2) -> list[dict]:
    """Best-matching archived cases for `query`, or [].

    Each hit is shaped like a manual chunk so the caller can treat it
    the same way: {"chunk_id": "case:<incident_id>",
    "source": "历史案例 / <incident_id>", "text": "根因：…；处置：…；
    结果：…"}. Only cases whose "根因 + 处置摘要" shares at least
    `min_overlap` terms with the query AND whose root cause shares at
    least one non-generic term count as a match.
    """
    scored = []
    # Day 11-12: the cases directory honours OPS_AGENT_CASES_DIR
    # (see cases.get_cases_dir) so the eval harness can isolate runs.
    from .cases import get_cases_dir
    cases_dir = get_cases_dir()
    if not cases_dir.exists():
        return []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — one bad file skips, no fail
            continue
        root = case.get("root_cause", "") or ""
        actions = case.get("action_summary", "") or ""
        total = _case_terms(query) & _case_terms(f"{root} {actions}")
        root_terms = _case_terms(query) & _case_terms(root)
        root_specific = {t for t in root_terms
                         if t not in GENERIC_ROOT_TERMS}
        if len(total) >= min_overlap and root_specific:
            scored.append((len(total), case))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, case in scored[:max_cases]:
        out.append({
            "chunk_id": f"case:{case.get('incident_id', '')}",
            "source": f"历史案例 / {case.get('incident_id', '')}",
            "text": (
                f"根因：{case.get('root_cause', '')}"
                f"（置信度 {float(case.get('confidence', 0)):.2f}）；"
                f"处置：{case.get('action_summary', '')}；"
                f"结果：{case.get('exec_summary', '')}"),
        })
    return out


def describe() -> dict:
    """Backend status without running a query (for the CLI check)."""
    try:
        chunks = load_chunks()
        n, docs = len(chunks), sorted({c["doc_id"] for c in chunks})
    except FileNotFoundError as e:
        return {"chunks": 0, "docs": [], "error": str(e),
                "es": probe_es(), "milvus": probe_milvus(),
                "reranker": _load_cross_encoder()[1],
                "reranker_reason": _load_cross_encoder()[2]}
    _, _, reranker_reason = _load_cross_encoder()
    return {"chunks": n, "docs": docs, "index_file": str(INDEX_FILE),
            "es": probe_es(), "milvus": probe_milvus(),
            "reranker": _load_cross_encoder()[1],
            "reranker_reason": reranker_reason}


def _self_check() -> int:
    """Build the index if needed and report what actually runs.
    Run: python3 -m app.retrieval [--query "..."]  (from ops-agent/)"""
    import sys
    query = None
    if "--query" in sys.argv:
        query = sys.argv[sys.argv.index("--query") + 1]
    chunks = load_chunks()
    print(f"index: {len(chunks)} chunks from "
          f"{len({c['doc_id'] for c in chunks})} docs -> {INDEX_FILE}")
    info = describe()
    print(f"ES: {info['es']['status']}"
          + (f" ({info['es'].get('reason', '')})"
             if info['es']['status'] != 'reachable' else ""))
    print(f"Milvus: {info['milvus']['status']}"
          + (f" ({info['milvus'].get('reason', '')})"
             if info['milvus']['status'] != 'reachable' else ""))
    print(f"reranker: {info['reranker']}"
          + (f" ({info['reranker_reason']})" if info['reranker_reason'] else ""))
    if query:
        results, _ = search(query, top_k=3, min_overlap=2)
        print(f"query: {query!r} -> {len(results)} chunks "
              f"(min_overlap=2)")
        for r in results:
            print(f"  - {r['source']} [{r['chunk_id']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
