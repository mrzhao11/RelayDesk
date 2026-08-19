# RelayDesk Current Design Rationale Benchmark

> 报告语言：中文  
> 基准性质：**Reconstructed Baseline（重建基线）**  
> 基准提交：`b27f122f918a42485afb300dacb6d547de47bd40`  
> 执行日期：2026-08-20（Asia/Shanghai）  
> 重要边界：本文验证“当前设计相对构造基线是否合理”，不把构造基线描述为真实历史版本，不虚构线上指标、用户反馈或历史演进。

## 最前面直接回答十个问题

### Q1. 相比最简单 LLM-only Intent，当前三路 Intent Fusion 到底提升了什么？

**本轮不能给出有效的提升数值。** 配置中的 `api.deepseek.com / deepseek-v4-pro` 在最小调用时返回 `HTTP 401`，所以 LLM Only、LLM + Pattern、LLM + Embedding 和 Current Fusion 都是 `NOT EXECUTED / 未执行`。把 LLM 失败后的 Pattern fallback 当成 Current 会夸大结论，因此没有这样做。

可复现的组件诊断显示：45 条非歧义样本上，Pattern 单分支 Accuracy 为 `0.4889`、Macro-F1 为 `0.4968`；本地字符 n-gram Embedding 单分支 Accuracy 为 `0.2889`、Macro-F1 为 `0.2520`。Pattern 在 fine-grained 分组上 Accuracy 为 `0.6667`，说明它有补充细粒度强关键词的潜力，但这不是“相对 LLM Only 的增益”。

还必须纠正一个容易误讲的点：当前 `.env` 使用第三方兼容端点，因此正式代码会关闭 Embedding，实际融合是 **LLM 85% + Pattern 15%**，不是三路 70/20/10。只有不设置第三方 `base_url` 时，代码才启用本地 Embedding 并采用 70/20/10。

### Q2. 相比 Direct ChromaDB Retrieval，Query Rewrite 到底解决了哪些真实 Bad Cases？

**本轮没有证据证明 Query Rewrite 已解决任何案例。** Direct Retrieval 已执行并观察到 10 个 Top-5 漏召回案例，包括服务台能力、申请进度、403、软件秒退、开票资料、发票抬头、重复交易、退款时效、账户资料和权限排查。Query Rewrite 因模型 401 未执行，不能把这些“候选动机案例”写成“已修复案例”。

当前能真实回答的是：Direct Retrieval 在中文口语、术语不一致和短查询上存在明显漏召回，因此 Query Rewrite 有合理动机；它是否有效，仍需模型恢复后补跑同一数据集。

### Q3. Rerank 主要提升 Recall 还是 Ranking Quality？

从代码机制看，Rerank 只重排已经召回的候选，不能创造新候选，所以设计目标主要是 **Ranking Quality / MRR**，不是候选集 Recall。实验上，Rerank 本轮 `NOT EXECUTED / 未执行`，不能声称 MRR 已提升。Direct 基线的 MRR 为 `0.2303`，只是待改进基线。

### Q4. 相比 Single General Agent，Multi-Agent 到底解决了什么问题？

在 12 条固定 gold intent 的确定性路由基准上，Single General 的必要角色关注点覆盖率为 `0.1667`，Current Primary + Supporting 为 `1.0000`，角色集合完全匹配率也从 `0.1667` 到 `1.0000`。这说明当前结构确实解决了“请求应交给哪个专业角色、复合请求是否覆盖两个专业角色”的问题。

但回答内容没有执行，因此不能把角色覆盖率解释成 Correctness、Completeness 或用户体验提升。

### Q5. 相比 Primary-Agent Only，Supporting Agent 什么时候真正有价值？

在本数据集的 5 条双领域样本中，Supporting Agent 补上了 Primary-only 缺失的第二专业角色，包括 401 + 重复扣款、500 + 重复支付、订阅 + 崩溃、账户安全 + 陌生扣款、发票 + 500。整体关注点角色覆盖率从 Primary-only 的 `0.7917` 提升到 Current 的 `1.0000`。

对单领域问题，Primary-only 已覆盖必要角色；此时 Supporting Agent 没有结构收益，不应无条件启用。当前实现通过 `_collaboration_targets` 和分数阈值只在显式复合领域出现时增加辅助角色，这个方向是合理的。

### Q6. 相比直接拼历史聊天，分层 Memory 为什么值得存在？

当前代码提供工作记忆、会话摘要、情景记忆和用户画像四类上下文，可针对长期事实、跨会话偏好和上下文预算分别处理；直接拼历史无法同时解决无限增长和旧事实检索。

不过本轮只验证了 `MemoryContext` 的结构与 prompt 格式，摘要压缩、画像提取、跨会话召回和事实保留率均 `NOT EXECUTED / 未执行`。因此“值得存在”目前是工程合理性判断，不是效果已被证明。`COMPRESS_AT=15`、保留最近 5 条和 24h TTL 都只是配置，不是效果证据。

### Q7. Dynamic Skills 相比全量 system prompt 有什么实际收益？

在 9 条 General/Technical/Billing 样本上，全量注入平均 `3507` 字符，Dynamic Skills 平均 `1238.56` 字符，减少 `64.68%`；无关 Skill 数从平均 `2.0` 降到 `0`，必要 Skill 覆盖率为 `1.0000`。这是本轮较强的结构证据。

Rule Compliance 和回答质量因模型不可用而未执行，所以不能进一步声称动态注入让答案更准确。

### Q8. Tool Cache / Timeout / Breaker / Fallback 分别解决什么真实故障？

- Cache：相同参数两次调用，handler 实际调用从 `2` 次降到 `1` 次。
- Timeout：0.2 秒慢请求在故障注入阈值 0.05 秒下约 `52.177ms` 返回 fallback，而 V0 等待约 `201.216ms`。正式默认 timeout 是 30 秒，本轮缩短值只用于验证机制。
- Breaker：连续 5 次失败后状态变为 OPEN；第 6 次没有调用 handler；恢复窗口后探测成功并回到 CLOSED。
- Fallback：慢请求、永久阻塞和 handler 异常都返回结构化降级结果，避免异常直接冒泡或主链路无限等待。

