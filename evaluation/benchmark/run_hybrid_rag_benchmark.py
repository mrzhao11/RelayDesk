#!/usr/bin/env python3
"""Run Qwen3 Dense versus BM25/RRF Hybrid retrieval benchmarks."""
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.embedding_provider import LocalSentenceTransformerEmbedding
from mcp.knowledge_base import KnowledgeBase


MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
QUERY_INSTRUCTION = (
    "Given an enterprise SaaS customer support request, retrieve the most relevant "
    "product documentation, account policy, billing rule, or troubleshooting passage that answers the request"
)
DATA_DIR = ROOT / "evaluation" / "datasets"
OUTPUT = ROOT / "evaluation" / "results" / "design_rationale" / "hybrid_rag_ablation.json"


def percentile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for k in (1, 3, 5):
        hits, recalls = [], []
        for row in rows:
            gold = set(row["gold_ids"])
            retrieved = row["retrieved_ids"][:k]
            hits.append(float(bool(gold.intersection(retrieved))))
            recalls.append(len(gold.intersection(retrieved)) / len(gold))
        result[f"Hit@{k}"] = round(statistics.mean(hits), 4)
        result[f"Recall@{k}"] = round(statistics.mean(recalls), 4)
    reciprocal_ranks = []
    for row in rows:
        gold = set(row["gold_ids"])
        rank = next((index + 1 for index, item in enumerate(row["retrieved_ids"]) if item in gold), None)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
    result["MRR"] = round(statistics.mean(reciprocal_ranks), 4)
    latencies = [row["retrieval_latency_ms"] for row in rows]
    result["latency_ms"] = {
        "retrieval_p50": round(percentile(latencies, 0.5), 3),
        "retrieval_p95": round(percentile(latencies, 0.95), 3),
        "rewrite_p50": 0.0,
        "rewrite_p95": 0.0,
        "rerank_p50": 0.0,
        "rerank_p95": 0.0,
        "total_p50": round(percentile(latencies, 0.5), 3),
        "total_p95": round(percentile(latencies, 0.95), 3),
    }
    result["sample_count"] = len(rows)
    return result


def rank_of(ids: Sequence[str], gold: Sequence[str]) -> Any:
    gold_set = set(gold)
    return next((index + 1 for index, item in enumerate(ids) if item in gold_set), None)


async def run_cases(kb: KnowledgeBase, samples: Sequence[Dict[str, Any]], gold_kind: str) -> Dict[str, Any]:
    dense_rows, hybrid_rows, comparisons = [], [], []
    for sample in samples:
        if gold_kind == "title":
            gold = sample["relevant_titles"]
        else:
            gold = sample["gold_chunk_ids"]

        started = time.perf_counter()
        dense_items = await kb.search_async(sample["query"], top_k=5)
        dense_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        hybrid_items = await kb.retrieve_async(sample["query"], top_k=5, use_bm25=True, candidate_k=10)
        hybrid_ms = (time.perf_counter() - started) * 1000
        bm25_items = kb.search_bm25(sample["query"], top_k=5)

        dense_ids = [item["title"] if gold_kind == "title" else item["chunk_id"] for item in dense_items]
        hybrid_ids = [item["title"] if gold_kind == "title" else item["chunk_id"] for item in hybrid_items]
        dense_rows.append({
            "id": sample["id"], "gold_ids": gold, "retrieved_ids": dense_ids,
            "retrieval_latency_ms": dense_ms,
        })
        hybrid_rows.append({
            "id": sample["id"], "gold_ids": gold, "retrieved_ids": hybrid_ids,
            "retrieval_latency_ms": hybrid_ms,
        })
        dense_rank = rank_of(dense_ids, gold)
        hybrid_rank = rank_of(hybrid_ids, gold)
        dense_value = 1 / dense_rank if dense_rank else 0.0
        hybrid_value = 1 / hybrid_rank if hybrid_rank else 0.0
        outcome = "improved" if hybrid_value > dense_value else "regressed" if hybrid_value < dense_value else "unchanged"
        comparisons.append({
            "id": sample["id"],
            "query": sample["query"],
            "gold": gold,
            "dense_rank": dense_rank,
            "hybrid_rank": hybrid_rank,
            "rank_delta": (dense_rank or 6) - (hybrid_rank or 6),
            "outcome": outcome,
            "dense_candidates": dense_items,
            "bm25_candidates": bm25_items,
            "rrf_candidates": hybrid_items,
            "final_candidates": hybrid_items,
        })
    return {
        "dense": {"metrics": metrics(dense_rows), "rows": dense_rows},
        "hybrid": {"metrics": metrics(hybrid_rows), "rows": hybrid_rows},
        "comparison": comparisons,
        "outcome_counts": {
            key: sum(row["outcome"] == key for row in comparisons)
            for key in ("improved", "regressed", "unchanged")
        },
    }


