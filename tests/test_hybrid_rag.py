import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager, Tool
from rag.bm25_retriever import BM25Retriever, tokenize
from rag.chunker import StructuredChunker
from rag.fusion import reciprocal_rank_fusion


def test_chunker_splits_markdown_headings_and_keeps_metadata():
    chunker = StructuredChunker(chunk_size=140, overlap=20)
    chunks = chunker.chunk_document({
        "source": "docs://technical",
        "title": "Technical Support",
        "content": "# Technical Support\nIntro.\n\n## SSO\nLogin failure guide.\n\n### OAuth\nToken refresh guide.",
    })
    assert [chunk["section_path"] for chunk in chunks] == [
        "Technical Support", "Technical Support / SSO", "Technical Support / SSO / OAuth"
    ]
    assert all(chunk["source"] == "docs://technical" for chunk in chunks)
    assert all(chunk["total_chunks"] == 3 for chunk in chunks)
    assert all(chunk["content"] for chunk in chunks)


def test_chunker_accumulates_paragraphs_and_uses_sentence_fallback():
    chunker = StructuredChunker(chunk_size=120, overlap=20)
    paragraph = "第一句用于解释登录问题。第二句用于说明排查方式。第三句继续提供足够长的安全处理信息。" * 4
    chunks = chunker.chunk_document({
        "title": "登录说明",
        "content": f"短段落一。\n\n短段落二。\n\n{paragraph}",
    })
    assert chunks[0]["content"].startswith("短段落一。\n\n短段落二。")
    assert len(chunks) >= 2
    assert all(0 < len(chunk["content"]) <= 120 for chunk in chunks)


def test_chunker_overlap_and_stable_ids():
    document = {
        "title": "API Guide",
        "source": "docs://api-guide",
        "content": "第一段提供足够多的信息用于形成第一个知识片段。" * 4 + "\n\n" + "第二段介绍 Webhook 403 的排查步骤。" * 4,
    }
    chunker = StructuredChunker(chunk_size=120, overlap=30)
    first = chunker.chunk_document(document)
    second = chunker.chunk_document(document)
    assert [chunk["chunk_id"] for chunk in first] == [chunk["chunk_id"] for chunk in second]
    assert len(first) >= 2
    assert any(first[index]["content"][-12:] in first[index + 1]["content"] for index in range(len(first) - 1))
    assert [chunk["chunk_index"] for chunk in first] == list(range(len(first)))


def test_chunker_hard_split_keeps_configured_overlap():
    chunks = StructuredChunker(chunk_size=100, overlap=20).chunk_document({
        "title": "Long Token",
        "content": "X" * 230,
    })
    assert len(chunks) == 3
    assert chunks[0]["content"][-20:] == chunks[1]["content"][:20]
    assert chunks[1]["content"][-20:] == chunks[2]["content"][:20]
    assert all(len(chunk["content"]) <= 100 for chunk in chunks)


def test_hard_set_gold_chunk_ids_match_current_chunker():
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "evaluation/datasets/hybrid_retrieval_hard_cases.json").read_text())
    chunks = [
        chunk
        for document in payload["documents"]
        for chunk in StructuredChunker(500, 80).chunk_document(document)
    ]
    for sample in payload["samples"]:
        expected = [
            chunk["chunk_id"] for chunk in chunks
            if chunk["source"] == sample["gold_source"]
            and chunk["section_path"] == sample["gold_section_path"]
            and chunk["chunk_index"] == sample["gold_chunk_index"]
        ]
        assert sample["gold_chunk_ids"] == expected


def _chunks():
    return [
        {"chunk_id": "401", "title": "401", "section_path": "Auth / 401", "content": "API 返回 401，检查 token。"},
        {"chunk_id": "403", "title": "Webhook 403", "section_path": "Webhook / 403", "content": "Webhook 返回 403，检查签名。"},
        {"chunk_id": "500", "title": "Server", "section_path": "API / 500", "content": "API 返回 500，记录 request_id。"},
        {"chunk_id": "oauth", "title": "OAuth", "section_path": "SSO / OAuth", "content": "oauth2 token 无法刷新，检查 client_id。"},
    ]


