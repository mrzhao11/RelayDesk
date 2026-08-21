# RelayDesk 完整使用指南

本文档说明 RelayDesk 的部署、启动、API 调用、知识库使用、ChromaDB 数据查看、监控评测和常见排障。

RelayDesk 是一个面向企业统一服务场景的多 Agent 请求协同系统，通过细粒度意图识别、按需 RAG、结构化主辅 Agent 路由、动态 Skills 和分层记忆，为用户提供综合咨询、技术支持、账户服务、费用结算和人工升级。

现有 Agent 对外角色如下，内部枚举和值保持兼容：

| 现有 Agent | 企业统一服务台角色 | 处理内容 |
|------------|--------------------|----------|
| `GeneralAgent` | 综合服务协调Agent | 通用咨询、流程说明、信息澄清、服务分流 |
| `TechnicalAgent` | 技术支持Agent | 登录失败、错误码、软件异常、系统故障 |
| `BillingAgent` | 费用与结算Agent | 账单、发票、退款、支付异常、订阅费用 |
| `ESCALATION` | 人工升级通道 | 投诉、紧急问题、低置信度或高风险请求 |

核心链路为：

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取 Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像
  -> IntentRecognizer 识别意图
  -> AgentOrchestrator 路由到 General/Technical/Billing Agent
  -> LLM 生成回复
  -> 写入 Redis，并异步更新 ChromaDB 用户画像
```

## 1. 项目结构

```text
RelayDesk/
├── api/main.py                    # FastAPI 入口，/chat /search /knowledge /monitor /eval
├── core/intent_recognizer.py      # 三路融合意图识别
├── agents/agent_orchestrator.py   # 多 Agent 路由编排
├── memory/conversation_memory.py  # Redis + ChromaDB 记忆管理
├── mcp/tool_manager.py            # 内部工具注册与可靠性治理、查询改写、重排、熔断、缓存、降级
├── mcp/knowledge_base.py          # ChromaDB RAG 知识库
├── monitor/performance_monitor.py # Agent/工具在线监控
├── evaluation/evaluator.py        # 端到端评测
├── skills/                        # 三类动态企业服务规范
├── RelayDeskFrontend/             # Vue 前端，与后端同仓库
├── tests/test_hardening.py        # 路由、RAG、协作和管理鉴权回归测试
├── scripts/smoke_acceptance.py    # 八个最小验收场景 HTTP 冒烟脚本
├── MIGRATION_NOTES.md             # 最小场景迁移与硬伤修复说明
├── docker-compose.yml             # Docker 全栈编排
├── Dockerfile
├── requirements.txt
└── .env
```

## 2. 环境准备

### 2.1 必需依赖

- Docker
- Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的第三方 API Key

### 2.2 配置 `.env`

复制示例文件：

```bash
cp .env.example .env
```

最少需要配置：

```env
ANTHROPIC_API_KEY=your_api_key
RELAYDESK_ADMIN_KEY=replace_with_a_long_random_value
```

`RELAYDESK_ADMIN_KEY` 用于保护知识写入、Skills 重载和评测接口；调用时通过
`X-RelayDesk-Admin-Key` 请求头传递。聊天、检索和只读状态接口不需要该密钥。

建议同时保留以下可靠性与安全默认值：

```env
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_DEVICE=cpu
EMBEDDING_QUERY_PROMPT=query
EMBEDDING_QUERY_INSTRUCTION=Given an enterprise service desk request, retrieve the most relevant policy or troubleshooting passage that answers the request
RAG_COLLECTION_NAME=knowledge_base_qwen3_embedding_0_6b
RAG_MIN_SCORE=0.45
RAG_TIMEOUT_SECONDS=20
CORS_ORIGINS=http://localhost,http://localhost:5173,http://127.0.0.1:5173
```

如果使用 DeepSeek 这类 Anthropic 兼容接口，可以配置：

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_API_KEY=your_deepseek_key
```