### Q9. 当前哪些参数有实验支持，哪些只是经验配置？

本轮支持的是**机制**，不是正式参数最优性：按需 Skills 能减少 prompt；Primary + Supporting 能覆盖复合领域；Cache/Timeout/Breaker/Fallback 状态机按预期工作。

以下仍主要是经验配置：Intent 的 70/20/10 或 85/15、置信度阈值 0.5、路由 supporting 阈值 0.45 和 0.55 比例、RAG 最低分 0.20、Top-K、12 秒 RAG timeout、Tool 30 秒 timeout、熔断 5 次/60 秒、Memory 15 条压缩/保留 5 条/24h TTL。不能在面试中说这些值是实验选出的最优值。

### Q10. “为什么要这样设计？”当前测试能给出哪些真实、可复现的证据？

可以说：

1. Direct Retrieval 在当前中文演示知识上只有 `Recall@5=0.5227`、`MRR=0.2303`，存在做 rewrite/rerank 的真实动机，但当前优化效果待补测。
2. Current 路由在手工标注的 12 条角色覆盖数据上达到 `1.0000`，而 General-only 为 `0.1667`、Primary-only 为 `0.7917`。
3. Dynamic Skills 将平均 prompt 字符减少 `64.68%`，同时保持必要 Skill 覆盖。
4. Tool 治理在缓存、超时、异常、连续故障和恢复注入下均表现出预期保护行为。

不能说：三路 Intent 已优于 LLM Only、Rewrite 已解决 Direct 的 10 个坏案例、Rerank 已提升 MRR、多 Agent 已提高回答质量、分层 Memory 已提高事实保留、Judge 已稳定区分回答。这些都因模型 401 或端到端条件不足而未执行。

---

## 1. Executive Summary

本次基准对当前 RelayDesk 做了**只读设计验证**。新增内容只位于 `evaluation/benchmark/`、`evaluation/datasets/`、`evaluation/results/` 和 `docs/evaluation/`，没有修改 `core/`、`agents/`、`api/`、`memory/`、`mcp/`、`monitor/`、`skills/` 的正式实现。

结论分三层：

| 证据等级 | 结论 |
|---|---|
| 较强、已执行 | 路由的角色关注点覆盖；Dynamic Skills 的 prompt 缩减与污染减少；Tool 生命周期可靠性 |
| 有问题证据、无优化效果证据 | Direct RAG 的召回与排序较弱，证明有优化动机；Rewrite/Rerank 效果未执行 |
| 仍是工程假设 | Intent Fusion 增益与权重；分层 Memory 效果；多 Agent 回答质量；Judge 稳定性 |

最重要的硬结论不是“当前设计全部正确”，而是：**项目已经具备几个合理的复杂机制，但对 LLM 依赖最强的设计还缺少可复现的效果证据；当前模型鉴权失败是完成这些证据链的直接阻塞项。**

## 2. Test Environment

| 项目 | 实测值 |
|---|---|
| Commit | `b27f122f918a42485afb300dacb6d547de47bd40` |
| Python | `3.9.6` |
| 执行时间 | `2026-08-20T00:12:48+08:00` 至 `2026-08-20T00:12:57+08:00` |
| LLM Provider | `api.deepseek.com` |
| Model | `deepseek-v4-pro` |
| 模型预检 | `AuthenticationError，HTTP 401` |
| Redis | TCP 可连接 |
| ChromaDB | TCP 可连接 |
| 当前 Intent 模式 | 第三方端点：LLM 85% + Pattern 15%，Embedding 关闭 |
| RAG_MIN_SCORE | 默认 `0.20` |
| RAG_TIMEOUT_SECONDS | 默认 `12` |
| LLM_TIMEOUT_SECONDS | 默认 `45` |
| LLM_MAX_RETRIES | 默认 `2` |

安全说明：报告和结果只记录“密钥是否配置”，没有写出密钥内容。

运行时出现 Chroma telemetry 兼容警告和 macOS LibreSSL 警告，但隔离知识库成功导入 20 个片段并完成 25 条查询；这些警告没有使本轮 Direct Retrieval 中断。

## 3. Current RelayDesk Architecture

根据 `api/main.py` 和实际类调用，单次 `/chat` 的顺序是：

```mermaid
flowchart TD
    A["接收 /chat"] --> B["Memory.get_context：工作记忆、摘要、情景记忆、画像"]
    B --> C["从最近记忆构造 history"]
    C --> D["IntentRecognizer：LLM / 可选 Embedding / Pattern / Fusion"]
    D --> E["RAG gating"]
    E -->|业务意图| F["Query Rewrite → 并行召回 → 去重 → Rerank → Top-K"]
    E -->|寒暄、人工等跳过| G["不检索"]
    F --> H["拼接 Memory prompt 与 Knowledge prompt"]
    G --> H
    H --> I["结构化路由：Primary + 可选 Supporting"]
    I --> J["每个 Agent 构造 system prompt 并动态注入 Skills"]
    J --> K["Agent LLM 执行"]
    K -->|多 Agent| L["Primary synthesis；失败则结构化合并"]
    K -->|单 Agent| M["直接返回"]
    L --> N["写入用户与助手消息到 Working Memory"]
    M --> N
    N --> O["后台任务更新用户画像"]
```

容易混淆的真实顺序：

- Memory 读取发生在 Intent 之前。
- RAG 在路由之前执行。
- Skills 不是全局前置步骤，而是在选定 Agent 后、该 Agent 调 LLM 前注入。
- 多 Agent 先并行执行，再由 Primary Agent synthesis；synthesis 失败时使用结构化拼接。
- Memory 写入发生在最终响应之后，画像更新是异步后台任务。

## 4. Benchmark Methodology

本轮遵循以下方法：

