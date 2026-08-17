import asyncio
import os
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

import api.main as api_main
from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    Request,
    RoutingDecision,
)
from core.intent_recognizer import IntentCategory


class RoutingTests(unittest.TestCase):
    @staticmethod
    def _orchestrator() -> AgentOrchestrator:
        orchestrator = object.__new__(AgentOrchestrator)
        orchestrator._llm_timeout_s = 1.0
        orchestrator._pool = {
            AgentType.GENERAL: [object()],
            AgentType.TECHNICAL: [object()],
            AgentType.BILLING: [object()],
        }
        return orchestrator

    def test_account_profile_routes_to_general(self):
        request = Request(
            message="我想修改企业账号邮箱",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.ACCOUNT,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.GENERAL)

    def test_account_security_routes_to_technical(self):
        request = Request(
            message="账号出现异常登录",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.ACCOUNT_SECURITY,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)

    def test_composite_request_keeps_technical_and_billing(self):
        request = Request(
            message="登录报401，而且还被重复扣款",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.PAYMENT_ISSUE,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertIn(AgentType.TECHNICAL, decision.supporting_agents)


class KnowledgeContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_manager = api_main._tool_manager
        self.previous_score = os.environ.get("RAG_MIN_SCORE")
        os.environ["RAG_MIN_SCORE"] = "0.20"
        os.environ["RAG_TIMEOUT_SECONDS"] = "1"

    async def asyncTearDown(self):
        api_main._tool_manager = self.previous_manager
        if self.previous_score is None:
            os.environ.pop("RAG_MIN_SCORE", None)
        else:
            os.environ["RAG_MIN_SCORE"] = self.previous_score

    @staticmethod
    def _manager(items):
        class FakeManager:
            async def search_with_rewrite(self, *_args, **_kwargs):
                return SimpleNamespace(success=True, data=items, reranked=True)

        return FakeManager()

    async def test_low_score_is_not_a_knowledge_hit(self):
        api_main._tool_manager = self._manager([
            {"title": "无关内容", "content": "无关知识", "score": 0.05},
        ])
        context, used = await api_main._build_knowledge_context("登录报401", intent="technical")
        self.assertEqual(context, "")
        self.assertFalse(used)

    async def test_fallback_is_not_a_knowledge_hit(self):
        api_main._tool_manager = self._manager([
            {"title": "降级", "content": "服务不可用", "score": 1.0, "fallback": True},
        ])
        context, used = await api_main._build_knowledge_context("登录报401", intent="technical")
        self.assertEqual(context, "")
        self.assertFalse(used)

    async def test_relevant_result_is_injected(self):
        api_main._tool_manager = self._manager([
            {"title": "401 排查", "content": "检查登录凭据和令牌。", "score": 0.75},
        ])
        context, used = await api_main._build_knowledge_context("登录报401", intent="technical")
        self.assertTrue(used)
        self.assertIn("401 排查", context)


class CollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_results_are_synthesized(self):
        orchestrator = object.__new__(AgentOrchestrator)
        orchestrator._llm_timeout_s = 1.0

        async def fake_execute(_request, agent_type):
            content = "先检查登录令牌。" if agent_type == AgentType.TECHNICAL else "准备两笔脱敏交易号。"
            return AgentResponse(agent_type=agent_type, content=content, success=True)

        class FakePrimary:
            async def synthesize_collaboration(self, _request, _responses):
                return "先排查登录令牌，再准备两笔脱敏交易号交由人工财务核验。"

        orchestrator._execute = fake_execute
        orchestrator._best_agent = lambda _agent_type: FakePrimary()
        request = Request("登录报401，而且还被重复扣款", "u1", "c1")
        decision = RoutingDecision(
            primary_agent=AgentType.BILLING,
            supporting_agents=[AgentType.TECHNICAL],
        )

        result = await orchestrator.run_parallel(request, decision)
        self.assertNotIn("[billing", result.response)
        self.assertIn("先排查登录令牌", result.response)
        self.assertEqual(set(result.agent_types), {AgentType.BILLING, AgentType.TECHNICAL})


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("RELAYDESK_ADMIN_KEY")

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("RELAYDESK_ADMIN_KEY", None)
        else:
            os.environ["RELAYDESK_ADMIN_KEY"] = self.previous

    def test_admin_auth_fails_closed_without_configuration(self):
        os.environ.pop("RELAYDESK_ADMIN_KEY", None)
        with self.assertRaises(HTTPException) as context:
            api_main._require_admin_key(None)
        self.assertEqual(context.exception.status_code, 503)

    def test_admin_auth_accepts_only_matching_key(self):
        os.environ["RELAYDESK_ADMIN_KEY"] = "test-secret"
        with self.assertRaises(HTTPException) as context:
            api_main._require_admin_key("wrong")
        self.assertEqual(context.exception.status_code, 401)
        self.assertIsNone(api_main._require_admin_key("test-secret"))


if __name__ == "__main__":
    unittest.main()
