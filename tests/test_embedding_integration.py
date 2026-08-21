import asyncio

from core.intent_recognizer import IntentRecognizer
from agents.agent_orchestrator import AgentOrchestrator
from mcp.knowledge_base import KnowledgeBase


class FakeEmbeddingFunction:
    """Small deterministic stand-in; production uses SentenceTransformer."""

    def __init__(self):
        self.calls = 0

    def __call__(self, input):
        self.calls += 1
        vectors = []
        for text in input:
            lowered = text.lower()
            vectors.append([
                float("401" in lowered or "登录" in lowered),
                float("发票" in lowered),
                float("退款" in lowered or "扣款" in lowered),
                1.0,
            ])
        return vectors


def test_third_party_llm_keeps_injected_embedding_enabled():
    embedding = FakeEmbeddingFunction()
    recognizer = IntentRecognizer(
        api_key="test-key",
        base_url="https://example.test/anthropic",
        embedding_function=embedding,
    )

    vector = asyncio.run(recognizer._embed_text("登录报 401"))

    assert recognizer._embedding_enabled is True
    assert vector == [1.0, 0.0, 0.0, 1.0]
    assert embedding.calls == 1


def test_orchestrator_passes_embedding_to_real_chat_recognizer():
    embedding = FakeEmbeddingFunction()
    orchestrator = AgentOrchestrator(
        api_key="test-key",
        base_url="https://example.test/anthropic",
        embedding_function=embedding,
    )

    assert orchestrator._intent_recognizer._embedding_enabled is True
    assert orchestrator._intent_recognizer._embedding_function is embedding


def test_knowledge_base_uses_injected_embedding_for_documents_and_queries(tmp_path):
    embedding = FakeEmbeddingFunction()
    kb = KnowledgeBase(
        chroma_host="127.0.0.1",
        chroma_port=65535,
        chroma_path=str(tmp_path),
        embedding_function=embedding,
        embedding_model="test-embedding",
        collection_name="knowledge-base-test-embedding",
    )
    calls_after_import = embedding.calls

    results = kb.search("登录报 401", top_k=3)

    assert kb.doc_count > 0
    assert calls_after_import > 0
    assert embedding.calls > calls_after_import
    assert len(results) == 3
