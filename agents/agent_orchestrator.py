"""
亮点：多 Agent 路由与编排

核心问题：多 Agent 情况下如何做 Routing？

路由策略（三层决策）：
  1. 意图路由 —— 根据 IntentCategory 直接映射到专属 Agent
  2. 性能路由 —— 同类 Agent 有多个时，选成功率最高、延迟最低的
  3. 降级路由 —— 专属 Agent 不可用时，自动降级到 GeneralAgent

并行协作：
  - 复杂问题（如"技术问题 + 账单问题"）可同时派发给多个 Agent
  - 结果由 Orchestrator 合并后返回

升级机制：
  - Agent 置信度低于阈值 → 自动升级到更高级 Agent 或转人工
"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL   = "general"    # 通用客服
    TECHNICAL = "technical"  # 技术支持
    BILLING   = "billing"    # 账单/退款
    ESCALATION = "escalation" # 人工升级（占位）


# Supporting Agent 只由这些集中维护的强证据触发。这里有意不包含
# “套餐”“帮助”“问题”等宽泛词，以高 Precision 为优先目标。
DOMAIN_STRONG_SIGNALS: Dict[AgentType, tuple[tuple[str, str], ...]] = {
    AgentType.TECHNICAL: (
        ("401", r"(?<!\d)401(?!\d)"),
        ("403", r"(?<!\d)403(?!\d)"),
        ("500", r"(?<!\d)500(?!\d)"),
        ("error", r"\berror\b"),
        ("crash", r"\bcrash(?:ed|es|ing)?\b"),
        ("崩溃", r"崩溃"),
        ("登录失败", r"登录失败"),
        ("无法登录", r"无法登录"),
        ("验证码异常", r"验证码.{0,6}(?:异常|失败|收不到|无法获取)"),
    ),
    AgentType.BILLING: (
        ("退款", r"退款"),
        ("重复扣款", r"重复扣款"),
        ("多扣", r"多扣"),
        ("重复交易", r"(?:扣了|收了|出现)(?:两次|两遍)|两笔(?:相同|一样)(?:扣款|交易)"),
        ("陌生扣款", r"陌生扣款"),
        ("支付失败", r"支付失败"),
        ("发票", r"发票"),
        ("账单异常", r"账单.{0,6}(?:异常|有误|不对)"),
        ("refund", r"\brefund\b"),
        ("invoice", r"\binvoice\b"),
    ),
}

DOMAIN_ENTITY_SIGNALS: Dict[AgentType, tuple[str, ...]] = {
    AgentType.TECHNICAL: ("error_code",),
    AgentType.BILLING: ("amount",),
}


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和同类型多实例择优使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """实例级评分：只在同类型有多个 Agent 实例时用于择优。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0