Embedding 与 LLM Provider 解耦：即使 LLM 使用 DeepSeek 兼容端点，Intent 仍会执行
`LLM 70% + Embedding 20% + Pattern 10%` 三路融合，RAG 也使用同一个中文模型。
首次运行会下载约 1.2 GB 模型。RAG 查询使用企业服务台专用英文 instruction，
文档和 Intent 模板不添加检索 instruction。更换 `EMBEDDING_MODEL` 时必须同时更换
`RAG_COLLECTION_NAME`，让知识库在新 collection 中重新导入，不能混用不同维度的向量。
`RAG_MIN_SCORE=0.45` 是基于当前 22 条有答案、3 条无答案样本得到的保守起点，
不是通用最优值；知识规模或模型变化后必须重新校准。

Docker Compose 场景下，Redis 和 ChromaDB 的连接由 `docker-compose.yml` 覆盖为容器内地址。通常不需要手动改：

```env
REDIS_PASSWORD=relaydesk123
CHROMA_HOST=localhost
CHROMA_PORT=8001
```

### 2.3 全栈部署和 run 开发模式的区别

RelayDesk 常用两种 Docker 启动方式：`docker compose up` 全栈部署，以及 `docker run` 开发模式。两者最大的区别是：**全栈部署会同时启动应用和依赖服务；run 开发模式通常只手动运行一个应用容器，依赖服务需要提前启动**。

| 对比项 | Docker Compose 全栈部署 | Docker run 开发模式 |
|--------|--------------------------|----------------------|
| 启动命令 | `docker compose up -d --build` | `docker run ... relaydesk ...` |
| 启动内容 | RelayDesk、Redis、ChromaDB、Prometheus、Nginx | 只启动你指定的单个容器 |
| Redis/ChromaDB | 自动启动并加入同一网络 | 必须先执行 `docker compose up -d redis chromadb` |
| 容器网络 | Compose 自动创建并管理 | 需要手动指定 `--network relaydesk_relaydesk-network` |
| 服务名解析 | 应用可直接访问 `redis`、`chromadb` | 只有加入同一网络后才可访问 `redis`、`chromadb` |
| 代码更新 | 通常需要 rebuild 或重启服务 | 挂载 `-v "$(pwd):/workspace"` 后，代码修改可直接生效，重启容器即可 |
| 适合场景 | 演示、联调、完整部署、HTTP API 服务 | 本地开发、调试 CLI、临时覆盖环境变量 |
| 常见问题 | API Key 或依赖健康检查失败 | 忘记启动 Redis/ChromaDB，导致 `redis:6379 Name or service not known` |

选择建议：

- 想完整体验 HTTP API、Swagger、Nginx、Prometheus：用 **Docker Compose 全栈部署**。
- 想调试源码或 CLI，并且希望本地改代码后快速重跑：用 **Docker run 开发模式**。
- 如果只是跑 CLI，最省心的方式是 `docker compose run --rm relaydesk python api/main.py --cli`，它会自动使用 Compose 网络。

## 3. Docker Compose 全栈部署

推荐使用此方式启动完整服务。

```bash
docker compose up -d --build
```

查看服务状态：

```bash
docker compose ps
```

查看应用日志：

```bash
docker compose logs -f relaydesk
```

看到 RelayDesk 启动日志并且健康检查通过后，服务可用。

启动后的端口：

| 服务 | 容器名 | 宿主机端口 | 容器内端口 | 用途 |
|------|--------|------------|------------|------|
| RelayDesk API | `relaydesk-app` | `8000` | `8000` | 主 API 服务 |
| Nginx | `relaydesk-nginx` | `80` | `80` | 反向代理 |
| ChromaDB | `relaydesk-chromadb` | `8001` | `8000` | 向量数据库 |
| Redis | `relaydesk-redis` | `6379` | `6379` | 工作记忆 |
| Prometheus | `relaydesk-prometheus` | `9090` | `9090` | 监控数据 |

健康检查：

```bash
curl http://localhost:8000/health
```

Swagger 文档：

```text
http://localhost:8000/docs
```

也可以通过 Nginx 访问：

```bash
curl http://localhost/health
```

## 4. Docker Run 开发模式

开发时可以只用 Compose 启动依赖，然后用 `docker run` 挂载当前代码目录。

先启动 Redis 和 ChromaDB：

```bash
docker compose up -d redis chromadb
```

构建镜像：

```bash
docker compose build --no-cache relaydesk
```

启动 HTTP 服务：

