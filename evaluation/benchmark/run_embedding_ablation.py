#!/usr/bin/env python3
"""Compare the legacy and current local embedding models on RelayDesk data."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.benchmark.run_design_rationale import (
    DATA_DIR,
    DEFAULT_OUTPUT_DIR,
    envelope,
    load_json,
    macro_classification_metrics,
    retrieval_metrics,
    write_json,
)


MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
MODEL_SOURCE = os.getenv("EMBEDDING_MODEL_SOURCE", MODEL_NAME)
QUERY_INSTRUCTION = (
    "Given an enterprise service desk request, retrieve the most relevant "
    "policy or troubleshooting passage that answers the request"
)


def rank_of(titles: Sequence[str], relevant: Sequence[str]) -> Any:
    relevant_set = set(relevant)
    return next((index + 1 for index, title in enumerate(titles) if title in relevant_set), None)


async def run_variant(kb: Any, samples: Sequence[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[float]]:
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for sample in samples:
        started = time.perf_counter()
        items = await kb.search_async(sample["query"], top_k=5)
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        rows.append({
            "id": sample["id"],
            "query": sample["query"],
            "query_type": sample["query_type"],
            "relevant_titles": sample["relevant_titles"],
            "retrieved_titles": [item["title"] for item in items],
            "retrieved_items": items,
            "latency_ms": round(latency, 3),
        })
    return rows, latencies


async def main() -> None:
    from core.embedding_provider import LocalSentenceTransformerEmbedding
    from core.intent_recognizer import IntentRecognizer
    from mcp.knowledge_base import KnowledgeBase

    samples = load_json(DATA_DIR / "rag_design_rationale.json")["samples"]
    embedding_function = LocalSentenceTransformerEmbedding(
        model_name=MODEL_SOURCE,
        device="cpu",
        query_prompt_name="query",
        query_instruction=QUERY_INSTRUCTION,
        normalize_embeddings=True,
    )

    previous = load_json(DEFAULT_OUTPUT_DIR / "rag_ablation.json")
    legacy_variant = previous["results"]["variants"]["V0 Direct Retrieval"]
    legacy_rows = legacy_variant["per_sample_results"]

    with tempfile.TemporaryDirectory(prefix="relaydesk-qwen3-embedding-") as candidate_dir:
        candidate_kb = KnowledgeBase(
            chroma_host="127.0.0.1",
            chroma_port=65535,
            chroma_path=candidate_dir,
            embedding_function=embedding_function,
            embedding_model=MODEL_NAME,
            collection_name="knowledge-base-qwen3-ablation",
        )
        candidate_rows, candidate_latencies = await run_variant(candidate_kb, samples)

    legacy_by_id = {row["id"]: row for row in legacy_rows}
    candidate_by_id = {row["id"]: row for row in candidate_rows}
    examples = []
    improved = 0
    regressed = 0
    unchanged = 0
    for sample in samples:
        legacy = legacy_by_id[sample["id"]]
        candidate = candidate_by_id[sample["id"]]
        legacy_rank = rank_of(legacy["retrieved_titles"], sample["relevant_titles"])
        candidate_rank = rank_of(candidate["retrieved_titles"], sample["relevant_titles"])
        legacy_score = 1.0 / legacy_rank if legacy_rank else 0.0
        candidate_score = 1.0 / candidate_rank if candidate_rank else 0.0
        outcome = "improved" if candidate_score > legacy_score else "regressed" if candidate_score < legacy_score else "unchanged"
        if outcome == "improved":
            improved += 1
        elif outcome == "regressed":
            regressed += 1
        else:
            unchanged += 1
        examples.append({
            "id": sample["id"],
            "query": sample["query"],
            "relevant_titles": sample["relevant_titles"],
            "legacy_rank": legacy_rank,
            "candidate_rank": candidate_rank,
            "outcome": outcome,
            "legacy_top5": legacy["retrieved_titles"],
            "candidate_top5": candidate["retrieved_titles"],
            "candidate_top5_scores": [item["score"] for item in candidate["retrieved_items"]],
        })

    intent_samples = [
        sample for sample in load_json(DATA_DIR / "intent_design_rationale.json")["samples"]
        if not sample.get("ambiguous")
    ]
    recognizer = IntentRecognizer(
        api_key="embedding-ablation-placeholder",
        base_url="https://example.test/anthropic",
        embedding_function=embedding_function,
    )
    intent_rows = []
    intent_examples = []
    for sample in intent_samples:
        prediction = await recognizer._embedding_recognize(sample["message"])
        predicted = prediction["intent"].value
        intent_rows.append((sample["id"], sample["gold_intent"], predicted))
        intent_examples.append({
            "id": sample["id"],
            "message": sample["message"],
            "gold_intent": sample["gold_intent"],
            "predicted_intent": predicted,
            "confidence": round(float(prediction["confidence"]), 4),
        })

    previous_intent = load_json(DEFAULT_OUTPUT_DIR / "intent_ablation.json")
    legacy_intent_metrics = previous_intent["results"]["supplementary_component_diagnostics"]["local_embedding_only_diagnostics"]

    payload = envelope(
        "EXECUTED",
        "The legacy Chroma default embedding and Qwen3 Embedding were compared on the same fixed RelayDesk dataset.",
        "The legacy per-sample baseline is reused from rag_ablation.json. Qwen3 uses the official instruction format with an enterprise service desk task description, an isolated temporary ChromaDB collection, the same 20 demonstration documents, and direct Top-5 retrieval without query rewrite or reranking.",
        {
            "legacy_model": "Chroma default all-MiniLM-L6-v2",
            "candidate_model": MODEL_NAME,
            "query_instruction": QUERY_INSTRUCTION,
            "metrics": {
                "legacy": legacy_variant["metrics"],
                "candidate": retrieval_metrics(candidate_rows, candidate_latencies),
            },
            "outcome_counts": {
                "improved": improved,
                "regressed": regressed,
                "unchanged": unchanged,
            },
            "examples": examples,
            "intent_embedding_only": {
                "legacy_character_ngram": legacy_intent_metrics,
                "candidate": macro_classification_metrics(intent_rows),
                "per_sample_results": intent_examples,
            },
        },
    )
    output = DEFAULT_OUTPUT_DIR / "embedding_ablation.json"
    write_json(output, payload)
    print(json.dumps({
        "status": "complete",
        "output": str(output),
        "metrics": payload["results"]["metrics"],
        "outcome_counts": payload["results"]["outcome_counts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
