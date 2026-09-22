# 模型与受控工具接入（M04）

本阶段交付可装配的模型协议、工具网关和手动推进会话，并用离线测试验证它们与真实资料库、检索、证据回读的连接。未调用真实模型或验收分析效果，没有自动循环运行、追问恢复或结果持久发布。现有 HTTP 服务仍返回 `analysis_enabled: false`，不会因为配置了环境变量就偷偷开启模型调用。

## 先运行无密钥演示

```powershell
python -m pip install -e ".[api,documents,model,dev]"
python -m juli model-demo --directory scenarios
```

该命令使用 `minimal@0.1.0` 场景、合成资料和临时数据库。脚本模型按预设步骤发出检索请求，根据实际检索结果选择证据 ID，再请求回读并提供固定格式的合成回答。网关、数据库读取、权限和引用校验均为真实实现，模型决策本身是测试脚本。

预期输出包括 `mode: offline_scripted_integration`、`scripted_model_calls: 3`、`real_model_calls: 0`、`network_calls: 0`、`result_contract_valid: true` 和 `task_status: prepared`。验证结束清理临时库，不创建真实用户，不读取环境密钥。

## 一次会话怎样推进

`bootstrap.create_model_session(store, token, workspace_id, task_id, provider)` 用同一服务端范围装配本地工具与模型会话。主体必须是该空间所有者或编辑者，任务必须为 `prepared` 或 `running`；所有绑定资料必须仍有效。

1. `advance()` 只请求一次模型，返回一个工具提案，或经过引用与结构校验的结果。
2. 工具提案只能通过 `execute_pending(call_id)` 执行。传入其他 ID、重复执行或未知工具都会被拒绝。
3. 工具执行结果以 `tool` 角色加入会话，下一次 `advance()` 再请求模型。
4. 最终 JSON 必须符合 `AnalysisResult` 和该任务固定的场景结果结构；引用通过服务端数据库重新解析，必须有当前主体在此任务的回读记录。

本节的旧 `ModelSession` 是连接测试工具。会话串行锁、消息、预算与事件元数据都在内存中。成功返回的结果带 `persisted: false`，不会自动把数据库任务改成 `completed`。M05 已另行实现 [持久运行器](runtime.md)，生产装配应走该入口；持久任务不能重新创建旧会话。

任何调用失败都会关闭当前会话，未知结果不自动重试。重新创建会话会重置内存计数，不能把它用于绕过未知外部调用的核对，也不能将其称为任务全生命周期的费用保障。当前没有 HTTP 会话创建端点。

## 模型协议与上下文

核心 `ModelProvider` 接收 `ModelRequest`，返回带 usage 的 `ModelReply`，只允许“一个工具请求”或“最终文本”二选一。核心不依赖 HTTPX、供应商 SDK、LangChain 或 LangGraph。

上下文分为：

| 位置 | 内容 | 来源 |
|---|---|---|
| 第一条 system | 平台边界、引用要求、结果外壳 schema | 框架 |
| 第二条 system | 场景指导和场景结果 schema | 部署者安装、任务固定的受信包 |
| user | 本次问题、任务 ID、绑定版本的名称/类型/角色目录 | 明确标为不可信用户数据 |
| assistant 工具提案 | 模型返回的工具名称、参数和调用 ID | 模型，必须经网关检查 |
| tool | 作用域检索候选或经授权回读的原文 | 不可信资料数据，不提升成系统指令 |

初始上下文不装入所有文件正文。后续只加入本会话实际请求的候选和证据，不读取其他空间或未绑定资料。不会把模型判断写回背景库，也不生成自动长期记忆。上下文超限会报错，当前不做静默截断、自动摘要或压缩。

上述角色分层与系统提示降低混淆，但不是提示注入防护的证明。真正执行的硬边界是权限检查、任务绑定、工具白名单、参数校验和外发开关；结论质量还需要真实模型和场景评测。

## 工具网关

工具可见范围取“场景声明 ∩ 平台授予”，注册工具并不自动授权。首版装配的执行器仅为 `evidence.search@1.0.0` 和 `evidence.read@1.0.0`，没有 shell、任意文件读写或网络抓取。

网关校验输入和输出 JSON Schema；模型不能在参数里夹带主体 ID、其他工作空间、扩大预算或开启外发。写入型、外发型或需要确认的工具当前一律拒绝，没有用一个模型字段代替用户确认的路径。证据回读会写入内部读取凭证，这属于读取审计副作用，不是允许模型改写业务资料。