```bash
docker run -it --rm \
  --network relaydesk_relaydesk-network \
  -p 8000:8000 \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:relaydesk123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -e CHROMA_PERSIST_DIRECTORY="/workspace/data/chroma" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  relaydesk
```

CLI 交互模式：

```bash
docker run -it --rm \
  --network relaydesk_relaydesk-network \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:relaydesk123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  relaydesk \
  python api/main.py --cli
```

## 5. Swagger 和接口总览

RelayDesk 基于 FastAPI 构建，启动 HTTP 服务后可以直接在浏览器访问 Swagger UI 调用接口。

本地 Swagger 地址：

```text
http://localhost:8000/docs
```

如果使用 Nginx 反向代理：

```text
http://localhost/docs
```

打开 Swagger 后，可以点击任意接口右侧的 **Try it out**，填写参数后点 **Execute** 直接调用本地服务。常用调试顺序：

```text
1. GET /health                确认服务是否就绪
2. POST /chat                 测试主对话链路
3. GET /knowledge/stats       查看知识库是否已有数据
4. POST /knowledge/upload     上传演示知识库文件
5. POST /search               测试知识库检索、查询改写和重排
6. GET /monitor               查看 Agent 和工具运行指标
7. GET /skills                查看已加载 Skills
8. POST /skills/reload        重新加载 Skills
9. POST /eval/run             运行端到端评测
```

### 5.1 接口总览

| 方法 | 路径 | 参数位置 | 作用 | 适合场景 |
|------|------|----------|------|----------|
| `GET` | `/health` | 无 | 健康检查，返回服务状态和 Agent 统计 | 启动后确认服务可用 |
| `POST` | `/chat` | JSON Body | 主对话接口，完成记忆读取、意图识别、Agent 路由、回复生成、记忆写入 | 业务主链路 |
| `GET` | `/monitor` | 无 | 查看 Agent/工具统计、告警和优化建议 | 观察在线表现 |
| `POST` | `/search` | Query 参数 | 执行知识库检索优化链路：查询改写、并行召回、合并去重、LLM 重排 | 测试 RAG 检索 |
| `GET` | `/skills` | 无 | 查看当前加载的 Skills、匹配关键词和解析错误 | 确认动态能力是否生效 |
| `POST` | `/skills/reload` | 管理密钥 Header | 运行时重新扫描 Skill 目录 | 修改业务规则后热加载 |
| `POST` | `/knowledge/add` | 管理密钥 Header + JSON Body | 批量导入文档到 ChromaDB 知识库 | 程序化导入文档 |
| `POST` | `/knowledge/upload` | 管理密钥 Header + Form File | 上传 `.txt`、`.md`、`.json` 文件导入知识库 | 手动上传知识库文件 |
| `GET` | `/knowledge/stats` | 无 | 查看知识库文档片段总数 | 确认知识库是否有数据 |
| `POST` | `/eval/run` | 管理密钥 Header | 运行内置意图识别和端到端对话评测 | 演示 LLM-as-Judge 评测 |
| `GET` | `/docs` | 浏览器访问 | Swagger UI | 浏览和调试所有接口 |

### 5.2 Skills 动态能力加载

RelayDesk 支持从目录加载 Skills，用来把业务流程、客服话术、排障 SOP 等规则动态注入 Agent。

默认配置：

```env
RELAYDESK_SKILLS_DIR=./skills
RELAYDESK_SKILLS_MAX_PROMPT_CHARS=5000
```

推荐结构：

```text
skills/general_customer_service/SKILL.md
skills/technical_support/SKILL.md
skills/billing_support/SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: 企业综合服务接待规范
description: 通用咨询、流程说明、信息澄清和服务分流规范
keywords: 咨询,流程,申请,投诉,人工
agents: general
enabled: true
---

# 企业综合服务接待规范

- 信息不足时先确认关键信息。
- 不得编造企业制度、审批状态或后台操作结果。
- 涉及真实权限、资金或高风险请求时转人工核验。
```

查看加载结果：

```bash
curl http://localhost:8000/skills
```

修改 Skill 文件后热加载：

```bash
curl -X POST http://localhost:8000/skills/reload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY"
```

### 5.3 `/health`

用途：确认服务是否初始化完成。

```bash
curl http://localhost:8000/health
```