@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。

    confidence 表示 Primary 路由沿用的 Intent 置信度，不是独立计算的
    路由概率；Supporting 由可解释的强证据触发，不产生伪精确分数。
    """
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用和统计。"""

    agent_type: AgentType
    system_prompt: str

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        skill_manager: Optional[Any] = None,
        timeout_s: float = 30.0,
    ):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        self._timeout_s = max(1.0, timeout_s)
        self.stats   = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await asyncio.wait_for(self._call_llm(req), timeout=self._timeout_s)
            if not content.strip():
                raise RuntimeError("模型返回了空响应")
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            escalate = self._needs_escalation(content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
            )
        except asyncio.TimeoutError:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理超时: {self._timeout_s}s")
            return AgentResponse(
                agent_type=self.agent_type,
                content="模型服务响应超时，未执行任何后台操作。请稍后重试或申请人工协助。",
                success=False,
                latency_ms=ms,
                escalate=True,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content="模型服务暂时不可用，未执行任何后台操作。请稍后重试或申请人工协助。",
                success=False,
                latency_ms=ms,
                escalate=True,
            )

    async def _call_llm(self, req: Request) -> str:
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{_clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        messages.append({"role": "user", "content": _clean(req.message)})

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self._build_system_prompt(req),
            messages=messages,
        )
        return extract_text_content(resp.content)

    async def synthesize_collaboration(
        self,
        req: Request,
        responses: List[AgentResponse],
    ) -> str:
        """由主 Agent 把多个专业回复整合为一个一致、可执行的最终答复。"""
        source_text = "\n\n".join(
            f"[{response.agent_type.value}]\n{response.content}"
            for response in responses
        )
        prompt = (
            f"用户原始请求：{req.message}\n\n"
            f"专业 Agent 的候选回复：\n{source_text}\n\n"
            "请整合为一个直接面向用户的中文答复。去除重复内容，明确处理顺序，并保留技术、费用等必要边界。"
            "只能依据候选回复，不得编造制度、账户状态、财务记录或已执行的后台操作。"
            "不要展示内部 Agent 名称、路由分数或候选回复标签。"
        )
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=(
                f"{self._build_system_prompt(req)}\n\n"
                "你还负责整合多专业团队意见。输出必须一致、精炼、分步骤且不夸大系统能力。"
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        content = extract_text_content(resp.content).strip()
        if not content:
            raise RuntimeError("协作整合返回了空响应")
        return content

    def _build_system_prompt(self, req: Request) -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。"""
        if self._skill_manager is None:
            return self.system_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return self.system_prompt
        return f"{self.system_prompt}\n\n[动态 Skills]\n{skill_prompt}"

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测）。"""
        keywords = ["转人工", "人工客服", "escalate", "specialist", "无法处理"]
        return any(kw in content for kw in keywords)


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    system_prompt = (
        "你是RelayDesk企业级SaaS统一客户服务平台的综合服务协调Agent。"
        "负责处理产品咨询、使用流程说明、信息澄清和跨领域客户服务分流。"
        "如果信息不足，应先确认租户、Workspace和使用场景；不得编造产品能力、账户状态或后台操作结果。"
        "超出能力范围时建议转交对应专业团队或人工服务。"
    )


class TechnicalAgent(BaseAgent):
    agent_type    = AgentType.TECHNICAL
    system_prompt = (
        "你是RelayDesk企业级SaaS统一客户服务平台的技术支持Agent。"
        "负责处理租户账号与SSO登录、Workspace权限、错误码、API/SDK/Webhook和客户端故障。"
        "请提供清晰、低风险、可逆的排查步骤。"
        "涉及管理员权限、数据删除、安全风险或后台操作时，应明确建议转人工处理，不得声称已经执行操作。"
    )


class BillingAgent(BaseAgent):
    agent_type    = AgentType.BILLING
    system_prompt = (
        "你是RelayDesk企业级SaaS统一客户服务平台的费用与结算Agent。"
        "负责处理套餐与订阅、账单、发票、退款、支付异常和费用规则咨询。"
        "请区分平台通用规则与真实租户账户结果，不得编造账单、退款状态或财务记录。"
        "涉及真实资金操作或费用争议时，应说明需要人工核验。"
    )


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    路由逻辑（三层）：
      1. 意图 → Agent 类型映射
      2. 同类多实例时按 routing_score() 选最优
      3. 专属 Agent 失败时降级到 GeneralAgent
    """

    # 意图 → Agent 类型的静态映射（路由表）
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.TECHNICAL:  AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_LOGIN: AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_CRASH: AgentType.TECHNICAL,
        IntentCategory.BILLING:    AgentType.BILLING,
        IntentCategory.REFUND:     AgentType.BILLING,
        IntentCategory.INVOICE:    AgentType.BILLING,
        IntentCategory.PAYMENT_ISSUE: AgentType.BILLING,
        IntentCategory.ACCOUNT:    AgentType.GENERAL,
        IntentCategory.ACCOUNT_SECURITY: AgentType.TECHNICAL,
        IntentCategory.ESCALATION: AgentType.ESCALATION,
        IntentCategory.HUMAN_HANDOFF: AgentType.ESCALATION,
        # 其余意图 → GENERAL（默认）
    }

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
        embedding_function: Optional[Any] = None,
        llm_timeout_s: float = 30.0,
        llm_max_retries: int = 1,
    ):
        self._llm_timeout_s = max(1.0, llm_timeout_s)
        kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "timeout": self._llm_timeout_s,
            "max_retries": max(0, llm_max_retries),
        }
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(
            api_key=api_key,
            base_url=base_url,
            model=model,
            embedding_function=embedding_function,
        )
        self._skill_manager = skill_manager

        # Agent 池：每种类型可有多个实例（水平扩展）
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:   [GeneralAgent(client, model, skill_manager, self._llm_timeout_s)],
            AgentType.TECHNICAL: [TechnicalAgent(client, model, skill_manager, self._llm_timeout_s)],
            AgentType.BILLING:   [BillingAgent(client, model, skill_manager, self._llm_timeout_s)],
        }

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await asyncio.wait_for(
            self._intent_recognizer.recognize(message, history=history),
            timeout=self._llm_timeout_s,
        )

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 路由选 Agent → 执行 → 检查升级 → 返回结果
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self.recognize_intent(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        if self._needs_clarification(req):
            return OrchestratorResult(
                request_id=req.request_id,
                response="我还不能确定您要处理的是哪类问题。请补充一下是通用服务、账户问题、技术故障、费用结算，还是需要人工协助？",
                agent_type=AgentType.GENERAL,
                intent=req.intent,
                escalated=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.GENERAL],
                primary_agent=AgentType.GENERAL,
                routing_reason="低置信度 OTHER 意图，先澄清用户需求",
                routing_confidence=req.intent_confidence,
            )

        # 复杂问题自动并行协作，例如同一句同时涉及登录故障和扣款/退款。
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 2. 执行主 Agent（含降级）
        response = await self._execute(req, decision.primary_agent)

        # 4. 升级检查
        escalated = False
        if response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        ):
            escalated = True
            logger.warning(f"请求 {req.request_id} 触发升级: urgency={req.urgency}")
            # 这里只返回升级标记；未来接入真实 CRM/工单系统后才能创建工单或通知人工。

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        并行派发给多个 Agent，合并结果。
        适用于复杂问题（如同时涉及技术和账单）。
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        successful = [
            response
            for response in responses
            if isinstance(response, AgentResponse) and response.success
        ]
        all_failed = not successful
        if len(successful) == 1:
            combined = successful[0].content
        elif successful:
            primary = self._best_agent(decision.primary_agent) or self._best_agent(AgentType.GENERAL)
            try:
                if primary is None:
                    raise RuntimeError("没有可用的主 Agent 负责整合")
                combined = await asyncio.wait_for(
                    primary.synthesize_collaboration(req, successful),
                    timeout=self._llm_timeout_s,
                )
            except Exception as ex:
                logger.warning(f"协作结果整合失败，使用结构化降级结果: {ex}")
                labels = {
                    AgentType.GENERAL: "综合服务建议",
                    AgentType.TECHNICAL: "技术排查建议",
                    AgentType.BILLING: "费用核验建议",
                }
                combined = "\n\n".join(
                    f"{labels.get(response.agent_type, '处理建议')}：\n{response.content}"
                    for response in successful
                )
        else:
            combined = "模型服务暂时不可用，未执行任何后台操作。请稍后重试或申请人工协助。"

        escalated = all_failed or any(
            isinstance(response, AgentResponse) and response.escalate
            for response in responses
        )

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[
                response.agent_type for response in responses
                if isinstance(response, AgentResponse) and response.success
            ] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route(self, intent: Optional[IntentCategory], urgency: Optional[UrgencyLevel]) -> AgentType:
        """
        三层路由决策：
          1. 意图映射
          2. 紧急度覆盖（CRITICAL 直接升级）
          3. 默认 GENERAL
        """
        if urgency == UrgencyLevel.CRITICAL:
            return AgentType.ESCALATION

        if intent and intent in self._INTENT_ROUTING:
            target = self._INTENT_ROUTING[intent]
            # 如果目标类型有可用实例则使用，否则降级
            if target in self._pool and self._pool[target]:
                return target

        return AgentType.GENERAL

    def _route_decision(self, req: Request) -> RoutingDecision:
        """
        结构化路由决策。

        Primary 只由最终 Intent 映射，Routing 层不重复进行领域打分。
        Supporting 只检查 Primary 之外的 Technical/Billing 强证据。
        """
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason="紧急度为 CRITICAL，触发升级路由",
                confidence=1.0,
            )

        if req.intent in (IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF):
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason=f"意图为 {req.intent.value if req.intent else 'unknown'}，触发升级路由",
                confidence=max(req.intent_confidence, 0.8),
            )

        primary_agent = self._route(req.intent, req.urgency)
        supporting_evidence = self._supporting_strong_evidence(req, primary_agent)
        supporting_agents = list(supporting_evidence)
        intent_name = req.intent.value if req.intent else "unknown"
        reason_parts = [f"primary={primary_agent.value} from intent={intent_name}"]
        if supporting_evidence:
            for agent_type, evidence in supporting_evidence.items():
                reason_parts.append(
                    f"supporting={agent_type.value} because {', '.join(evidence)}"
                )
        else:
            reason_parts.append("supporting=none; no strong cross-domain evidence")

        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason="; ".join(reason_parts),
            confidence=req.intent_confidence,
        )

    def _supporting_strong_evidence(
        self,
        req: Request,
        primary_agent: AgentType,
    ) -> Dict[AgentType, List[str]]:
        """返回非 Primary 领域的 Supporting Agent 及其强证据。

        当前只允许 Technical 与 Billing 互为 Supporting。General 不作为
        Supporting，也不因为宽泛业务词触发协作。每个领域最多返回一次。
        """
        if primary_agent == AgentType.TECHNICAL:
            candidate_domains = (AgentType.BILLING,)
        elif primary_agent == AgentType.BILLING:
            candidate_domains = (AgentType.TECHNICAL,)
        else:
            return {}

        message = (req.message or "").lower()
        entities = req.entities or {}
        candidates: Dict[AgentType, List[str]] = {}
        for domain in candidate_domains:
            evidence: List[str] = []
            for label, pattern in DOMAIN_STRONG_SIGNALS[domain]:
                if re.search(pattern, message, flags=re.IGNORECASE):
                    evidence.append(f"keyword={label}")
            for entity_name in DOMAIN_ENTITY_SIGNALS[domain]:
                if entities.get(entity_name):
                    evidence.append(f"entity={entity_name}")

            # 单个强关键词、单个强 Entity，或多个强证据均可触发。
            evidence = list(dict.fromkeys(evidence))
            if evidence and self._pool.get(domain):
                candidates[domain] = evidence

        return candidates

    @staticmethod
    def _needs_clarification(req: Request) -> bool:
        """低置信度且无明确意图时，先追问，避免误路由。"""
        if req.intent != IntentCategory.OTHER:
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False
        return req.intent_confidence < 0.5

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        性能路由：从同类 Agent 中选 routing_score() 最高的。
        这是"基于在线表现动态调整路由"的核心。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda a: a.stats.routing_score())

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行 Agent，失败时降级到 GeneralAgent。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(req)

        # 专属 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 technical_0。
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
