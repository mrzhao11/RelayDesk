# RelayDesk 最小场景迁移与硬伤修复说明

## 1. 迁移目标

本次改造把项目从偏电商售后客服的演示定位，最小范围调整为“企业统一服务台”。产品名最终由 EchoMind 更名为 RelayDesk。

保持不变的部分：

- 不迁移 OnCall，不改变现有 Python 架构。
- 不新增 Agent，不新增或删除 `IntentCategory`。
- 不修改 `/chat` 等现有 API 的请求和响应字段。
- 不新增业务 Tool，继续只使用 `knowledge_search`。
- 不接入真实 OA、IAM、财务或工单系统。
- 保留 `order_status`、`logistics` 等历史兼容意图。

## 2. 最小场景迁移是怎么改的

### 产品与 Agent 包装

- 将页面、README、API 标题、Docker 服务和运行脚本统一更名为 RelayDesk。
- 保留内部 Agent 值 `general`、`technical`、`billing`、`escalation`。
- 只调整 General、Technical、Billing 的 system prompt，使其分别承担综合服务、技术支持、费用与结算职责。

### Skills

- 保留原目录和动态加载机制。
- `general_customer_service` 改为企业综合服务接待规范。
- `technical_support` 增加企业账号、401/403/500、权限与安全升级边界。
- `billing_support` 改为企业费用与结算规范。
- `/skills/reload` 仍可使用，但现在需要管理密钥。

### 知识库与 RAG

- KnowledgeBase 结构、`title + content` 导入格式和 500 字切片机制保持不变。
- 默认知识扩充到 20 篇企业服务演示文档，覆盖综合服务、账户技术、费用结算。
- 演示内容明确声明不代表真实企业制度，并包含适用场景、处理步骤、人工升级条件。
- 寒暄和人工升级不触发 RAG。
- `fallback=true` 不注入 Prompt，也不计为 `knowledge_used`。
- 本轮进一步增加 `RAG_MIN_SCORE`，低相关度结果同样不计为有效知识命中。

### Evaluation 与前端

- 保留 Accuracy、Macro-F1、LLM-as-Judge 和回归检测实现，只替换默认样本。
- 默认评测包含 18 条意图样本和 9 个对话场景。
- 前端更新欢迎语、示例问题、Agent 展示名称和知识上传说明。
- 前端与后端合并为一个仓库，目录为 `RelayDeskFrontend`。

## 3. 本轮硬伤修复

- 账户资料意图路由到 GeneralAgent，账户安全意图路由到 TechnicalAgent，不再误入 BillingAgent。
- 技术与费用复合请求仍并行执行，随后由主 Agent 做受约束的统一整合；整合失败时使用结构化降级答案。
- Agent 调用加入超时、有限重试配置；全部 Agent 失败时明确说明未执行后台操作并触发人工升级标记。
- RAG 加入相关度阈值和检索超时，fallback 与低分结果都不会伪装成真实知识命中。
- 管理接口使用 `X-RelayDesk-Admin-Key`，未配置密钥时默认关闭，避免匿名写知识或运行高成本评测。
- CORS 改为可配置白名单，不再默认 `*`。
- 前端默认使用仓库实际提供的 Python 后端，并修复 Python `/search` 的 `top_k` 参数名。
- 管理密钥只保存在当前页面内存，不写入 localStorage。

## 4. 验证方式

不依赖真实模型的核心测试：

```bash
python -m unittest discover -s tests -v
```

配置有效模型 Key 并启动服务后，运行八个最小验收场景：

```bash
python scripts/smoke_acceptance.py --base-url http://localhost:8000
```

管理接口示例：

```bash
curl -X POST http://localhost:8000/skills/reload \
  -H "X-RelayDesk-Admin-Key: $RELAYDESK_ADMIN_KEY"
```
