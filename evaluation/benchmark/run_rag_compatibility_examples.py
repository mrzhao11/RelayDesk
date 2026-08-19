#!/usr/bin/env python3
"""Run a small RAG diagnostic with a larger structured-output budget.

This does not modify production configuration. It reuses production rewrite and
rerank prompts while raising max_tokens from 256 to 4096 for six fixed examples.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.benchmark.run_design_rationale import (
    DATA_DIR,
    DEFAULT_OUTPUT_DIR,
    envelope,
    llm_settings,
    load_json,
    retrieval_metrics,
    tracked_llm_client,
    write_json,
)


SELECTED_IDS = {"RAG-001", "RAG-006", "RAG-008", "RAG-011", "RAG-012", "RAG-014"}


def case_row(sample: Dict[str, Any], items: Sequence[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {
        "id": sample["id"],
        "query": sample["query"],
        "query_type": sample["query_type"],
        "relevant_titles": sample["relevant_titles"],
        "retrieved_titles": [item["title"] for item in items[:5]],
        "retrieved_items": list(items[:5]),
        **extra,
    }


def rank_of(items: Sequence[Dict[str, Any]], gold: Sequence[str]) -> Any:
    gold_set = set(gold)
    return next((index + 1 for index, item in enumerate(items) if item.get("title") in gold_set), None)


async def main() -> None:
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager

    source = load_json(DATA_DIR / "rag_design_rationale.json")["samples"]
    samples = [sample for sample in source if sample["id"] in SELECTED_IDS]
    key, base_url, model = llm_settings()
    manager = MCPToolManager(key, base_url=base_url, model=model)
    tracker = tracked_llm_client(min_max_tokens=4096)
    manager._client = tracker
    semaphore = asyncio.Semaphore(3)

    os.environ.setdefault("PYTHONPYCACHEPREFIX", "/tmp/relaydesk-rag-examples-pycache")
    with tempfile.TemporaryDirectory(prefix="relaydesk-rag-examples-") as directory:
        kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=65535, chroma_path=directory)

        direct_rows: List[Dict[str, Any]] = []
        direct_latencies: List[float] = []
        direct_top10: Dict[str, List[Dict[str, Any]]] = {}
        for sample in samples:
            started = time.perf_counter()
            items = await kb.search_async(sample["query"], top_k=10)
            elapsed = (time.perf_counter() - started) * 1000
            direct_top10[sample["id"]] = items
            direct_latencies.append(elapsed)
            direct_rows.append(case_row(sample, items, latency_ms=round(elapsed, 3)))

        async def rewrite_one(sample: Dict[str, Any]) -> Tuple[str, List[str], float]:
            async with semaphore:
                started = time.perf_counter()
                queries = await manager.rewrite_query(sample["query"], n=3)
                return sample["id"], queries, (time.perf_counter() - started) * 1000

        before_rewrite = tracker.snapshot()
        rewrite_results = await asyncio.gather(*(rewrite_one(sample) for sample in samples))
        rewrite_usage = tracker.delta(before_rewrite)
        rewrites = {sample_id: queries for sample_id, queries, _ in rewrite_results}
        rewrite_ms = {sample_id: elapsed for sample_id, _, elapsed in rewrite_results}

        async def multi_recall(queries: Sequence[str]) -> Tuple[List[Dict[str, Any]], float]:
            started = time.perf_counter()
            groups = await asyncio.gather(*(kb.search_async(query, top_k=5) for query in queries))
            merged: Dict[Tuple[str, Any, str], Dict[str, Any]] = {}
            for group in groups:
                for item in group:
                    item_key = (item.get("title", ""), item.get("chunk", 0), item.get("content", ""))
                    if item_key not in merged or float(item.get("score", -999)) > float(merged[item_key].get("score", -999)):
                        merged[item_key] = item
            ordered = sorted(merged.values(), key=lambda item: float(item.get("score", -999)), reverse=True)
            return ordered, (time.perf_counter() - started) * 1000

        candidates: Dict[str, List[Dict[str, Any]]] = {}
        recall_ms: Dict[str, float] = {}
        rewrite_rows: List[Dict[str, Any]] = []
        rewrite_latencies: List[float] = []
        for sample in samples:
            items, elapsed = await multi_recall(rewrites[sample["id"]])
            candidates[sample["id"]] = items
            recall_ms[sample["id"]] = elapsed
            total = rewrite_ms[sample["id"]] + elapsed
            rewrite_latencies.append(total)
            rewrite_rows.append(case_row(sample, items, rewrite_queries=rewrites[sample["id"]], latency_ms=round(total, 3)))

        async def rerank_one(sample: Dict[str, Any], items: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], float]:
            async with semaphore:
                started = time.perf_counter()
                result = await manager._rerank(sample["query"], items, 5)
                return sample["id"], result, (time.perf_counter() - started) * 1000

        before_current = tracker.snapshot()
        current_results = await asyncio.gather(*(rerank_one(sample, candidates[sample["id"]]) for sample in samples))
        current_usage = tracker.delta(before_current)
        current_items = {sample_id: items for sample_id, items, _ in current_results}
        current_ms = {sample_id: elapsed for sample_id, _, elapsed in current_results}
        current_rows: List[Dict[str, Any]] = []
        current_latencies: List[float] = []
        for sample in samples:
            total = rewrite_ms[sample["id"]] + recall_ms[sample["id"]] + current_ms[sample["id"]]
            current_latencies.append(total)
            current_rows.append(case_row(
                sample,
                current_items[sample["id"]],
                rewrite_queries=rewrites[sample["id"]],
                candidate_titles_before_rerank=[item["title"] for item in candidates[sample["id"]]],
                latency_ms=round(total, 3),
            ))

        before_rerank_only = tracker.snapshot()
        rerank_only_results = await asyncio.gather(*(rerank_one(sample, direct_top10[sample["id"]]) for sample in samples))
        rerank_only_usage = tracker.delta(before_rerank_only)
        rerank_only_items = {sample_id: items for sample_id, items, _ in rerank_only_results}
        rerank_only_ms = {sample_id: elapsed for sample_id, _, elapsed in rerank_only_results}
        rerank_only_rows: List[Dict[str, Any]] = []
        rerank_only_latencies: List[float] = []
        for sample in samples:
            total = direct_rows[[row["id"] for row in direct_rows].index(sample["id"])]["latency_ms"] + rerank_only_ms[sample["id"]]
            rerank_only_latencies.append(total)
            rerank_only_rows.append(case_row(
                sample,
                rerank_only_items[sample["id"]],
                candidate_titles_before_rerank=[item["title"] for item in direct_top10[sample["id"]]],
                latency_ms=round(total, 3),
            ))

    by_id = {
        name: {row["id"]: row for row in rows}
        for name, rows in {
            "direct": direct_rows,
            "rewrite": rewrite_rows,
            "current": current_rows,
            "rerank_only": rerank_only_rows,
        }.items()
    }
    examples = []
    for sample in samples:
        sample_id = sample["id"]
        examples.append({
            "id": sample_id,
            "query": sample["query"],
            "gold_titles": sample["relevant_titles"],
            "rewrite_queries": rewrites[sample_id],
            "direct_top5": by_id["direct"][sample_id]["retrieved_titles"],
            "rewrite_top5": by_id["rewrite"][sample_id]["retrieved_titles"],
            "current_top5": by_id["current"][sample_id]["retrieved_titles"],
            "rerank_only_top5": by_id["rerank_only"][sample_id]["retrieved_titles"],
            "ranks": {
                "direct": rank_of(direct_top10[sample_id][:5], sample["relevant_titles"]),
                "rewrite": rank_of(candidates[sample_id][:5], sample["relevant_titles"]),
                "current": rank_of(current_items[sample_id], sample["relevant_titles"]),
                "rerank_only": rank_of(rerank_only_items[sample_id], sample["relevant_titles"]),
            },
        })

    payload = envelope(
        "EXECUTED",
        "Six fixed examples were rerun with a benchmark-only 4096-token floor for structured LLM output.",
        "Uses the production rewrite and rerank prompts and the same 20-document temporary ChromaDB corpus. This is a compatibility diagnostic, not the as-configured production result.",
        {
            "sample_ids": [sample["id"] for sample in samples],
            "production_max_tokens": 256,
            "diagnostic_min_max_tokens": 4096,
            "metrics": {
                "Direct Retrieval": retrieval_metrics(direct_rows, direct_latencies),
                "Query Rewrite": retrieval_metrics(rewrite_rows, rewrite_latencies),
                "Rewrite + Rerank": retrieval_metrics(current_rows, current_latencies),
                "Rerank Only": retrieval_metrics(rerank_only_rows, rerank_only_latencies),
            },
            "llm_usage": {
                "rewrite": rewrite_usage,
                "rewrite_rerank_stage": current_usage,
                "rerank_only": rerank_only_usage,
            },
            "examples": examples,
        },
    )
    output = DEFAULT_OUTPUT_DIR / "rag_compatibility_examples.json"
    write_json(output, payload)
    print(json.dumps({"status": "complete", "output": str(output), "examples": examples}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
