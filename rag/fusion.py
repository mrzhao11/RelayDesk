"""Reciprocal Rank Fusion for multi-query and multi-retriever results."""
import hashlib
from typing import Any, Dict, Iterable, List, Tuple


Ranking = Tuple[str, str, List[Dict[str, Any]]]


def reciprocal_rank_fusion(rankings: Iterable[Ranking], k: int = 60) -> List[Dict[str, Any]]:
    fused: Dict[str, Dict[str, Any]] = {}
    for retriever, query, items in rankings:
        for rank, item in enumerate(items, start=1):
            chunk_id = str(item.get("chunk_id") or _fallback_id(item))
            entry = fused.setdefault(chunk_id, {
                **item,
                "chunk_id": chunk_id,
                "rrf_score": 0.0,
                "matched_queries": [],
                "retrievers": [],
                "rank_evidence": [],
            })
            entry["rrf_score"] += 1.0 / (max(1, k) + rank)
            if query not in entry["matched_queries"]:
                entry["matched_queries"].append(query)
            if retriever not in entry["retrievers"]:
                entry["retrievers"].append(retriever)
            entry["rank_evidence"].append({"retriever": retriever, "query": query, "rank": rank})
            rank_key = f"{retriever}_rank"
            entry[rank_key] = min(rank, entry.get(rank_key, rank))
            if "score" in item:
                entry["dense_score"] = max(float(item["score"]), float(entry.get("dense_score", -1.0)))
            if "bm25_score" in item:
                entry["bm25_score"] = max(float(item["bm25_score"]), float(entry.get("bm25_score", 0.0)))

    for entry in fused.values():
        entry["rrf_score"] = round(entry["rrf_score"], 8)
        entry["score"] = round(float(entry.get("dense_score", entry.get("score", 0.0))), 4)
    return sorted(fused.values(), key=lambda item: (-item["rrf_score"], item["chunk_id"]))


def _fallback_id(item: Dict[str, Any]) -> str:
    raw = f"{item.get('source', '')}\n{item.get('section_path', '')}\n{item.get('content', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