响应示例：

```json
{
  "status": "ok",
  "agents": {
    "general_0": {
      "total": 0,
      "success_rate": 1.0,
      "avg_ms": 0.0,
      "monitor_penalty": 0.0,
      "routing_score": 1.0
    }
  }
}
```

### 5.4 `/chat`

用途：主对话接口。

请求体：

```json
{
  "message": "我要退款",
  "user_id": "user_001",
  "conv_id": "session_001"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `message` | 是 | 用户输入 |
| `user_id` | 否 | 用户 ID，默认 `anonymous` |
| `conv_id` | 否 | 会话 ID，不传则自动生成 |

返回字段：

| 字段 | 说明 |
|------|------|
| `conv_id` | 会话 ID |
| `response` | Agent 回复 |
| `intent` | 意图识别结果 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级 |
| `latency_ms` | 端到端耗时 |

### 5.5 `/search`

用途：测试内部知识库工具和 RAG 检索优化。RelayDesk 实现了内部工具注册与可靠性治理框架，目前接入 `knowledge_search`，并支持缓存、超时、熔断、fallback、查询改写和结果重排；当前不属于完整标准 MCP Server 实现。

Query 参数：

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | 是 | 无 | 用户检索问题 |
| `top_k` | 否 | `5` | 返回结果数量 |

示例：

```bash
curl -X POST "http://localhost:8000/search?query=退款多久到账&top_k=3"
```

### 5.6 `/knowledge/add`

用途：通过 JSON 批量导入知识库。

请求体：

```json
{
  "documents": [
    {
      "title": "企业账号密码重置（演示）",
      "content": "适用场景：忘记企业账号密码。处理步骤：通过企业身份验证入口完成重置。人工升级条件：无法使用登记的验证方式。"
    }
  ]
}
```

### 5.7 `/knowledge/upload`

用途：上传文件导入知识库。

支持格式：

| 格式 | 说明 |
|------|------|
| `.txt` | 整个文件作为一篇文档 |
| `.md` | 整个文件作为一篇文档 |
| `.json` | JSON 数组，格式为 `[{ "title": "...", "content": "..." }]` |

示例：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

### 5.8 `/knowledge/stats`

用途：查看知识库片段数量。

```bash
curl http://localhost:8000/knowledge/stats
```

### 5.9 `/monitor`

用途：查看 Agent 和工具在线指标。

```bash
curl http://localhost:8000/monitor
```

返回内容包括：

| 字段 | 说明 |
|------|------|
| `agent_stats` | Agent 调用次数、成功率、延迟、routing_score |
| `tool_stats` | 工具调用次数、成功率、延迟、熔断状态 |
| `active_alerts` | 最近告警 |
| `suggestions` | 优化建议 |

### 5.10 `/eval/run`

用途：运行内置评测。

```bash
curl -X POST http://localhost:8000/eval/run \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY"
```

返回内容包括：

| 字段 | 说明 |
|------|------|
| `pass_rate` | 评测通过率 |
| `total` | 评测项总数 |
| `passed` | 通过项数量 |
| `avg_scores` | 平均评分 |
| `regressions` | 回归检测结果 |
| `recommendations` | 优化建议 |
| `results` | 每条评测结果 |

## 6. 使用项目

### 6.1 主对话接口

请求：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "企业统一服务台可以处理哪些问题？",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

响应示例：

```json
{
  "conv_id": "session_001",
  "response": "我可以协助通用咨询、账户问题、技术故障、费用结算和人工升级。涉及真实后台结果时需要对应系统或人工核验。",
  "intent": "query",
  "agent_type": "general",
  "escalated": false,
  "latency_ms": 1234.5
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `message` | 用户输入 |
| `user_id` | 用户唯一标识，用于隔离记忆和用户画像 |
| `conv_id` | 会话 ID，相同 `conv_id` 表示同一轮多轮对话 |
| `intent` | 识别出的意图 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级/转人工 |
| `latency_ms` | 端到端延迟 |

### 6.2 多轮对话

多轮对话只需要保持同一个 `user_id` 和 `conv_id`。

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我需要申请企业账号权限",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

系统会从 Redis 读取当前会话最近消息，并从 ChromaDB 读取相关历史和用户画像，拼成上下文传给 Agent。

### 6.3 技术问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "应用登录一直报 401 错误",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

预期会路由到 `technical` Agent。

### 6.4 账单问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "为什么这个月重复扣款了？我要退款",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

预期会路由到 `billing` Agent。

### 6.5 复合问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "登录报错 401，而且这个月还重复扣款了",
    "user_id": "user_mix",
    "conv_id": "mix_001"
  }'