1. 使用当前提交的正式代码作为 Current，不修改正式阈值、Prompt 或控制流。
2. V0/V1 都是测试适配器构造的 Reconstructed Baseline，不代表真实历史。
3. Intent 使用相同 46 条样本，其中 2 条歧义样本不进入主 Accuracy；覆盖现有 19 个枚举、上下文依赖、复合和误导关键词。
4. RAG 使用隔离临时 ChromaDB，自动加载当前 20 篇默认演示知识；不读取或覆盖正式 `data/chroma`。
5. 路由基准固定 gold intent，隔离衡量角色关注点覆盖，不把它当回答质量。
6. Skills 比较全量三 Skill 拼接与当前 `SkillManager.prompt_for`。
7. Tool 用 Fake Tool 注入正常、重复、慢、永久阻塞、异常、连续失败与恢复。
8. 所有不能可靠运行的指标写为 `NOT EXECUTED / 未执行`，并记录原因。

主要限制：数据集由当前知识与路由设计人工构造，样本量小；没有独立第二标注者；模型不可用使含 LLM 的关键比较缺失。

## 5. Reconstructed Baseline Definition

| 领域 | V0 | V1/V2 | Current |
|---|---|---|---|
| Intent | LLM Only | LLM+Pattern；LLM+Embedding | 正式配置对应 Fusion |
| RAG | 原查询直查 Chroma Top-K | Rewrite + 多查询召回，无 Rerank；可选 Rerank-only | Rewrite + 并行召回 + 去重 + Rerank + Top-K |
| Agent | 所有请求给 General | 只执行当前 Primary | Primary + Supporting 并行 + Synthesis |
| Memory | 最近 N 条原始消息 | Working Memory 截断旧历史 | 工作记忆 + Summary + Episodic + Profile |
| Skills | 三个 Skill 全量拼入 | 不适用 | 按 Agent + Keyword 动态注入 |
| Tool | 直接 `await handler()` | 不适用 | Cache + Timeout + Breaker + Fallback |

这些基线用于回答“复杂机制是否解决了简单方案的可观察问题”，不是项目时间线。

## 6. Intent Recognition Ablation

### 6.1 主消融状态

| 版本 | 状态 | 原因 |
|---|---|---|
| V0 LLM Only | NOT EXECUTED / 未执行 | 模型预检 HTTP 401 |
| V1 LLM + Pattern | NOT EXECUTED / 未执行 | 没有有效 LLM 分支输出 |
| V2 LLM + Embedding | NOT EXECUTED / 未执行 | 没有有效 LLM 分支输出 |
| Current Fusion | NOT EXECUTED / 未执行 | 不能用 LLM failure fallback 冒充正常 Current |

### 6.2 可执行的组件诊断

| 组件 | Accuracy | Macro-F1 |
|---|---:|---:|
| Pattern Only | 0.4889 | 0.4968 |
| 本地字符 n-gram Embedding Only | 0.2889 | 0.2520 |
| 当前第三方配置下 LLM 故障 fallback | 0.4889 | 0.4968 |

Pattern 分组 Accuracy：clear `0.6316`、paraphrase `0.3684`、context-dependent `0.3333`、keyword-heavy `0.5556`、fine-grained `0.6667`、composite `0.5000`、misleading-keywords `0.5000`、human-escalation `0.7500`。

这组结果暴露出两个硬问题：

1. 当前模型失败时，第三方端点配置下会退化成 Pattern，44 条样本 Accuracy 不到 0.5；可靠性 fallback 存在，但语义能力明显不足。
2. Embedding 模板仍包含较多旧售后表达，且轻量字符向量在中文企业服务台语义上表现较弱；它不能单独承担分类。

### 6.3 Weight Ablation

`0.9/0.05/0.05`、`0.8/0.1/0.1`、`0.7/0.2/0.1`、`0.6/0.3/0.1`、`0.6/0.2/0.2` 均 `NOT EXECUTED / 未执行`。原因是没有有效 LLM 输出；权重比较若混用失败输出没有意义。

因此当前 70/20/10 和 85/15 都只能描述为**经验初始化**，不能描述为实验最优。

### 6.4 Intent Bad Cases

由于 LLM Only 和 Current 都未执行，本轮没有生成“LLM 错、Current 对”或“LLM 对、Current 错”的真实对照案例。组件级逐样本预测保存在 `intent_ablation.json`，但不升级为设计效果结论。

## 7. RAG Ablation

### 7.1 Direct Retrieval 真实结果

| 版本 | Recall@1 | Recall@3 | Recall@5 | MRR | P50 ms | P95 ms |
|---|---:|---:|---:|---:|---:|---:|
| Direct Retrieval | 0.0909 | 0.3636 | 0.5227 | 0.2303 | 124.672 | 132.412 |
| + Query Rewrite | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED |
| + Rewrite + Rerank | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED |
| Rerank Only | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED |

样本：22 条有答案、3 条无答案。无答案查询在不加拒答阈值的 Direct Top-5 中都返回了内容，比例 `1.0000`，说明“检索有返回”绝不能直接等价于“知识命中”。这也支持当前 API 层对最低分和 fallback 标志进行过滤的必要性。

### 7.2 Direct Retrieval 观察到的坏案例

下列查询的 gold 文档没有进入 Top-5：

| ID | 查询 | Gold | Direct Top-3 摘要 |
|---|---|---|---|
| RAG-001 | 服务台能处理哪些企业问题 | 企业统一服务台使用说明 | 订阅费用、401、密码重置 |
| RAG-003 | 申请状态一直没变化怎么跟进 | 申请进度、处理时效 | 订阅、支付失败、发票申请 |
| RAG-006 | 能登录但是某个资源 forbidden | 403 排查 | 软件崩溃、订阅、异常登录 |
| RAG-008 | 电脑端程序启动后秒退 | 软件崩溃 | 订阅、人工升级、申请流程 |
| RAG-010 | 开票前要填哪些东西 | 发票申请 | 500、软件崩溃、发票抬头 |
| RAG-011 | 票已经开了还能换公司名称吗 | 发票抬头修改 | 服务台说明、401、VPN |
| RAG-012 | 一笔服务出现两条一样的交易 | 重复扣款 | 软件崩溃、异常登录、支付失败 |
| RAG-014 | 退的钱一般几天回原账户 | 退款时效 | 发票抬头、支付失败、异常登录 |
| RAG-018 | 修改账号联系人信息 | 账户资料变更 | 发票抬头、密码重置、异常登录 |
| RAG-024 | 正常登录但财务文件无访问权 | 403 排查 | 异常登录、支付失败、申请流程 |