def chunking_diagnostics(documents: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    from rag.chunker import StructuredChunker
    chunks = [chunk for doc in documents for chunk in StructuredChunker(500, 80).chunk_document(doc)]
    sizes = [len(chunk["content"]) for chunk in chunks]
    return {
        "new_chunk_count": len(chunks),
        "average_chunk_size": round(statistics.mean(sizes), 3),
        "p95_chunk_size": round(percentile(sizes, 0.95), 3),
        "empty_chunk_count": sum(not chunk["content"].strip() for chunk in chunks),
        "stable_chunk_id_count": len({chunk["chunk_id"] for chunk in chunks}),
        "strategy": "Markdown section -> paragraph -> sentence -> hard split, with natural-boundary overlap",
        "old_chunking_retrieval_comparison": "NOT EXECUTED",
        "reason": "The hard-set documents are shorter than 500 characters; structural tests cover boundary behavior, but retrieval metrics would not isolate chunking on this corpus.",
    }


async def main() -> None:
    values = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    model_source = os.getenv("EMBEDDING_MODEL_SOURCE", values.get("EMBEDDING_MODEL") or MODEL_NAME)
    embedding = LocalSentenceTransformerEmbedding(
        model_name=model_source,
        device=values.get("EMBEDDING_DEVICE") or "cpu",
        query_prompt_name=values.get("EMBEDDING_QUERY_PROMPT") or "query",
        query_instruction=values.get("EMBEDDING_QUERY_INSTRUCTION") or QUERY_INSTRUCTION,
        normalize_embeddings=True,
    )
    existing = json.loads((DATA_DIR / "rag_design_rationale.json").read_text())["samples"]
    existing = [sample for sample in existing if sample["relevant_titles"]]
    hard_payload = json.loads((DATA_DIR / "hybrid_retrieval_hard_cases.json").read_text())

    with tempfile.TemporaryDirectory(prefix="relaydesk-hybrid-rag-") as directory:
        default_kb = KnowledgeBase(
            chroma_host="127.0.0.1", chroma_port=65535, chroma_path=directory,
            embedding_function=embedding, embedding_model=MODEL_NAME,
            collection_name="hybrid-default-v2",
        )
        existing_result = await run_cases(default_kb, existing, "title")
        hard_kb = KnowledgeBase(
            chroma_host="127.0.0.1", chroma_port=65535, chroma_path=directory,
            embedding_function=embedding, embedding_model=MODEL_NAME,
            collection_name="hybrid-hard-v2", load_defaults=False,
        )
        hard_kb.add_documents(hard_payload["documents"])
        hard_result = await run_cases(hard_kb, hard_payload["samples"], "chunk")

    payload = {
        "status": "EXECUTED",
        "model": MODEL_NAME,
        "dense_store": "ChromaDB cosine",
        "lexical_retriever": "in-process BM25",
        "fusion": "RRF(k=60)",
        "existing_verified_baseline": {
            "Recall@1": 0.7727, "Recall@3": 0.9545, "Recall@5": 1.0, "MRR": 0.8879,
            "notice": "Preserved historical Qwen3 Direct result; not recomputed or overwritten by this field.",
        },
        "new_experiment": {
            "existing_answerable_set": existing_result,
            "lexical_hard_set": hard_result,
            "optional_llm_variants": {
                "dense_rewrite": "NOT EXECUTED",
                "hybrid_rewrite": "NOT EXECUTED",
                "hybrid_rewrite_rerank": "NOT EXECUTED",
                "reason": "A/B Dense and Hybrid were fully executed. LLM variants remain optional because prior audited runs showed structured-output truncation and high latency; no metrics were fabricated.",
            },
        },
        "chunking_diagnostics": chunking_diagnostics(hard_payload["documents"]),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "output": str(OUTPUT),
        "existing": {key: value["metrics"] for key, value in existing_result.items() if key in ("dense", "hybrid")},
        "hard": {key: value["metrics"] for key, value in hard_result.items() if key in ("dense", "hybrid")},
        "hard_outcomes": hard_result["outcome_counts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