```

这类问题会触发多 Agent 并行协作，由技术 Agent 和账单 Agent 分别处理后合并回复。

## 7. 知识库使用

RelayDesk 的知识库由 `mcp/knowledge_base.py` 管理，底层使用 ChromaDB collection：

```text
knowledge_base
```

首次启动时，如果知识库为空或仍是片段不足的旧版演示库，会补充 20 篇企业统一服务台演示知识。内容覆盖综合服务、账号与技术、费用与结算；每篇都明确适用场景、处理步骤、人工升级条件和“非真实企业制度”边界。长文档继续按约 500 字切片。

### 7.1 查看知识库统计

```bash
curl http://localhost:8000/knowledge/stats
```

响应示例：

```json
{
  "total_chunks": 20
}
```

### 7.2 批量导入文档

```bash
curl -X POST http://localhost:8000/knowledge/add \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "VPN 连接说明（演示）",
        "content": "适用场景：企业 VPN 无法连接。处理步骤：检查网络、客户端状态和设备时间。人工升级条件：证书告警或多人同时断连。"
      },
      {
        "title": "费用核验流程（演示）",
        "content": "适用场景：用户对费用记录存在争议。处理步骤：准备脱敏交易信息并提交人工财务核验。"
      }
    ]
  }'
```

系统会把长文档切成 500 字左右的片段，并写入 ChromaDB。

### 7.3 上传文件导入知识库

上传 Markdown：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -F "file=@data/demo_docs/troubleshooting.md"
```

上传 JSON：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

JSON 格式必须是数组：

```json
[
  {
    "title": "文档标题",
    "content": "文档内容"
  }
]
```

### 7.4 检索知识库

```bash
curl -X POST "http://localhost:8000/search?query=退款需要多久到账&top_k=3"
```

响应示例：

```json
{
  "query": "退款需要多久到账",
  "results": [
    {
      "title": "退款政策",
      "content": "审核通过后，款项将在 5-7 个工作日内退回原支付账户。",
      "score": 0.82,
      "chunk": 0
    }
  ],
  "reranked": true
}
```

`/search` 使用的是完整检索优化链路：

```text
原始查询
  -> LLM 查询改写成多个角度
  -> 多个子查询并行召回 ChromaDB
  -> 合并去重
  -> LLM 重排
  -> 返回 Top-K
```

## 8. ChromaDB 在项目中的用途

RelayDesk 使用了三个 ChromaDB collection：

| Collection | 模块 | 作用 |
|------------|------|------|
| `knowledge_base_qwen3_embedding_0_6b` | `mcp/knowledge_base.py` | 使用 Qwen3 Embedding 的 RAG 知识库文档片段 |
| `episodic` | `memory/conversation_memory.py` | 压缩后的历史对话摘要 |
| `user_profile` | `memory/conversation_memory.py` | 用户画像，包含偏好和关键实体 |

数据写入时机：

| 数据 | 写入时机 |
|------|----------|
| `knowledge_base` | 启动时自动导入默认文档，或调用 `/knowledge/add`、`/knowledge/upload` |
| `episodic` | 当前会话工作记忆超过阈值后自动压缩并写入 |
| `user_profile` | 每次 `/chat` 回复后异步提炼并更新 |

## 9. 在 Docker 中查看 ChromaDB 内容

Compose 中 ChromaDB 容器名是：

```text
relaydesk-chromadb
```

宿主机访问端口是：

```text
http://localhost:8001
```

容器内部端口是：

```text
http://localhost:8000
```

### 9.1 查看 ChromaDB 是否存活

宿主机执行：

```bash
curl http://localhost:8001/api/v1/heartbeat
```

容器内执行：

```bash
docker exec -it relaydesk-chromadb curl http://localhost:8000/api/v1/heartbeat
```

### 9.2 查看所有 collection