这些是 Query Rewrite 的**候选设计动机**，不是“Rewrite 已修复”的证据。

### 7.3 Query Rewrite 的真实结论边界

要求中的“至少 5 个 Direct 失败、Rewrite 成功”和“至少 3 个 Rewrite 无收益/变差”均未生成，因为 Rewrite 调用依赖已失效的模型鉴权。当前 `MCPToolManager.rewrite_query` 在失败时会返回原查询；若直接跑完全链路，结果会静默退化成 Direct，形成虚假对比。因此本轮主动阻止了这种误报。

### 7.4 Rerank 的真实结论边界

Rerank 同样未执行。代码层面可以确认它只接收召回后的候选并排序，因此预期主要影响 MRR/Top-1，而不是候选 Recall；实际 before/after rank 案例仍待补测。

## 8. Multi-Agent Ablation

### 8.1 确定性路由证据

| 版本 | 关注点角色覆盖率 | 角色集合完全匹配率 | 平均多余角色数 |
|---|---:|---:|---:|
| Single General | 0.1667 | 0.1667 | 0.8333 |
| Primary Only | 0.7917 | 0.5833 | 0.0000 |
| Current Primary + Supporting | 1.0000 | 1.0000 | 0.0000 |

Current 在 12 条路由样本中覆盖全部标注角色。Primary-only 的缺口集中于 5 条双领域请求，说明 Supporting 不是为了所有请求，而是为了复合请求的第二关注点。

### 8.2 回答质量与延迟

Single General、Primary Only、Current Multi-Agent 的实际回答没有生成，因此 Completeness、Correctness、Relevance、Actionability、P50/P95 和 LLM calls 均 `NOT EXECUTED / 未执行`。

路由 gold 与当前规则高度一致，存在数据集偏置风险。`1.0000` 只能说明“在这组手工角色标注上，代码能路由到目标集合”，不能证明回答质量完美。

### 8.3 为什么 Parallel 与 Synthesis 仍合理

- Parallel：两个专业响应互不依赖时，理论上比串行降低总等待；本轮未测端到端延迟。
- Synthesis：避免直接把两个答案生硬拼接，建立主次；本轮未测信息丢失与冲突。
- 结构化 fallback：synthesis 失败时保留各专业输出，是已有可靠性边界。

## 9. Memory Ablation

当前 Memory 包含：Redis Working Memory、Chroma Episodic Memory、User Profile、Conversation Summary。基准构造了 3 个长对话场景，但只执行 `MemoryContext.to_prompt_text` 的结构投影。

| 指标 | 状态 |
|---|---|
| Context 结构与字符数 | 已执行 |
| Information Retention Rate | NOT EXECUTED / 未执行 |
| Hallucinated Memory Rate | NOT EXECUTED / 未执行 |
| 压缩延迟 | NOT EXECUTED / 未执行 |
| 跨会话偏好保持 | NOT EXECUTED / 未执行 |

未执行原因：摘要压缩和画像提取依赖有效 LLM；仅手工填充 `MemoryContext` 会形成循环论证，不能证明真实系统能找回关键事实。

当前必须承认的 Memory 风险：

1. 错误摘要或画像可能长期污染后续上下文。
2. Episodic 检索与用户画像共用向量基础设施，服务不可用时会退化。
3. 15 条压缩、5 条保留、24h TTL 没有数据支持最优性。
4. 后台画像更新失败不会影响当前响应，但可能降低长期一致性；需要可观测性而非静默忽略。

## 10. Skills Ablation

| 指标 | 全量 Skills | Dynamic Skills |
|---|---:|---:|
| 平均 prompt 字符数 | 3507.00 | 1238.56 |
| 平均无关 Skill 数 | 2.00 | 0.00 |
| 必要 Skill 覆盖率 | 1.0000 | 1.0000 |

平均字符减少率为 `0.6468`。三个 Skills 加载成功，错误列表为空。

已证明：动态加载减少无关规则和上下文占用。未证明：它提高回答准确性或合规性，因为 Rule Compliance 需要实际模型回答。

潜在硬伤：Skill 匹配依赖 Agent 和关键词；无关键词的语义改写可能不命中。当前数据集有意使用可命中的企业服务表达，因此还需要增加“同义但无关键词”的负载测试。

## 11. Tool Reliability Comparison

| 场景 | V0 | Current | 真实观察 |
|---|---|---|---|
| 正常返回 | 直接成功 | 成功，有约 0.263ms 框架开销 | 治理有很小固定开销 |
| 重复参数 | handler 调 2 次 | handler 调 1 次 | 第二次命中缓存 |
| 0.2s 慢请求 | 等待约 201.216ms | 约 52.177ms fallback | 使用 0.05s 故障注入阈值 |
| 永久阻塞 | 无原生保护，需基准外部取消 | 内部 timeout 后 fallback | 保护主链路 |
| handler 异常 | `RuntimeError` 冒泡 | 成功封装 fallback | 隔离下游异常 |
| 连续失败 | 每次继续打下游 | 5 次后 OPEN，第 6 次不调用 | 熔断有效 |
| 恢复 | 无状态机 | HALF_OPEN 探测成功后 CLOSED | 自动恢复有效 |

注意：正式 Tool 默认 timeout 30 秒、breaker 失败阈值 5、恢复窗口 60 秒。本轮只把 timeout/recovery 缩短为 0.05 秒以快速验证状态机，没有证明 30/5/60 是最佳参数。

## 12. Judge Evaluation

已建立 3 组高/中/低质量受控答案，覆盖 401、重复扣款和 500。计划每个答案重复评测 3–5 次并输出 Relevance、Accuracy、Completeness、Helpfulness、方差与范围。

本轮全部 `NOT EXECUTED / 未执行`。原因是模型 HTTP 401。当前 `Evaluator` 在 Judge 异常时返回 0.5 并设置 `judge_failed=True`；本轮没有把这些 fallback 0.5 当真实评分。

Judge 的正确定位是自动化回归信号，不是绝对 ground truth。恢复后还需要检查：

