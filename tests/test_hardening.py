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
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel


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
            message="我想修改租户账号邮箱",
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

    def test_primary_mapping_covers_all_non_escalation_intents(self):
        technical_intents = {
            IntentCategory.TECHNICAL,
            IntentCategory.TECHNICAL_LOGIN,
            IntentCategory.TECHNICAL_CRASH,
            IntentCategory.ACCOUNT_SECURITY,
        }
        billing_intents = {
            IntentCategory.BILLING,
            IntentCategory.REFUND,
            IntentCategory.INVOICE,
            IntentCategory.PAYMENT_ISSUE,
        }
        escalation_intents = {
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        }
        orchestrator = self._orchestrator()
        for intent in IntentCategory:
            if intent in escalation_intents:
                continue
            expected = (
                AgentType.TECHNICAL if intent in technical_intents
                else AgentType.BILLING if intent in billing_intents
                else AgentType.GENERAL
            )
            with self.subTest(intent=intent.value):
                decision = orchestrator._route_decision(Request(
                    message="普通请求",
                    user_id="u1",
                    conv_id="c1",
                    intent=intent,
                ))
                self.assertEqual(decision.primary_agent, expected)
                self.assertEqual(decision.supporting_agents, [])

    def test_login_401_routes_to_technical_without_supporting(self):
        request = Request(
            message="登录一直报401",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            intent_confidence=0.87,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertEqual(decision.supporting_agents, [])
        self.assertEqual(decision.confidence, 0.87)
        self.assertIn("primary=technical from intent=technical_login", decision.reason)

    def test_refund_routes_to_billing_without_supporting(self):
        request = Request(
            message="我要退款",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.REFUND,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertEqual(decision.supporting_agents, [])

    def test_technical_primary_adds_billing_for_duplicate_charge(self):
        request = Request(
            message="登录报401，而且这个月被重复扣款",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            secondary_intents=[IntentCategory.PAYMENT_ISSUE],
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertEqual(decision.supporting_agents, [AgentType.BILLING])
        self.assertIn("supporting=billing from secondary_intent=payment_issue", decision.reason)

    def test_billing_primary_adds_technical_for_500(self):
        request = Request(
            message="这个月重复扣款，而且登录也报500",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.PAYMENT_ISSUE,
            secondary_intents=[IntentCategory.TECHNICAL_CRASH],
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertEqual(decision.supporting_agents, [AgentType.TECHNICAL])
        self.assertIn("supporting=technical from secondary_intent=technical_crash", decision.reason)

    def test_broad_plan_word_does_not_trigger_billing_supporting(self):
        request = Request(
            message="我想了解一下套餐怎么用",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.QUERY,
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.GENERAL)
        self.assertEqual(decision.supporting_agents, [])

    def test_same_agent_secondary_does_not_trigger_supporting(self):
        request = Request(
            message="登录失败后页面也崩溃",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            secondary_intents=[IntentCategory.TECHNICAL_CRASH],
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertEqual(decision.supporting_agents, [])

    def test_entities_are_context_only_and_do_not_trigger_supporting(self):
        request = Request(
            message="登录报401，涉及99元",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            entities={"amount": ["99元"], "error_code": ["401"]},
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertEqual(decision.supporting_agents, [])

    def test_supporting_agent_is_deduplicated_after_intent_mapping(self):
        request = Request(
            message="登录报401，而且还要处理扣款和发票",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            secondary_intents=[IntentCategory.PAYMENT_ISSUE, IntentCategory.INVOICE],
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.supporting_agents, [AgentType.BILLING])
        self.assertIn("secondary_intent=payment_issue,invoice", decision.reason)

    def test_invalid_general_and_escalation_secondaries_are_filtered(self):
        request = Request(
            message="登录报401",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_LOGIN,
            secondary_intents=[
                IntentCategory.QUERY,
                IntentCategory.HUMAN_HANDOFF,
                "invalid",  # type: ignore[list-item]
                None,  # type: ignore[list-item]
            ],
        )
        decision = self._orchestrator()._route_decision(request)
        self.assertEqual(decision.supporting_agents, [])

    def test_regex_entity_extraction_is_retained(self):
        recognizer = IntentRecognizer(api_key="test", base_url="http://localhost")
        entities = recognizer._extract_entities("订单号 RD-1234，金额199元，登录报401")
        self.assertEqual(entities["order_id"], ["RD-1234"])
        self.assertEqual(entities["amount"], ["199元"])
        self.assertEqual(entities["error_code"], ["401"])

    def test_escalation_and_critical_routing_are_unchanged(self):
        escalation = Request(
            message="请帮我转人工",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.HUMAN_HANDOFF,
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=0.9,
        )
        critical = Request(
            message="问题非常紧急",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL,
            urgency=UrgencyLevel.CRITICAL,
        )
        escalation_decision = self._orchestrator()._route_decision(escalation)
        critical_decision = self._orchestrator()._route_decision(critical)
        self.assertEqual(escalation_decision.primary_agent, AgentType.ESCALATION)
        self.assertEqual(critical_decision.primary_agent, AgentType.ESCALATION)
        self.assertEqual(escalation_decision.supporting_agents, [])
        self.assertEqual(critical_decision.supporting_agents, [])


class IntentLlmParsingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _recognizer_with_response(raw: str) -> IntentRecognizer:
        class FakeMessages:
            async def create(self, **_kwargs):
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=raw)])

        recognizer = IntentRecognizer(api_key="test", base_url="http://localhost")
        recognizer.client = SimpleNamespace(messages=FakeMessages())
        return recognizer

    async def test_valid_primary_and_secondary_are_parsed(self):
        recognizer = self._recognizer_with_response(
            '{"primary_intent":"technical_login","secondary_intents":["payment_issue"],'
            '"confidence":0.93,"reasoning":"两个独立诉求"}'
        )
        result = await recognizer._llm_recognize("登录报401而且重复扣款", None)
        self.assertEqual(result["intent"], IntentCategory.TECHNICAL_LOGIN)
        self.assertEqual(result["secondary_intents"], [IntentCategory.PAYMENT_ISSUE])

    async def test_empty_secondary_is_preserved(self):
        recognizer = self._recognizer_with_response(
            '{"primary_intent":"refund","secondary_intents":[],"confidence":0.9}'
        )
        result = await recognizer._llm_recognize("我要退款", None)
        self.assertEqual(result["secondary_intents"], [])

    async def test_invalid_duplicate_and_primary_secondary_are_filtered(self):
        recognizer = self._recognizer_with_response(
            '{"primary_intent":"technical_login","secondary_intents":'
            '["technical_login","payment_issue","invalid","payment_issue","invoice","refund"]}'
        )
        result = await recognizer._llm_recognize("复合请求", None)
        self.assertEqual(
            result["secondary_intents"],
            [IntentCategory.PAYMENT_ISSUE, IntentCategory.INVOICE],
        )

    async def test_invalid_primary_uses_other_while_valid_secondary_remains_parseable(self):
        recognizer = self._recognizer_with_response(
            '{"primary_intent":"invalid","secondary_intents":["invoice"]}'
        )
        result = await recognizer._llm_recognize("未知请求", None)
        self.assertEqual(result["intent"], IntentCategory.OTHER)
        self.assertEqual(result["secondary_intents"], [IntentCategory.INVOICE])

    async def test_malformed_json_uses_existing_failure_fallback(self):
        recognizer = self._recognizer_with_response("not-json")
        result = await recognizer._llm_recognize("登录报401", None)
        self.assertTrue(result["failed"])
        self.assertEqual(result["intent"], IntentCategory.OTHER)
        self.assertEqual(result["secondary_intents"], [])


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