def test_bm25_preserves_error_codes_and_chinese_english_terms():
    assert "e_auth_102" in tokenize("登录失败 E_AUTH_102")
    assert "401" in tokenize("接口返回401")
    retriever = BM25Retriever()
    retriever.build(_chunks())
    assert retriever.search("接口返回 401", 1)[0]["chunk_id"] == "401"
    assert retriever.search("Webhook 返回 403", 1)[0]["chunk_id"] == "403"
    assert retriever.search("OAuth token 无法刷新", 1)[0]["chunk_id"] == "oauth"
    assert retriever.search("Webhook 返回 403", 4) == retriever.search("Webhook 返回 403", 4)


def test_rrf_fuses_ranks_deduplicates_and_is_deterministic():
    dense = [{"chunk_id": "a", "content": "A", "score": 0.8}, {"chunk_id": "b", "content": "B", "score": 0.7}]
    bm25 = [{"chunk_id": "b", "content": "B", "bm25_score": 4.0}, {"chunk_id": "c", "content": "C", "bm25_score": 3.0}]
    fused = reciprocal_rank_fusion([("dense", "q", dense), ("bm25", "q", bm25)], k=60)
    assert [item["chunk_id"] for item in fused] == ["b", "a", "c"]
    assert fused[0]["dense_rank"] == 2
    assert fused[0]["bm25_rank"] == 1
    assert fused[0]["retrievers"] == ["dense", "bm25"]
    assert fused == reciprocal_rank_fusion([("dense", "q", dense), ("bm25", "q", bm25)], k=60)
    assert [item["chunk_id"] for item in reciprocal_rank_fusion([("dense", "q", dense)])] == ["a", "b"]


def test_hybrid_retrieval_merges_dense_bm25_and_multiple_queries():
    kb = object.__new__(KnowledgeBase)
    kb._rrf_k = 60
    kb.search = lambda query, top_k: [
        {"chunk_id": "dense", "content": f"dense:{query}", "score": 0.8},
        {"chunk_id": "shared", "content": "shared", "score": 0.7},
    ][:top_k]
    kb.search_bm25 = lambda query, top_k: [
        {"chunk_id": "shared", "content": "shared", "bm25_score": 5.0},
        {"chunk_id": f"lexical-{query}", "content": query, "bm25_score": 4.0},
    ][:top_k]
    results = kb.retrieve("401", top_k=5, queries=["401", "auth token"], use_bm25=True)
    assert results[0]["chunk_id"] == "shared"
    assert len({item["chunk_id"] for item in results}) == len(results)
    assert set(results[0]["matched_queries"]) == {"401", "auth token"}


def test_rewrite_aggregation_and_rerank_failure_fall_back():
    observed = {}

    async def handler(params, _context):
        observed.update(params)
        return [
            {"chunk_id": str(index), "content": f"candidate {index}", "score": 0.8 - index / 100}
            for index in range(10)
        ]

    manager = MCPToolManager(api_key="test", base_url="http://localhost")
    manager.rewrite_query = lambda _query, n=3: asyncio.sleep(0, result=["original", "rewrite one"])
    manager._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: asyncio.sleep(
        0, result=SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")])
    )))
    manager.register(Tool(
        name="knowledge_search",
        description="test",
        handler=handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"}, "top_k": {"type": "integer"},
                "mode": {"type": "string"}, "queries": {"type": "array"},
                "candidate_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        supports_rerank=True,
    ))
    result = asyncio.run(manager.search_with_rewrite(
        "knowledge_search", "original", top_k=3, retrieval_mode="hybrid_rewrite_rerank"
    ))
    assert observed["queries"] == ["original", "rewrite one"]
    assert observed["mode"] == "hybrid"
    assert [item["chunk_id"] for item in result.data] == ["0", "1", "2"]