- 高质量回答是否稳定高于中/低质量回答；
- 流畅但虚构后台结果的回答能否被 Accuracy 明显惩罚；
- 同一答案 3–5 次评分范围是否可接受；
- Judge failure 是否从汇总指标中排除。

## 13. Design Motivation Bad Case Catalog

汇总文件共保存 35 条真实观察：

| Feature | 数量 | 证据含义 |
|---|---:|---|
| Direct Retrieval | 10 | Direct Top-5 漏召回；Current 未执行，不能声称修复 |
| Primary / Supporting 路由 | 15 | 两个基线在若干样本漏角色，而 Current 覆盖；同一请求可能对应两个基线案例 |
| Dynamic Skills | 5 | 全量注入有 2 个无关 Skill，Current 为 0 |
| Cache | 1 | 重复 handler 调用减少 |
| Timeout/Fallback | 2 | 慢与永久阻塞得到保护 |
| Fallback | 1 | handler 异常被结构化封装 |
| Circuit Breaker | 1 | 连续失败后阻断并恢复 |

下面逐条解释全部 35 个 Bad Case。这里的“坏案例”包括两类：一类是真实失败，例如 Direct Retrieval 没把 gold 文档召回；另一类是重建基线缺少当前机制时出现的结构缺口，例如 General-only 没有覆盖 technical 角色。它们都不代表曾在线上发生过。

### 13.1 Direct Retrieval：10 个 Top-5 漏召回案例

所有 RAG 案例的 Current Rewrite/Rerank 结果都是 `NOT EXECUTED`。因此下表只证明 Direct Retrieval 存在问题，不能证明当前链路已经修复。

| Bad Case | 输入 | Gold 文档 | Direct Top-5 | 观察与可能原因 |
|---|---|---|---|---|
| BC-RAG-001 | 服务台能处理哪些企业问题 | 企业统一服务台使用说明 | 订阅费用；401；密码重置；人工升级；申请流程 | 宽泛短查询被多个具体主题吸走；需要验证 rewrite 能否补充“服务范围/能力说明”语义 |
| BC-RAG-002 | 申请状态一直没变化怎么跟进 | 申请进度；处理时效 | 订阅费用；支付失败；发票申请；人工升级；申请流程 | 同时包含“进度”和“跟进时效”两个信息需求，单向量没有召回任一 gold |
| BC-RAG-003 | 能登录但是某个资源 forbidden | 403 排查 | 软件崩溃；订阅费用；异常登录；密码重置；申请进度 | 中英混合且用 `forbidden` 替代 403；相邻登录文档造成干扰 |
| BC-RAG-004 | 电脑端程序启动后秒退 | 软件崩溃 | 订阅费用；人工升级；申请流程；支付失败；密码重置 | “秒退”是“闪退/崩溃”的口语同义词，当前 embedding 没建立足够强的对应 |
| BC-RAG-005 | 开票前要填哪些东西 | 发票申请 | 500；软件崩溃；发票抬头；订阅费用；申请流程 | 查询很短且没有“发票申请”等完整术语，只出现“开票”口语表达 |
| BC-RAG-006 | 票已经开了还能换公司名称吗 | 发票抬头修改 | 服务台说明；401；VPN；支付失败；异常登录 | 组合条件“已开具 + 公司名称修改”没有映射到“抬头修改/作废重开” |
| BC-RAG-007 | 一笔服务出现两条一样的交易 | 重复扣款 | 软件崩溃；异常登录；支付失败；500；发票抬头 | 查询有意避开“重复扣款”，Direct 没识别“两条一样的交易”这一同义表达 |
| BC-RAG-008 | 退的钱一般几天回原账户 | 退款时效 | 发票抬头；支付失败；异常登录；500；人工升级 | “退的钱”与“退款时效”术语不一致，且时间意图没有被正确聚焦 |
| BC-RAG-009 | 修改账号联系人信息 | 账户资料变更 | 发票抬头；密码重置；异常登录；人工升级；订阅费用 | “联系人信息”是账户资料的短表达，与发票抬头修改产生相似的“信息变更”噪声 |
| BC-RAG-010 | 可以登录，但财务文件无访问权，不是密码错误 | 403 排查 | 异常登录；支付失败；申请流程；软件崩溃；密码重置 | 查询明确排除登录问题，但向量仍被“登录/财务”表面词吸引；否定语义处理不足 |

对这 10 个案例，后续补测必须保存：rewrite queries、去重前后候选、gold 在 rewrite 前后的 rank、rerank 前后 rank、延迟和 LLM calls。只有 gold 从 Direct Top-5 外进入 rewrite Top-5，才能写成“Query Rewrite 修复”；只有 gold 已在候选中且 rank 上升，才能写成“Rerank 改善排序”。

### 13.2 Primary / Supporting Routing：15 个基线缺口

这 15 条来自 10 个请求；同一个复合请求可能同时形成一条 General-only 缺口和一条 Primary-only 缺口。

