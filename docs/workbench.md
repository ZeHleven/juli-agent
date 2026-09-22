# 本地工作台与执行 API（M06）

工作台随 Python 包安装，不依赖前端构建、CDN 或在线字体。数据库模式下打开 `http://127.0.0.1:8790/` 即可使用。静态页面公开可读，资料、身份和执行接口仍需要有效个人令牌。

## 开始体验

先安装依赖并显式创建据理独立数据库：

```powershell
python -m pip install -e ".[api,documents,model,dev]"
python -m juli db migrate --database .local/juli.sqlite3
$identity = python -m juli identity create --database .local/juli.sqlite3 --name "Local owner" | ConvertFrom-Json
```

`$identity.token` 是新创建的个人访问令牌，将它粘贴到工作台的令牌输入框。不要把它放进网址、截图、仓库或共享日志。

默认启动只管理资料和建立任务，模型执行关闭：

```powershell
python -m juli serve --database .local/juli.sqlite3 --directory scenarios --port 8790
```

如果想先体验完整操作流程，显式启用离线演示：

```powershell
python -m juli serve --database .local/juli.sqlite3 --directory scenarios --port 8790 --demo-model
```

页面始终显示“离线流程演示”。此演示仅支持 `minimal` 自定义资料分析模板：先追问一个关键词，再按关键词检索、回读并引用资料，未命中时返回限制说明。它是确定性的操作夹具，不做真实分析，也没有外部网络或费用。

三个场景的自动化演示另见 [M08 场景示例](../examples/scenario_acceptance/README.md)。它通过进程内 HTTP 客户端验收，并没有扩大 `--demo-model` 的网页支持范围。

1. 创建工作空间和会话。
2. 上传 TXT、Markdown、DOCX、文本 PDF，或保存一段背景文字。只有解析成功的资料可绑定到任务。
3. 选择场景，按它声明的角色勾选资料版本，输入问题并建立任务。
4. 点击“开始 / 继续”，页面逐个推进动作，直到完成、停止或需要补充。
5. 回答追问并提交，再点击“开始 / 继续”。结果按事实、推断、建议、限制分别呈现。
6. 点击引用查看原文、版本和位置，可下载对应版本原件。切换会话与任务可读取已保存的结果。

任务创建、开始和继续是分开的操作。重新打开或刷新页面不会擅自继续模型调用；重新连接并选回任务后，由你点击继续。关闭页面只停止后续页面调度，已发出的服务端动作可能仍然完成。取消任务可拦截之后返回的结果，但不能撤回已经外发的内容。

## 权限与数据边界

- 令牌只保存在当前页面内存，不写入 localStorage、sessionStorage、Cookie 或 URL；断开连接清除页面资料，刷新后重新输入。
- 空间只读成员可以浏览资料目录；上传、创建会话、建立与运行任务需要写权限。工作台任务列表仅显示当前主体创建的任务，执行详情与追问由创建者管理。
- 页面正文通过纯文本 DOM 呈现，不渲染上传资料或模型输出中的 HTML、脚本和任意链接。
- 浏览器与 API 同源，不配置跨域放行。静态资源有 CSP；响应带 `no-store`。这些措施不等于对主机、浏览器插件或数据库文件进行加密或隔离。
- 资料撤下和令牌撤销仍由服务端逐次校验；页面不是实时撤权推送系统，已显示过的内容不能从人的记忆或截图中追回。
- 单机开发方案保持 `127.0.0.1` 监听。没有多租户公网部署、网页登录、账号找回、后台队列、SSE、OCR 或任务间自动长期记忆；这些不属于本次交付。

## 执行 API

下列端点与已有资料/任务接口共用 `Authorization: Bearer <token>`：

| 方法与路径 | 行为 |
|---|---|
| `GET /v1/runtime` | 当前执行模式、可执行场景和外发属性，不返回密钥 |
| `GET /v1/workspaces/{space}/tasks/{task}/runtime?after=0&limit=100` | 状态、累计预算、结果、游标事件；读取不启动运行器 |
| `POST /v1/workspaces/{space}/tasks/{task}/step` | 仅推进一个模型或工具动作 |
| `POST /v1/workspaces/{space}/tasks/{task}/answer` | 幂等保存追问回答，返回状态，不自动调用模型 |
| `POST /v1/workspaces/{space}/tasks/{task}/cancel` | 复用已有版本校验与持久取消 |

`step` 请求示例：

```json
{"expected_actions": 0}
```

先读取当前 `usage.actions`，新任务使用 0。该值在动作预留事务里比较，旧值重发返回 409，不会悄悄推进下一个动作。浏览器遇到网络错误不自动重试执行请求，先刷新状态再继续。

`answer` 必须附 `Idempotency-Key` 头，正文为：

```json
{"expected_version": 3, "text": "对当前追问的回答"}
```

实际版本以读取结果为准。同键、同版本、同内容可安全重发；同键改内容或版本过期拒绝。执行端点不接受模型名称、端点、密钥、主体、工具权限或外发授权等额外字段。

默认未配置模型时，step/answer 返回 503；读取、取消和资料管理仍可用。一次 step 使用普通同步 HTTP 响应，持续时间受运行器时限约束；没有后台任务自动启动机制。未知动作的恢复行为见 [持久运行器](runtime.md)。

## 受信服务端接入模型

已有外部模型适配器通过 Python 显式装配；本轮只验证离线演示，没有运行真实模型。下面是部署代码的装配形状，`approved_provider_factory` 由部署者创建，不能来自客户端请求：

```python
from juli.api import create_app
from juli.runtime_api import RuntimeConfig
from juli.application.model_session import SessionPolicy

app = create_app(
    registry,
    database=database_path,
    runtime=RuntimeConfig(
        provider_factory=approved_provider_factory,
        profile="approved-deployment-model-v1",
        mode="external",
        policy=SessionPolicy(allow_external_egress=True),
    ),
)
```

`mode="local"` 只能配不外发的 provider；`mode="external"` 必须显式允许外发，HTTP 适配器仍独立核对批准端点。工作台会显示外部模型提示。场景、模型配置和外发审批都由服务端负责；没有从网页“开启外发”的接口。真实费用、敏感资料和场景质量应在实际接入前按用途验收。

## 部署者配置新任务预算（M08）

多次回读资料会累积模型输入，默认的 `TaskBudget` 仍为 24 个动作、32000 输入计量单位和 4000 输出 token。部署者可在受信 Python 装配中显式传入：

```python
from juli.core.tasks import TaskBudget

app = create_app(
    registry,
    database=database_path,
    runtime=approved_runtime_config,
    task_budget=TaskBudget(max_input_tokens=128000),
)
```

`approved_runtime_config` 由部署者构造，沿用上节的显式模型配置。此处 128000 是 M08 合成流程选用的上限，不是普适推荐或实际费用估算。输入计量采用保守的字节计量；真实模型的上下文窗口、token 计费和使用量仍须单独确认。`SessionPolicy` 的调用次数、单次输出和时间限制继续生效。

预算只作用于新任务并固定到快照；重新配置服务不会提高或降低已有任务额度。同一幂等创建请求始终返回原任务和原预算。客户端请求中的 `budget` / `task_budget` 字段会被拒绝。没有网页调额度功能，现有 CLI 默认值不变；超过预算的任务仍停止，不以自动增加额度的方式续跑。