```bash
curl http://localhost:8001/api/v1/collections
```

如果 ChromaDB 版本返回 tenant/database 相关错误，可以使用 Python 客户端查看，见下一节。

### 9.3 用 Python 客户端查看 collections

进入应用容器：

```bash
docker exec -it relaydesk-app bash
```

在容器里执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
print("heartbeat:", client.heartbeat())

collections = client.list_collections()
print("collections:")
for c in collections:
    print("-", c.name, "count=", c.count())
PY
```

预期可以看到：

```text
collections:
- knowledge_base count= ...
- episodic count= ...
- user_profile count= ...
```

### 9.4 查看 `knowledge_base` 文档内容

```bash
docker exec -it relaydesk-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

data = col.get(limit=10, include=["documents", "metadatas"])
for i, doc_id in enumerate(data["ids"]):
    print("=" * 80)
    print("id:", doc_id)
    print("metadata:", data["metadatas"][i])
    print("document:", data["documents"][i][:500])
PY
```

### 9.5 查询 `knowledge_base`

```bash
docker exec -it relaydesk-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

result = col.query(
    query_texts=["退款多久到账"],
    n_results=3,
    include=["documents", "metadatas", "distances"],
)

for doc, meta, dist in zip(
    result["documents"][0],
    result["metadatas"][0],
    result["distances"][0],
):
    print("=" * 80)
    print("title:", meta.get("title"))
    print("distance:", dist)
    print("content:", doc[:300])
PY
```

### 9.6 查看用户画像 `user_profile`

先多调用几次 `/chat`，让系统异步生成用户画像：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "我经常咨询企业账号和费用问题，回答请简洁一点", "user_id": "profile_user", "conv_id": "profile_session"}'
```

等待几秒后查看：

```bash
docker exec -it relaydesk-app bash
```

```bash
python - <<'PY'
import json
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("user_profile")

data = col.get(
    where={"user_id": "profile_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print(json.dumps(json.loads(doc), ensure_ascii=False, indent=2))
PY
```

### 9.7 查看情景记忆 `episodic`

情景记忆只有在当前会话消息数量达到压缩阈值后才会写入。默认阈值在 `MemoryManager.COMPRESS_AT` 中，目前是 15 条消息。

可以连续发送多条消息触发压缩：

```bash
for i in $(seq 1 16); do
  curl -s -X POST http://localhost:8000/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"这是第 $i 条测试消息，我想咨询退款和订单问题\", \"user_id\": \"episodic_user\", \"conv_id\": \"episodic_session\"}" > /dev/null
done
```

查看情景记忆：

```bash
docker exec -it relaydesk-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": "episodic_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 9.8 查看 ChromaDB 持久化文件

ChromaDB 的持久化卷在 Compose 中定义为：

```yaml
volumes:
  chromadb-data:
```

查看 Docker volume：

```bash
docker volume ls | grep chromadb
docker volume inspect relaydesk_chromadb-data
```

查看容器内数据目录：

```bash
docker exec -it relaydesk-chromadb sh
ls -lah /chroma/chroma
find /chroma/chroma -maxdepth 2 -type f | head
```

注意：不建议直接修改这些底层文件。查看和管理数据应优先使用 ChromaDB API 或 Python 客户端。

### 9.9 清空 ChromaDB 数据

谨慎操作。停止服务并删除 volume：

```bash
docker compose down
docker volume rm relaydesk_chromadb-data
docker compose up -d --build
```

如果只想删除某个 collection，可以用 Python 客户端：

```bash
docker exec -it relaydesk-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
client.delete_collection("knowledge_base")
print("deleted knowledge_base")
PY
```

删除后重启应用，`KnowledgeBase` 会在 collection 为空时重新导入默认文档。

## 10. Redis 工作记忆查看

Redis 容器名：

```text
relaydesk-redis
```

进入 Redis：

```bash
docker exec -it relaydesk-redis redis-cli -a relaydesk123
```

查看 key：

```redis
KEYS *
```

工作记忆 key 格式：

```text
wm:{user_id}:{conv_id}
```

会话摘要 key 格式：

```text
summary:{user_id}:{conv_id}
```

查看某个会话最近消息：

```redis
LRANGE wm:user_001:session_001 0 -1
```

查看 TTL：

```redis
TTL wm:user_001:session_001
```

默认 TTL 是 24 小时。

## 11. 查看工作记忆压缩内容

工作记忆压缩发生在 `memory/conversation_memory.py` 中。默认配置：

```text
WORKING_MAX = 20
COMPRESS_AT = 15
```

当同一个 `user_id + conv_id` 的工作记忆达到 15 条消息时，系统会：

```text
旧消息 -> LLM 摘要 -> Redis summary
旧消息摘要 -> ChromaDB episodic
最近 5 条消息 -> 继续保留在 Redis wm 列表
```

日志示例：

```text
工作记忆压缩完成: cli_user/5a076f2b-b607-4339-9e9f-f0399862d366，摘要 19 字
```

其中：

```text
user_id = cli_user
conv_id = 5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.1 查看 Redis 中的会话摘要