| Bad Case | 输入 | 必要角色 | 失败基线及其角色 | Current 角色 | 解释 |
|---|---|---|---|---|---|
| BC-ROUTE-001 | 登录报 401 | technical | General-only → general | technical | General-only 没进入技术排查角色 |
| BC-ROUTE-002 | 页面一直返回 500 | technical | General-only → general | technical | 500 应由技术角色处理 |
| BC-ROUTE-003 | 如何修改发票抬头 | billing | General-only → general | billing | 发票制度与财务边界需要 billing |
| BC-ROUTE-004 | 为什么出现重复扣款 | billing | General-only → general | billing | 真实流水核验边界属于 billing |
| BC-ROUTE-005 | 401 且重复扣款 | technical + billing | General-only → general | technical + billing | General-only 同时漏掉两个专业方向 |
| BC-ROUTE-006 | 401 且重复扣款 | technical + billing | Primary-only → technical | technical + billing | Primary 能排查登录，但漏掉扣款核验 |
| BC-ROUTE-007 | 页面 500 且支付扣了两遍 | technical + billing | General-only → general | technical + billing | 请求包含故障与费用两类关注点 |
| BC-ROUTE-008 | 页面 500 且支付扣了两遍 | technical + billing | Primary-only → technical | technical + billing | Primary-only 漏掉重复支付部分 |
| BC-ROUTE-009 | 订阅扣费后客户端崩溃 | billing + technical | General-only → general | billing + technical | 费用事件与客户端故障需要分别处理 |
| BC-ROUTE-010 | 订阅扣费后客户端崩溃 | billing + technical | Primary-only → billing | billing + technical | billing 为主，但需要 technical 补充崩溃排查 |
| BC-ROUTE-011 | 投诉并要求转人工 | escalation | General-only → general | escalation | 明确人工请求不能继续作为普通 General 问答 |
| BC-ROUTE-012 | 账号疑似被盗且有陌生扣款 | technical + billing | General-only → general | technical + billing | 账户安全与费用风险必须同时覆盖 |
| BC-ROUTE-013 | 账号疑似被盗且有陌生扣款 | technical + billing | Primary-only → technical | technical + billing | technical 负责安全处置，billing 补充陌生扣款核验 |
| BC-ROUTE-014 | 先讲开票流程，再讲 500 信息收集 | billing + technical | General-only → general | billing + technical | 两个明确诉求分属不同专业角色 |
| BC-ROUTE-015 | 先讲开票流程，再讲 500 信息收集 | billing + technical | Primary-only → billing | billing + technical | Primary-only 只覆盖开票，漏掉 500 排查 |

这些案例能证明的是“路由角色集合更完整”。它们不能证明最终答案更完整，因为 Agent 回答和 synthesis 没有执行。后续需要逐条检查 Supporting 是否产生冲突、Primary synthesis 是否遗漏专业信息，以及单领域请求是否因为误加 Supporting 增加无收益成本。

### 13.3 Dynamic Skills：5 个无关规则注入案例

| Bad Case | 输入 | V0 无关 Skill 数 | Current 无关 Skill 数 | 具体含义 |
|---|---|---:|---:|---|
| BC-SKILL-001 | 企业统一服务台能做什么 | 2 | 0 | 全量方式会额外注入 technical 与 billing 规则；Current 只选 general |
| BC-SKILL-002 | 我要投诉并转人工 | 2 | 0 | 全量方式会携带无关技术/费用 SOP；Current 只保留综合服务与升级边界 |
| BC-SKILL-003 | 企业账号登录报 401 | 2 | 0 | 全量方式会混入 general 与 billing；Current 只注入 technical_support |
| BC-SKILL-004 | 页面出现 500，怎么安全排查 | 2 | 0 | Current 只保留技术低风险、可逆排查与人工升级规则 |
| BC-SKILL-005 | VPN 证书告警怎么办 | 2 | 0 | Current 避免把费用与通用服务规则带入安全排查 |

这 5 条的 Measured Effect 是 prompt 结构变化，不是回答质量。平均字符数从 3507 降到 1238.56，必要 Skill 覆盖仍为 1.0；但“无关键词同义表达是否漏装 Skill”还没有单独形成负向数据集。

### 13.4 Tool Reliability：5 个故障注入案例

| Bad Case | 故障场景 | V0 问题 | Current 机制 | 实测效果 | 参数边界 |
|---|---|---|---|---|---|
| BC-TOOL-001 | repeated-parameters | 相同参数直接调用两次会执行 handler 两次 | TTL Cache | handler 调用从 2 次降到 1 次，第二次返回同一序号 | 没有验证跨进程缓存或缓存失效一致性 |
| BC-TOOL-002 | slow-request | V0 等待 0.2 秒 handler 完成 | Timeout + Fallback | 使用 0.05 秒注入阈值时约 50ms 返回 fallback | 正式默认 30 秒未做最优性验证 |
| BC-TOOL-003 | never-ending-request | V0 没有原生退出条件，只能由基准外部取消 | Native Timeout + Fallback | Current 自行结束并返回结构化降级结果 | timeout 过短可能误杀正常慢请求 |
| BC-TOOL-004 | handler-exception | `RuntimeError` 直接冒泡 | Exception Wrapper + Fallback | Current 将异常转成可识别的 fallback | 上层必须区分 fallback 与真实业务成功 |
| BC-TOOL-005 | consecutive failures | 持续故障会反复打到下游 | 5 次失败后 OPEN，恢复窗口后 HALF_OPEN | 第 6 次没有调用 handler；恢复探测成功后 CLOSED | 5 次/60 秒是经验配置，本轮只验证状态机 |

### 13.5 没有生成 Bad Case 的部分

- Intent Fusion：LLM Only 与 Current 均未执行，所以没有真实的“LLM 错、Current 对”或“Current 反而变差”案例。
- Query Rewrite：没有有效 rewrite queries，所以没有“Direct 失败、Rewrite 成功”或“Rewrite query drift”案例。
- Rerank：没有 before/after rank，所以没有“升排”或“错降”案例。
- Multi-Agent 回答：没有实际回答与 Judge，所以没有漏答、冲突、synthesis 丢信息案例。
- Memory：没有真实压缩和跨会话检索，所以没有事实遗忘、错误画像或历史污染案例。
- Judge：没有重复评分，所以没有排序失败和方差过大案例。

完整机器可读明细见英文文件 `evaluation/results/design_rationale/bad_cases.json`。

## 14. Reconstructed Design Evolution

以下是**逻辑演进重建**，不是项目历史陈述：

### Intent

1. LLM Only：语义强，但成本、延迟、可用性和粗细粒度不稳定。
2. + Pattern：对 401、发票、退款等细粒度强信号提供低成本修正和 LLM 故障兜底。
3. + Embedding：尝试覆盖无关键词同义表达。
4. + Fusion：在分支互补时聚合，但权重需要校准。

本轮只证明 Pattern/Embedding 的独立能力有限，没有证明 Fusion 优于 LLM Only。

### RAG

1. Direct：简单、低调用数，但本轮 Recall@5 仅 0.5227。
2. + Rewrite：设计上拆分术语差异和多信息需求；未执行。
3. + Rerank：设计上改善候选顺序；未执行。
4. + score/fallback filter：防止“有返回即命中”；无答案样本的 Direct 返回率 1.0 支持这个边界。

### Agent

