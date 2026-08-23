"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与内部工具治理框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    正式应用注入独立中文 Embedding Function；未注入时保留 ChromaDB 默认
    all-MiniLM-L6-v2，确保现有测试和调用方兼容。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        embedding_function: Optional[Any] = None,
        embedding_model: Optional[str] = None,
        collection_name: Optional[str] = None,
    ):
        self._embedding_function = embedding_function
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 显式 Embedding Function 在客户端统一编码文档和查询；未注入时继续使用
        # ChromaDB 默认模型。新模型使用独立 collection，避免新旧向量混用。
        metadata: Dict[str, Any] = {"description": "RelayDesk RAG 知识库"}
        if embedding_function is not None:
            metadata.update({
                "hnsw:space": "cosine",
                "embedding_model": embedding_model or "custom",
            })
        collection_args: Dict[str, Any] = {
            "name": collection_name or self.COLLECTION_NAME,
            "metadata": metadata,
        }
        if embedding_function is not None:
            collection_args["embedding_function"] = embedding_function
        self._collection = self._client.get_or_create_collection(**collection_args)

        # 如果知识库为空，导入企业级 SaaS 客户支持知识。
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ChromaDB 会自动生成 Embedding
            self._collection.add(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        查询侧优先使用模型专用 instruction 生成向量，再与文档向量做余弦匹配。
        """
        embed_query = getattr(self._embedding_function, "embed_query", None)
        if callable(embed_query):
            results = self._collection.query(
                query_embeddings=[embed_query(query)],
                n_results=top_k,
            )
        else:
            results = self._collection.query(
                query_texts=[query],
                n_results=top_k,
            )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── 内部工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为内部知识库工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入虚构 SaaS 产品的通用客户支持知识。"""
        default_docs = [
            {
                "title": "RelayDesk SaaS 客户服务平台使用说明",
                "content": (
                    "这是虚构 SaaS 产品 RelayDesk 的平台通用知识库规则。"
                    "适用场景：企业客户需要咨询产品能力、租户账号、Workspace 成员权限、技术故障、套餐费用或人工支持。"
                    "处理步骤：说明所在租户或 Workspace、目标和现象，再提供必要的脱敏信息；平台会进行信息澄清、知识说明和专业方向分流。"
                    "RelayDesk 当前只接入知识库搜索 Tool，不会声称已经修改租户配置、创建工单、授权、退款或完成后台操作。"
                    "人工升级条件：客户明确要求人工、问题紧急、涉及租户管理员权限、资金操作、安全风险，或多次澄清后仍无法判断。"
                ),
            },
            {
                "title": "SaaS 客户服务请求流程",
                "content": (
                    "适用场景：租户资料变更、成员权限、产品技术支持、套餐费用等需要办理或人工核验的请求。"
                    "处理步骤：明确请求事项和期望结果；准备租户或 Workspace 标识、成员账号、业务用途、期望时间及必要附件；通过平台正式入口提交并保存请求编号。"
                    "当前知识库只能说明流程和材料，不能生成真实请求编号、变更配置或返回处理结果。"
                    "人工升级条件：没有合适入口、涉及合同例外、请求长时间无反馈，或操作涉及高权限与敏感数据。"
                ),
            },
            {
                "title": "租户与 Workspace 资料变更",
                "content": (
                    "适用场景：需要修改租户名称、Workspace 显示名称、联系人邮箱、域名或成员资料。"
                    "处理步骤：确认租户、目标 Workspace 和变更字段；准备管理员身份与必要核验材料；通过真实租户后台或客户支持入口提交；完成后重新登录验证。"
                    "RelayDesk 未连接真实租户管理系统，不能读取、修改或确认账户资料。"
                    "人工升级条件：无法通过管理员验证、域名归属有争议、关键字段冲突，或变更影响 Organization 级权限。"
                ),
            },
            {
                "title": "客户支持请求进度说明",
                "content": (
                    "适用场景：企业客户已经提交支持请求，希望了解处理进度。"
                    "处理步骤：准备真实系统生成的请求编号、提交时间、问题类型和当前可见状态；先在原提交入口查询；超过平台通用响应时效后请求人工跟进。"
                    "RelayDesk 当前没有接入真实 CRM 或工单系统，不能查询或编造进度、负责人和完成时间。"
                    "人工升级条件：原入口无法访问、状态冲突、超过承诺时限，或问题对租户业务的影响持续扩大。"
                ),
            },
            {
                "title": "人工支持与投诉升级说明",
                "content": (
                    "适用场景：明确投诉、要求人工、紧急故障、服务多次未解决或涉及高风险决策。"
                    "处理步骤：记录租户或 Workspace、事件时间、影响范围、相关请求编号、已尝试步骤及期望结果；移除密码、验证码和完整财务信息；由人工团队核验。"
                    "RelayDesk 只会标记需要升级，不会创建真实工单或承诺处理结论。"
                    "立即升级条件：安全事件、数据丢失、多个租户不可用、疑似欺诈或重大费用争议。"
                ),
            },
            {
                "title": "客户支持响应时效说明",
                "content": (
                    "以下为平台通用响应目标，具体租户仍以套餐、合同与真实支持记录为准。"
                    "适用场景：客户询问技术响应、费用核验或人工升级需要多久。"
                    "通用规则为：一般咨询即时提供知识说明；普通人工请求预计 1 个工作日内响应；技术故障按租户影响范围分级；费用争议预计 1 至 3 个工作日完成初步核验。"
                    "处理步骤：保存请求时间与编号，在通用时限后仍无反馈时补充最新业务影响并请求人工跟进。"
                    "人工升级条件：超过约定时限、影响扩大、出现安全或资金风险，或客户核心业务完全受阻。"
                ),
            },
            {
                "title": "租户账号密码与 SSO 登录恢复",
                "content": (
                    "适用场景：本地账号忘记密码、账号锁定，或通过企业 IdP 的 SSO 登录失败。"
                    "处理步骤：先确认租户登录方式；本地账号通过平台验证入口重置；SSO 用户检查 IdP 登录状态、企业域名和成员映射；重新登录并验证目标 Workspace。"
                    "平台支持不会索要密码、短信验证码、恢复密钥或完整 SSO 断言。"
                    "人工升级条件：无法使用登记的验证方式、租户域名或 SSO 配置异常、成员映射不一致，或恢复后仍无法登录。"
                ),
            },
            {
                "title": "租户账号异常登录处理",
                "content": (
                    "适用场景：收到陌生登录提醒、登录地点异常或怀疑账号被他人使用。"
                    "处理步骤：停止继续输入敏感信息；从可信设备修改密码；退出其他会话；检查多因素认证和最近操作；保存异常时间与提示截图。"
                    "不要点击可疑邮件链接，也不要向平台支持人员提供验证码。"
                    "人工升级条件：仍有异常会话、账号无法控制、敏感数据可能泄露或发现未经授权的费用。"
                ),
            },
            {
                "title": "登录与 API 401 排查",
                "content": (
                    "适用场景：SaaS 平台登录或 API 调用返回 401。"
                    "401 通常表示认证信息缺失、失效或不匹配，但具体原因必须结合真实环境核验。"
                    "处理步骤：确认使用正确环境和登录入口；检查会话或 Token 是否过期；安全退出后重新登录；确认设备时间准确；记录发生时间、系统名称、请求 ID 和脱敏错误信息。"
                    "不得公开完整 Token、API Key、密码或验证码。"
                    "人工升级条件：重新认证仍失败、多名用户同时出现、账号状态异常或认证服务疑似故障。"
                ),
            },
            {
                "title": "Workspace 权限不足 403 排查",
                "content": (
                    "适用场景：可以登录，但访问页面、文件或接口时返回 403。"
                    "403 通常表示身份已识别但没有对应资源权限。"
                    "处理步骤：确认租户、Workspace、资源地址与环境；核对成员账号、角色和资源范围；确认权限是否刚变更并重新登录；由租户管理员通过真实后台调整。"
                    "RelayDesk 不连接真实租户权限系统，不能声称已经授权或变更角色。"
                    "人工升级条件：角色配置正确仍持续 403、疑似越权、敏感资源暴露或 Organization 级权限异常。"
                ),
            },
            {
                "title": "页面与 API 500 错误处理",
                "content": (
                    "适用场景：页面或接口返回 500、内部服务器错误。"
                    "处理步骤：保存尚未提交的内容；记录发生时间、页面地址、请求 ID、操作步骤和脱敏响应；安全刷新一次并确认能否复现；检查是否只有本人受影响；避免连续重复提交。"
                    "500 通常需要服务端核验，不能仅凭状态码断定根因。"
                    "人工升级条件：持续复现、影响多名用户、涉及数据写入或支付、出现数据错乱，或核心服务完全不可用。"
                ),
            },
            {
                "title": "SaaS 客户端崩溃处理",
                "content": (
                    "适用场景：SaaS 桌面或移动客户端闪退、卡死或反复崩溃。"
                    "处理步骤：先保存可恢复内容；记录软件版本、操作系统、崩溃时间和复现步骤；安全重启软件；检查官方更新；在不删除业务数据的前提下尝试最小复现。"
                    "不要未经备份删除配置、缓存目录或业务文件。"
                    "人工升级条件：更新后仍崩溃、影响多人、无法导出重要数据，或崩溃伴随权限和安全告警。"
                ),
            },
            {
                "title": "API Token、Webhook 与 SDK 连接排查",
                "content": (
                    "适用场景：API Token 失效、Webhook 回调超时或验签失败、SDK 连接异常。"
                    "处理步骤：确认租户和调用环境；核对 Token 权限范围与过期时间；检查回调地址、HTTPS 证书、签名时间窗和重试记录；记录 SDK 语言、版本、请求 ID 与脱敏日志。"
                    "不要提供完整 Token、签名密钥或私钥，也不要绕过证书校验。"
                    "人工升级条件：疑似密钥泄露、异常调用、持续验签失败、多个租户同时受影响，或需要服务端日志与后台配置。"
                ),
            },
            {
                "title": "SaaS 订阅发票申请说明",
                "content": (
                    "适用场景：企业客户需要为 SaaS 套餐、席位或增值服务费用申请发票。"
                    "处理步骤：准备租户、账单或交易编号、发票类型、抬头、税号、接收邮箱、开票金额和费用范围；核对无误后通过真实财务入口提交。"
                    "RelayDesk 不连接真实财务系统，不会声称已开票。平台通用处理目标为资料完整后 1 至 3 个工作日完成初步核验，实际结果以账户与财务记录为准。"
                    "人工升级条件：专票、企业转账、跨主体开票、资料不一致或超过约定时限。"
                ),
            },
            {
                "title": "SaaS 订阅发票抬头修改",
                "content": (
                    "适用场景：发票提交前或开具后需要修改抬头、税号等信息。"
                    "处理步骤：确认发票是否已开具；准备原费用编号、原抬头、新抬头、新税号和修改原因；通过真实财务入口申请核验。"
                    "未开具发票通常可在提交前更正；已开具、跨月或已报销发票可能涉及作废重开，不能承诺一定可以修改。"
                    "人工升级条件：发票已开具、跨月、已报销、税务信息冲突或需要作废重开。"
                ),
            },
            {
                "title": "SaaS 订阅重复扣款处理",
                "content": (
                    "适用场景：客户发现两笔疑似重复的套餐、席位或增值服务费用。"
                    "处理步骤：分别记录两笔交易的时间、金额、渠道和脱敏交易号；确认是否对应不同订阅或结算周期；不要再次支付；通过真实财务渠道提交核验。"
                    "RelayDesk 无法读取真实流水，不能直接认定重复扣款或承诺自动退款。"
                    "人工升级条件：两笔金额与时间高度相近、费用已实际入账、用户无法识别对应服务，或存在疑似欺诈。"
                ),
            },
            {
                "title": "SaaS 订阅支付失败处理",
                "content": (
                    "适用场景：支付被拒绝、超时或显示成功但服务未生效。"
                    "处理步骤：检查网络和支付渠道状态；核对金额、币种和支付限额；确认是否已有扣款记录；等待明确结果后再决定是否重试；保存脱敏交易号与错误提示。"
                    "不要提供支付密码、验证码或完整银行卡号，也不要在状态不明时连续支付。"
                    "人工升级条件：已扣款但服务未生效、状态长时间不一致、重复失败或涉及较大金额。"
                ),
            },
            {
                "title": "SaaS 订阅退款时效说明",
                "content": (
                    "适用场景：用户咨询退款申请、审核或到账时间。"
                    "平台通用流程为提交费用信息、人工核验、确认退款资格与金额、按原支付渠道处理。通用审核目标为 1 至 3 个工作日，渠道处理时间通常为审核通过后 3 至 7 个工作日；具体结果以租户套餐、合同和支付渠道为准。"
                    "处理步骤：准备交易号、支付时间、金额、渠道和退款原因；提交后保留申请记录；显示退款完成但未到账时核对原支付账户。"
                    "RelayDesk 不连接财务系统，不能确认资格、金额、状态或到账日期。"
                    "人工升级条件：超过约定时效、退款记录与账户不一致、原渠道失效或存在费用争议。"
                ),
            },
            {
                "title": "套餐、席位与自动续费说明",
                "content": (
                    "适用场景：查询周期订阅、自动续费、套餐变更或取消后的费用。"
                    "处理步骤：确认订阅名称、结算周期、续费日期和支付渠道；区分取消后续续费与退款当前周期；在真实订阅入口检查状态并保存变更确认。"
                    "知识库不提供租户的真实合同价格，也不读取账户订阅；实际费用、折扣和生效时间需要通过真实账户 Tool 或人工核验。"
                    "人工升级条件：取消后仍扣款、套餐与账单不一致、企业合同费用或用户对续费存在争议。"
                ),
            },
            {
                "title": "SaaS 费用争议人工核验流程",
                "content": (
                    "适用场景：用户不认可账单、扣款、退款金额、订阅费用或发票结果。"
                    "处理步骤：明确争议项目和期望结果；准备账单周期、金额、币种、脱敏交易号、支付渠道和相关申请记录；避免提交密码、验证码、完整卡号；由人工财务人员对真实记录进行核验。"
                    "平台通用初步响应目标为 1 个工作日，复杂争议预计 3 个工作日给出进度说明；具体时效以租户套餐、合同和真实支持记录为准。"
                    "RelayDesk 只能说明材料和流程，不能裁定争议、调整账单、发起退款或承诺补偿。"
                    "立即升级条件：疑似欺诈、未经授权扣款、大额差异、法律或监管风险，以及多次核验仍不一致。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