进入 Redis：

```bash
docker exec -it relaydesk-redis redis-cli -a relaydesk123
```

查询摘要：

```redis
GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

一条命令快速查看：

```bash
docker exec -it relaydesk-redis redis-cli -a relaydesk123 \
  GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.2 查看压缩后仍保留的最近 5 条工作记忆

进入 Redis 后执行：

```redis
LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

一条命令快速查看：

```bash
docker exec -it relaydesk-redis redis-cli -a relaydesk123 \
  LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

说明：

- Redis 使用 `LPUSH` 写入，最新消息在列表前面。
- 代码读取时会 `reversed(raws)` 还原时间顺序。
- 压缩后 Redis 工作记忆列表只保留最近 5 条；更早的内容会以摘要形式进入 Redis summary 和 ChromaDB `episodic`。

### 11.3 查看 ChromaDB 中的情景记忆摘要

如果是全栈部署，应用容器名通常是：

```text
relaydesk-app
```

进入应用容器：

```bash
docker exec -it relaydesk-app bash
```

如果你是用 `docker run --rm` 跑 CLI，容器名可能是随机的。先查看：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Networks}}\t{{.Status}}'
```

进入对应容器：

```bash
docker exec -it <容器名> bash
```

执行 Python 脚本查询 `episodic`：

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"
conv_id = "5a076f2b-b607-4339-9e9f-f0399862d366"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    meta = data["metadatas"][i]
    if meta.get("conv_id") == conv_id:
        print("=" * 80)
        print("metadata:", meta)
        print("summary:", doc)
        print("full_text_preview:", meta.get("full_text"))
PY
```

字段说明：

| 字段 | 含义 |
|------|------|
| `documents[i]` | LLM 生成的历史对话摘要 |
| `metadata.user_id` | 用户 ID |
| `metadata.conv_id` | 会话 ID |
| `metadata.ts` | 写入时间 |
| `metadata.full_text` | 被压缩的原始旧消息前 500 字预览 |

### 11.4 如果只想看某个用户的所有情景记忆

```bash
docker exec -it relaydesk-app bash
```

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 11.5 Redis summary 和 ChromaDB episodic 的区别

| 位置 | 保存内容 | 用途 |
|------|----------|------|
| Redis `summary:{user_id}:{conv_id}` | 当前会话压缩摘要 | 下一次同会话请求直接拼入 prompt |
| ChromaDB `episodic` | 压缩摘要 + metadata | 跨会话按语义检索相关历史 |
| Redis `wm:{user_id}:{conv_id}` | 最近 5 条消息 | 保持当前对话连贯性 |

## 12. Monitor 在线监控

查看监控摘要：

```bash
curl http://localhost:8000/monitor
```

响应包含：

```json
{
  "agent_stats": {
    "general_0": {
      "total": 10,
      "success_rate": 1.0,
      "avg_ms": 1200.3,
      "monitor_penalty": 0.0,
      "routing_score": 0.836
    }
  },
  "tool_stats": {
    "knowledge_search": {
      "total": 5,
      "success_rate": 1.0,
      "avg_latency_ms": 80.2,
      "consecutive_fails": 0,
      "circuit_state": "closed"
    }
  },
  "active_alerts": [],
  "suggestions": []
}
```

指标含义：