1. General-only：简单，但角色覆盖只有 0.1667。
2. Primary-only：单领域足够，整体覆盖 0.7917。
3. Primary + Supporting：复合场景角色覆盖 1.0。
4. Synthesis：设计上消解拼接与主次问题；效果未执行。

### Skills

1. 全量规则：必要规则全覆盖，但每次带入 3507 字符和 2 个无关 Skill。
2. Dynamic：平均字符减少 64.68%，必要 Skill 仍覆盖。

### Tool

1. 直接调用：简单，但无缓存、超时、熔断和降级。
2. 生命周期治理：本轮故障注入证明各机制按预期工作。

## 15. Current Design Strengths

1. **控制流边界清晰**：Intent、RAG gating、结构化路由、Agent、Skills、Memory 写回有明确顺序。
2. **复合请求有主辅结构**：不是无主次地调用所有 Agent。
3. **Skills 可热加载且按需注入**：本轮有直接 prompt 长度证据。
4. **Tool 可靠性完整**：缓存、超时、熔断、fallback 都通过故障注入。
5. **RAG fallback 不作为真实命中**：这是必要的语义正确性边界。
6. **失败时不声称后台操作已完成**：Agent 提示词与演示知识均强调真实系统边界。

## 16. Current Design Trade-offs

1. **复杂度与证据不对称**：Intent Fusion、Rewrite/Rerank、Memory、Multi-Agent/Judge 都增加复杂度，但本轮缺少 LLM 侧效果数据。
2. **提供方影响架构形态**：第三方 `base_url` 会关闭 Embedding，使“当前三路融合”在实际环境中并不存在。
3. **RAG 中文基础召回偏弱**：当前默认 `all-MiniLM-L6-v2` Direct 结果较差；Rewrite 可能缓解，但不能代替适合中文/多语种的 embedding 选型验证。
4. **规则对关键词敏感**：Intent Pattern、路由复合检测和 Skills 选择都依赖词表，否定、金额和错误码可能产生误判。
5. **Synthesis 额外增加一次 LLM 调用**：可能提升整合，也可能丢失专业细节或增加延迟。
6. **Memory 错误会放大**：错误摘要和画像可能跨轮次持续影响答案。
7. **Tool fallback 语义需上层识别**：成功返回 fallback 不等于业务成功；当前 RAG 已过滤，但其他未来工具也必须遵守。

## 17. Designs With Strong Experimental Evidence

### 17.1 Dynamic Skills 的结构收益

- 平均字符：3507 → 1238.56。
- 减少率：64.68%。
- 无关 Skill：2.0 → 0。
- 必要 Skill 覆盖：1.0。

证据范围：prompt 构造，不含答案质量。

### 17.2 Primary + Supporting 的角色覆盖收益

- General-only：0.1667。
- Primary-only：0.7917。
- Current：1.0000。

证据范围：固定 gold intent 下的角色集合，不含回答质量。

### 17.3 Tool 生命周期治理

Cache、Timeout、Fallback、Breaker 和 Recovery 均通过 Fake Tool 故障注入。证据范围：控制流和状态机，不含真实外部系统容量。

### 17.4 “检索有结果不等于知识命中”

3 条无答案查询的 Direct Top-5 返回率是 1.0。这个结果强烈支持最低分过滤、fallback 过滤和 `knowledge_used` 语义边界。

## 18. Designs Still Mainly Based on Engineering Heuristics

| 设计/参数 | 当前状态 | 为什么仍是 heuristic |
|---|---|---|
| Intent Fusion 是否优于 LLM Only | 未证明 | LLM 401 |
| 70/20/10 与 85/15 | 未校准 | Weight Ablation 未执行 |
| confidence 0.5 | 未校准 | 没有 threshold curve |
| Query Rewrite | 有动机、无效果证据 | Direct 差，但 Rewrite 未执行 |
| Rerank | 机制合理、无效果证据 | 无 before/after rank |
| RAG score 0.20 / Top-K | 未校准 | 无 precision-recall/拒答曲线 |
| supporting score 0.45 / ratio 0.55 | 仅小样本吻合 | 路由数据集与规则同源 |
| Multi-Agent Synthesis | 未证明 | 回答/Judge 未执行 |
| Memory 15/5/24h | 未证明 | 没有长对话保留率 |
| Tool 30s / 5 failures / 60s | 机制通过、参数未优化 | 故障注入使用缩短时间 |
| Judge 阈值与稳定性 | 未证明 | Judge 未执行 |

## 19. Interview Story Candidates

以下话术都应明确称为“通过重建基线验证当前设计”，不能说成真实历史线上演进。

### Story A：为什么需要 Primary / Supporting

#### 背景

企业服务台请求可能同时包含技术与费用诉求。

#### Baseline

Single General；或只执行当前 Primary。

#### Bad Case

“登录报 401，而且还被重复扣款”需要 technical 和 billing 两个关注点。

#### Root Cause

单角色只覆盖一个专业方向。

#### Design Choice

结构化主辅路由，仅在复合领域触发 Supporting。

#### Why

避免所有请求都广播，同时补齐第二关注点。

#### Result

12 条角色基准：General 0.1667、Primary-only 0.7917、Current 1.0000。

#### Trade-off

多一次 Agent 调用和可能的 synthesis 调用；回答质量与延迟仍待补测。

#### 面试可能追问

“gold 怎么标？”“为什么阈值是 0.45？”“Supporting 冲突怎么办？”回答时应承认阈值未校准，当前只有小样本角色覆盖证据。

### Story B：为什么动态加载 Skills

#### 背景

General、Technical、Billing 有不同 SOP 和人工边界。

#### Baseline

把三个 Skill 全部放进每次 system prompt。

#### Bad Case

技术请求同时携带发票/退款规则，增加无关上下文。

#### Root Cause

全量注入不区分 Agent 与关键词。

#### Design Choice

沿用 SkillManager，根据 Agent + Keyword 动态选择。

#### Why

减少 prompt 占用与跨领域规则污染。

#### Result

平均字符减少 64.68%，平均无关 Skill 从 2 降到 0，必要覆盖为 1.0。

#### Trade-off

