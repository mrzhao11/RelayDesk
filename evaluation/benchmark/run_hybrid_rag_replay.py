#!/usr/bin/env python3
"""Replay verified Qwen3 Dense ranks through the new BM25/RRF pipeline."""
import ast
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.benchmark.run_hybrid_rag_benchmark import metrics, rank_of
from rag.bm25_retriever import BM25Retriever
from rag.chunker import StructuredChunker
from rag.fusion import reciprocal_rank_fusion


DATA_DIR = ROOT / "evaluation" / "datasets"
RESULT_DIR = ROOT / "evaluation" / "results" / "design_rationale"
OUTPUT = RESULT_DIR / "hybrid_rag_ablation.json"


def default_documents() -> List[Dict[str, str]]:
    tree = ast.parse((ROOT / "mcp" / "knowledge_base.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_load_default_docs":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "default_docs" for target in statement.targets
                ):
                    return ast.literal_eval(statement.value)
    raise RuntimeError("default_docs not found")


def simple_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return metrics(rows)


def main() -> None:
    chunker = StructuredChunker(500, 80)
    default_chunks = [chunk for doc in default_documents() for chunk in chunker.chunk_document(doc)]
    chunks_by_title = {chunk["title"]: chunk for chunk in default_chunks}
    bm25 = BM25Retriever()
    bm25.build(default_chunks)
    samples = json.loads((DATA_DIR / "rag_design_rationale.json").read_text())["samples"]
    samples = [sample for sample in samples if sample["relevant_titles"]]
    verified = json.loads((RESULT_DIR / "embedding_ablation.json").read_text())["results"]["examples"]
    verified_by_id = {row["id"]: row for row in verified}
    dense_rows, hybrid_rows, comparisons, stage_latencies = [], [], [], []

    for sample in samples:
        verified_row = verified_by_id[sample["id"]]
        dense_items = []
        for title, score in zip(verified_row["candidate_top5"], verified_row["candidate_top5_scores"]):
            chunk = chunks_by_title[title]
            dense_items.append({**chunk, "score": score})
        started = time.perf_counter()
        bm25_items = bm25.search(sample["query"], top_k=10)
        hybrid_items = reciprocal_rank_fusion([
            ("dense", sample["query"], dense_items),
            ("bm25", sample["query"], bm25_items),
        ], k=60)[:5]
        stage_ms = (time.perf_counter() - started) * 1000
        stage_latencies.append(stage_ms)
        dense_titles = [item["title"] for item in dense_items]
        hybrid_titles = [item["title"] for item in hybrid_items]
        gold = sample["relevant_titles"]
        dense_rows.append({
            "id": sample["id"], "gold_ids": gold, "retrieved_ids": dense_titles,
            "retrieval_latency_ms": 0.0,
        })
        hybrid_rows.append({
            "id": sample["id"], "gold_ids": gold, "retrieved_ids": hybrid_titles,
            "retrieval_latency_ms": stage_ms,
        })
        dense_rank, hybrid_rank = rank_of(dense_titles, gold), rank_of(hybrid_titles, gold)
        dense_value = 1 / dense_rank if dense_rank else 0.0
        hybrid_value = 1 / hybrid_rank if hybrid_rank else 0.0
        comparisons.append({
            "id": sample["id"], "query": sample["query"], "gold": gold,
            "dense_rank": dense_rank, "hybrid_rank": hybrid_rank,
            "rank_delta": (dense_rank or 6) - (hybrid_rank or 6),
            "outcome": "improved" if hybrid_value > dense_value else "regressed" if hybrid_value < dense_value else "unchanged",
            "dense_candidates": dense_items, "bm25_candidates": bm25_items,
            "rrf_candidates": hybrid_items, "final_candidates": hybrid_items,
        })

    dense_result = simple_metrics(dense_rows)
    hybrid_result = simple_metrics(hybrid_rows)
    # Replay has no per-query Qwen latency; preserve only ranking metrics and the measured lexical/fusion stage.
    dense_result["latency_ms"] = "NOT RE-MEASURED; see existing verified baseline"
    hybrid_result["latency_ms"] = {
        "bm25_rrf_stage_p50": round(statistics.median(stage_latencies), 3),
        "bm25_rrf_stage_p95": round(sorted(stage_latencies)[int((len(stage_latencies) - 1) * 0.95)], 3),
        "total": "NOT RE-MEASURED because Qwen3 query inference could not run under current memory limit",
    }

    hard = json.loads((DATA_DIR / "hybrid_retrieval_hard_cases.json").read_text())
    hard_chunks = [chunk for doc in hard["documents"] for chunk in chunker.chunk_document(doc)]
    hard_bm25 = BM25Retriever()
    hard_bm25.build(hard_chunks)
    hard_rows, hard_details = [], []
    for sample in hard["samples"]:
        started = time.perf_counter()
        items = hard_bm25.search(sample["query"], top_k=5)
        latency = (time.perf_counter() - started) * 1000
        ids = [item["chunk_id"] for item in items]
        hard_rows.append({
            "id": sample["id"], "gold_ids": sample["gold_chunk_ids"],
            "retrieved_ids": ids, "retrieval_latency_ms": latency,
        })
        hard_details.append({
            "id": sample["id"], "query": sample["query"], "gold": sample["gold_chunk_ids"],
            "bm25_rank": rank_of(ids, sample["gold_chunk_ids"]), "bm25_candidates": items,
            "dense_candidates": "NOT EXECUTED", "rrf_candidates": "NOT EXECUTED",
            "final_candidates": items,
        })

    counts = {
        key: sum(row["outcome"] == key for row in comparisons)
        for key in ("improved", "regressed", "unchanged")
    }
    payload = {
        "status": "PARTIALLY EXECUTED",
        "execution_note": (
            "The repository's verified Qwen3 Direct Top-5 results were replayed without modification. "
            "BM25 and RRF were executed now against the new stable chunks. Fresh Qwen3 inference was attempted "
            "twice and terminated by the host with exit 137; Docker fallback was stopped after registry metadata stalled."
        ),
        "existing_verified_baseline": {
            "Recall@1": 0.7727, "Recall@3": 0.9545, "Recall@5": 1.0, "MRR": 0.8879,
        },
        "existing_answerable_set_replay": {
            "Dense Direct": dense_result,
            "Hybrid Direct": hybrid_result,
            "outcome_counts": counts,
            "per_query_comparison": comparisons,
        },
        "lexical_hard_set": {
            "BM25 Only": simple_metrics(hard_rows),
            "Dense Direct": "NOT EXECUTED due Qwen3 host memory limit",
            "Hybrid Direct": "NOT EXECUTED because fresh Dense ranks were unavailable",
            "per_query_results": hard_details,
        },
        "optional_llm_variants": {
            "Dense + Rewrite": "NOT EXECUTED",
            "Hybrid + Rewrite": "NOT EXECUTED",
            "Hybrid + Rewrite + Rerank": "NOT EXECUTED",
            "reason": "The required A/B replay was prioritized; known LLM structured-output latency/fallback metrics were not fabricated.",
        },
        "chunking": {
            "strategy": "Markdown section -> paragraph -> sentence -> hard split, with natural-boundary overlap",
            "default_document_chunk_count": len(default_chunks),
            "hard_set_chunk_count": len(hard_chunks),
            "empty_chunk_count": sum(not chunk["content"].strip() for chunk in default_chunks + hard_chunks),
            "old_vs_new_retrieval": "NOT EXECUTED; current demo documents are below the 500-character boundary",
        },
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "output": str(OUTPUT), "dense": dense_result, "hybrid": hybrid_result,
        "outcomes": counts, "hard_bm25": payload["lexical_hard_set"]["BM25 Only"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