每次模型和工具动作前后重查当前权限、任务状态版本和全部绑定资料。资料撤下、令牌撤销或任务取消后，随后返回的响应会被丢弃；无法追回已经发送给模型的内容。旧内存会话不是并发执行协调器；M05 持久运行器已把最终结果校验与状态提交纳入同一事务。

工具返回最多 64 KiB。SQLite 查询使用 deadline-aware busy timeout、进度回调和提交前检查；超时会回滚事务。Python 解析/哈希等操作采用前后时限检查，不能抢占任意阻塞的插件代码，不能声称硬实时沙箱。额外安装 Python 扩展意味着信任其代码。

## 预算和失败处理

任务快照已有的 `max_actions`、`max_input_tokens`、`max_output_tokens` 用于当前会话的限制。模型请求和工具执行各消耗一次行动；`SessionPolicy` 另设模型调用次数、单次输出上限和总时限，默认 8 次模型请求、每次最多 1,024 输出 tokens、120 秒。

输入采用 UTF-8 字节数加消息/工具开销的保守准入单位，随后结合服务返回的 usage 计费数补充检查。**它不是供应商 tokenizer，也不是人民币/美元费用上限**。输入字段名虽延续 `max_input_tokens`，实际准入可能比供应商 token 计数严格；具体模型的上下文窗口必须显式配置。

请求发出前预留预算；未知结果保留本次输入/输出预留且关闭会话。成功返回后按 usage 结算输出，输入至少扣除预留单位；供应商报告超出额度时丢弃响应。已发生的外部消耗无法撤销。所有计数只在当前对象内有效，不跨新会话或进程重启。

当前模型适配器不做自动重试：HTTP 429/5xx、连接或读超时都交由调用者核对；不因“只读模型请求”就默认不会重复收费。异常不回显服务错误正文、凭据或请求文本。HTTP 传输使用阶段超时并检查总经过时间，不保证任意网络阻塞都能被精确按墙钟时限抢占。

## 首个 HTTP 模型适配器

`ChatCompletionsProvider` 使用显式 HTTPS `/chat/completions` 目标，提供文本与单函数调用的受限兼容路径。需安装 `.[model]`；没有默认模型、默认服务地址、密钥自动发现或自动供应商切换。

配置示意如下，仅构造对象，不发送请求：

```python
from juli.adapters.chat_completions import ChatCompletionsProvider, ChatModelConfig

config = ChatModelConfig(
    endpoint="https://your-approved-provider.example/v1/chat/completions",
    model="your-verified-model-id",
    context_window=32000,  # 根据实际模型资料设置，不是框架承诺
)
# api_key 由受信调用方传入；不要写进仓库、日志或模型消息。
provider = ChatCompletionsProvider(config, api_key=api_key)
```

默认调用会拒绝外发。真正使用前，需要先确认具体供应商、模型、要发送的资料与费用范围；之后由受信程序传入与配置完全一致的 `approved_endpoint`，并为本次会话提供 `SessionPolicy(allow_external_egress=True)`。任何一个开关缺失都会拒绝调用，模型不能修改它们。当前实现只支持 HTTPS 443，不接收携带用户密码、查询密钥或 fragment 的 URL，也不适配本地明文 HTTP 推理端点。

发送内容包括场景指导、问题、绑定资料目录，以及此前检索/回读取得的片段。API key 只进该目标的 Authorization 头；拒绝重定向，不采用环境代理，不将请求或错误正文写到应用日志。部署者仍要检查供应商的数据处理规则、网络与日志设置。

协议使用 `max_completion_tokens`、`stream: false`、`n: 1`、`store: false`，并关闭并行工具请求。工具的公开名称通过稳定别名映射到供应商函数名称；工具返回使用匹配的 `tool_call_id`。依据 [OpenAI 官方 Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 和 [函数调用文档](https://developers.openai.com/api/docs/guides/function-calling) 实现并用模拟 HTTP 验证，未声称所有兼容服务支持同样字段或具有同样的数据保留策略。

响应最大 1 MiB，必须有一致的整数 usage，并且只返回一个最终文本或单个工具调用。拒绝并行调用、未知函数名、拒答、截断、混合工具/文本、非法 JSON 或缺失 usage。最终文本不使用供应商强制 JSON 模式，由框架解析与校验，不接受 Markdown 包裹或自动“修复”结果。

尚未实现流式输出、Responses 协议、图像/音频、多模态、供应商特有推理字段、工具并行或真实模型回归评测。兼容协议验证与实际模型能力验收是两件事。

M06 已新增 [工作台和单动作执行 API](workbench.md)。服务端使用 RuntimeConfig 显式装配模型，网页不能传入模型配置或外发授权；默认关闭执行，--demo-model 仅为离线预设流程。