| 指标 | 含义 |
|------|------|
| `total` | 调用次数 |
| `success_rate` | 成功率 |
| `avg_ms` / `avg_latency_ms` | 平均延迟 |
| `routing_score` | Agent 路由评分 |
| `monitor_penalty` | Monitor 根据在线表现写回的降权系数 |
| `consecutive_fails` | 工具连续失败次数 |
| `circuit_state` | 工具熔断器状态，可能是 `closed`、`open`、`half_open` |

Prometheus 页面：

```text
http://localhost:9090
```

## 13. 运行端到端评测

```bash
curl -X POST http://localhost:8000/eval/run \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY"
```

评测内容：

1. 意图识别准确率和 Macro-F1
2. 调用 Orchestrator 生成真实回复
3. LLM-as-Judge 从相关性、准确性、完整性、有用性打分
4. 与上一次评测结果做回归检测
5. 生成优化建议

响应示例：

```json
{
  "pass_rate": 0.83,
  "total": 5,
  "passed": 4,
  "avg_scores": {
    "intent_accuracy": 0.875,
    "relevance": 0.88,
    "accuracy": 0.82,
    "completeness": 0.79,
    "helpfulness": 0.85
  },
  "regressions": [],
  "recommendations": [
    "意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据"
  ],
  "results": []
}
```

## 14. 停止、重启和清理

停止服务：

```bash
docker compose stop
```

重启服务：

```bash
docker compose restart relaydesk
```

停止并删除容器，但保留数据卷：

```bash
docker compose down
```

停止并删除容器和数据卷：

```bash
docker compose down -v
```

重新构建并启动：

```bash
docker compose up -d --build
```

## 15. 常见问题

### 15.1 `/health` 返回 503

查看应用日志：

```bash
docker compose logs -f relaydesk
```

重点检查：

- `.env` 是否配置 `ANTHROPIC_API_KEY`
- Redis 是否健康
- ChromaDB 是否健康
- 应用容器是否正在反复重启

### 15.2 ChromaDB 连接失败

查看 ChromaDB 状态：

```bash
docker compose ps chromadb
docker compose logs -f chromadb
curl http://localhost:8001/api/v1/heartbeat
```

应用容器内测试：

```bash
docker exec -it relaydesk-app bash
python - <<'PY'
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
print(client.heartbeat())
PY
```

### 15.3 Redis 认证失败

确认 `.env` 和 `docker-compose.yml` 中使用的密码一致。默认密码是：

```text
relaydesk123
```

测试连接：

```bash
docker exec -it relaydesk-redis redis-cli -a relaydesk123 ping
```

### 15.4 `/search` 没有结果

先确认知识库中有数据：

```bash
curl http://localhost:8000/knowledge/stats
```

如果是 0，可以重新导入演示文档：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

再测试：

```bash
curl -X POST "http://localhost:8000/search?query=API如何接入&top_k=3"
```

### 15.5 用户画像查不到

用户画像是异步更新的，并且依赖 LLM 调用成功。排查步骤：

1. 先调用 `/chat`，使用固定 `user_id`
2. 等待几秒
3. 查看 `docker compose logs -f relaydesk` 是否出现 `用户画像已更新`
4. 使用第 8.6 节的 Python 脚本查询 `user_profile`

### 15.6 情景记忆查不到

情景记忆不是每次对话都写入。只有当前会话消息数达到压缩阈值后才写入。默认阈值：

```text
MemoryManager.COMPRESS_AT = 15
```

连续发 16 条以上消息后再查看 `episodic`。

## 16. 推荐验证流程

完整验证可以按这个顺序执行：

```bash
# 1. 启动
docker compose up -d --build

# 2. 健康检查
curl http://localhost:8000/health

# 3. 主对话
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，我想了解退款政策", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. 知识库统计
curl http://localhost:8000/knowledge/stats

# 5. 导入演示知识库
curl -X POST http://localhost:8000/knowledge/upload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY" \
  -F "file=@data/demo_docs/sample_knowledge.json"

# 6. 检索
curl -X POST "http://localhost:8000/search?query=RelayDesk如何接入API&top_k=3"

# 7. 监控
curl http://localhost:8000/monitor

# 8. Skills
curl http://localhost:8000/skills

# 9. 评测
curl -X POST http://localhost:8000/eval/run \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY"
```