关键词漏匹配可能导致必要 Skill 缺失；Rule Compliance 未测。

### Story C：为什么 Tool 需要完整生命周期

#### 背景

知识检索或未来内部工具可能变慢、报错、阻塞或持续故障。

#### Baseline

直接 `await handler()`。

#### Bad Case

永久阻塞没有原生退出；连续故障反复打到下游；重复参数浪费调用。

#### Root Cause

缺少调用边界和状态管理。

#### Design Choice

Cache + Timeout + Breaker + Fallback。

#### Why

保护主链路并让失败可预测。

#### Result

缓存减少一次重复调用；timeout 返回 fallback；5 次失败后第 6 次被阻断；恢复探测后关闭熔断。

#### Trade-off

fallback 可能隐藏真实失败，必须带标志；参数过松或过严都会造成问题。

### Story D：为什么 RAG 需要 Rewrite/Rerank（谨慎版）

#### 背景

中文口语、术语不一致和复合查询对 Direct embedding 不友好。

#### Baseline

Original Query → ChromaDB Top-K。

#### Bad Case

22 条有答案样本中 Direct Recall@5 为 0.5227，出现 10 个 Top-5 漏召回。

#### Root Cause

可能包括 embedding 语言适配、短查询信息不足和多意图向量混合；本轮尚未分别归因。

#### Design Choice

当前代码使用 Query Rewrite、多查询召回、去重、Rerank。

#### Why

机制上 Rewrite 扩大候选视角，Rerank 改善顺序。

#### Result

**NOT EXECUTED / 未执行**：不能说当前链路已提升 Recall 或 MRR。

#### Trade-off

增加 LLM 调用、延迟、token 成本和 query drift 风险。

## 20. Metrics Suitable for Interview Discussion

可以直接讨论：

- 路由关注点角色覆盖率：0.1667 / 0.7917 / 1.0000。
- Dynamic Skills 平均 prompt 字符减少率：64.68%。
- Direct RAG：Recall@1 0.0909、Recall@3 0.3636、Recall@5 0.5227、MRR 0.2303、P95 132.412ms。
- 无答案 Direct Top-5 返回率：1.0，说明需要命中语义过滤。
- Tool：重复调用 2→1；5 次失败后第 6 次不再打 handler；恢复后 CLOSED。

讨论时必须同时给限制：样本小、人工标注、路由数据集与规则同源、模型 401、端到端质量未执行、延迟只代表本机隔离环境。

不适合讨论为“效果提升”的指标：Pattern-only Accuracy、Embedding-only Accuracy。它们只是组件诊断，没有 LLM-only 对照。

## 21. Reproduction Commands

从仓库根目录执行：

```bash
TMPDIR=/tmp \
PYTHONPYCACHEPREFIX=/tmp/relaydesk-design-pycache \
TOKENIZERS_PARALLELISM=false \
python3 evaluation/benchmark/run_design_rationale.py
```

验证脚本、Python 核心文件和结果 JSON：

```bash
PYTHONPYCACHEPREFIX=/tmp/relaydesk-design-pycache \
python3 -m py_compile \
  evaluation/benchmark/run_design_rationale.py \
  api/main.py \
  core/intent_recognizer.py \
  core/skill_loader.py \
  agents/agent_orchestrator.py \
  mcp/tool_manager.py \
  mcp/knowledge_base.py \
  memory/conversation_memory.py \
  monitor/performance_monitor.py \
  evaluation/evaluator.py

for file in evaluation/results/design_rationale/*.json; do
  python3 -m json.tool "$file" >/dev/null
done
```

补跑完整 LLM 消融前，先修复 `.env` 中模型与密钥，使最小 `messages.create` 不再返回 401。补跑后仍应检查 `judge_failed`，不能把 fallback 分数计入 Judge 指标。

## 22. Raw Results Appendix

### 结果文件

- `evaluation/results/design_rationale/intent_ablation.json`
- `evaluation/results/design_rationale/intent_weight_ablation.json`
- `evaluation/results/design_rationale/rag_ablation.json`
- `evaluation/results/design_rationale/routing_ablation.json`
- `evaluation/results/design_rationale/multi_agent_ablation.json`
- `evaluation/results/design_rationale/memory_ablation.json`
- `evaluation/results/design_rationale/skills_ablation.json`
- `evaluation/results/design_rationale/tool_reliability.json`
- `evaluation/results/design_rationale/judge_reliability.json`
- `evaluation/results/design_rationale/bad_cases.json`

### 数据集

- `evaluation/datasets/intent_design_rationale.json`：47 条，45 条进入主组件诊断，覆盖 19 个现有意图。
- `evaluation/datasets/rag_design_rationale.json`：25 条，22 条有答案、3 条无答案。
- `evaluation/datasets/multi_agent_design_rationale.json`：12 条角色关注点样本。
- `evaluation/datasets/memory_design_rationale.json`：3 个长对话结构场景。
- `evaluation/datasets/skills_design_rationale.json`：9 条动态注入样本。
- `evaluation/datasets/judge_design_rationale.json`：3 组高/中/低受控答案。

### 执行状态总表

| 文件 | 状态 |
|---|---|
| intent_ablation.json | PARTIALLY EXECUTED / 部分执行 |
| intent_weight_ablation.json | NOT EXECUTED / 未执行 |
| rag_ablation.json | PARTIALLY EXECUTED / 部分执行 |
| routing_ablation.json | EXECUTED / 已执行 |
| multi_agent_ablation.json | NOT EXECUTED / 未执行 |
| memory_ablation.json | PARTIALLY EXECUTED / 部分执行 |
| skills_ablation.json | PARTIALLY EXECUTED / 部分执行 |
| tool_reliability.json | EXECUTED / 已执行 |
| judge_reliability.json | NOT EXECUTED / 未执行 |
| bad_cases.json | EXECUTED / 已执行 |

中文报告负责完整解释测试设计、指标、限制与全部 Bad Case；数据集 schema、基准脚本、结果字段、状态说明和机器可读结论均为英文。JSON 中保留的中文仅是被测用户语句、知识标题、知识正文和候选回答，因为它们属于 RelayDesk 中文业务语料，而不是工件说明语言。
