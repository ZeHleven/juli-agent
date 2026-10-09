# 据理 Juli

**结合你的资料，分析眼前的问题。**

据理的目标是一个可独立部署、可扩展场景的资料分析 Agent 框架与工作台。背景资料可以长期维护，每次结合新的文件与问题进行分析，并让结论可以回到具体资料。

**当前为 `0.1.0.dev12` 本地开发预览版。E07为普通分析增加关键问题声明：还有关键问题时进入等待回答，候选不作为正式结果。已通过离线流程验证，真实模型效果尚未复测。E06岗位题仍漏实际追问的历史结果保留；E05审查实验默认关闭。结构通过不等于内容质量通过。**

项目仓库：[ZeHleven/juli-agent](https://github.com/ZeHleven/juli-agent)。

首次使用可从 [安装与体验](docs/getting-started.md) 开始；扩展自己的用途可读 [场景开发](docs/scenarios.md) 和 [三个可运行示例](examples/scenario_acceptance/README.md)。本地数据维护见 [部署说明](docs/deployment.md)，参与改进见 [贡献指南](CONTRIBUTING.md)。

首轮真实模型评测的 [案例与验收标准](examples/quality_eval/v1/README.md) 已在本地准备：12 道基础题、4 道配对变体，覆盖岗位、招投标与课程选择。E01 通过资料准备检查；E02 首次真实调用收到响应后因多工具及文本混合格式被当时协议拒绝，尚未验证分析质量。详见 [E01 记录](docs/evaluation/round-01-plan.md)、[E02 首次结果](docs/evaluation/round-02-result.md) 与 [E03 修复及续测记录](docs/evaluation/round-03-plan.md)。

三题真实模型冒烟入口已另行准备，默认只做离线预检查；显式提供密钥和 `--live` 才会按批准配置外发合成资料。它使用既有运行器，增加评测专用 DeepSeek 非思考模式适配、共享费用预留和原始轨迹记录。使用范围与限制见 [评测运行说明](docs/evaluation/smoke-running.md)，最新离线修复见 [E07关键问题与等待回答](docs/evaluation/round-07-result.md)，最近真实效果见 [E06格式与交互验证](docs/evaluation/round-06-result.md)。旧试次不能重跑；本轮未增加付费调用。

## 当前可以验证什么

- 资料角色由场景声明：可绑定多份资料，一个资源版本也可承担多个角色。
- 任务固定资料版本、场景内容指纹和预算配置，背景更新不会改写旧任务快照。
- 场景包含分析指导、输入角色、工具需求和结果 JSON Schema；新增场景不用修改核心。
- 合同层检查工作空间范围、证据来源、原文哈希和任务内回读记录。
- 追问、继续、结束、取消已接入持久任务状态；回答防重，事件可按游标增量读取。
- 普通analysis须明确声明关键问题或null；有问题时先校验候选再等待，正式result为空。识别关键未知仍依赖模型，未声称语义稳定。
- 工具合同声明读写、外发和确认要求；注册工具不等于授权执行。
- 个人访问令牌可过期、撤销；空间成员分为所有者、编辑者与只读成员，每次存储操作都重新校验权限。
- 文本资料、不可变版本、会话与任务保存到据理独立数据库；重启后仍可读取。
- 创建任务支持幂等请求；任务只进入 `prepared`，可以按状态版本取消；受信 Python 程序可装配运行器推进任务。
- 提供场景目录、合同 schema、持久化与单动作执行 API，以及同源网页工作台；命令只监听本机。
- 上传 TXT、Markdown、DOCX 和带文本层的 PDF，保存原件、解析状态与不可变版本。
- 片段记录字符位置，DOCX 附正文段落序号，PDF 附页码；本地关键词检索只查任务绑定的有效资料。
- 证据回读重查权限、任务绑定、原件哈希和原文位置，持久记录主体与任务的读取凭证。搜索候选不等于已经回读。
- 模型只看到场景与平台共同允许的工具；网关校验参数、返回结构、作用域与超时，拒绝任意命令、写入和工具外发。
- 初始模型上下文只包含问题与资料目录，原文通过检索和回读按需取得；资料文字不被拼进系统指令。
- 持久运行器保存消息、动作、预算和结果；每个会话只允许一个活动任务，未知调用结果不自动重试。

招投标、岗位匹配和自定义模板都通过同一个场景包接口加载。M08 增加独立的设备选择示例，验证新资料角色和结果结构无需修改核心；三种场景都覆盖已有背景复用、上传新文件、追问、检索、原文引用和资料不足流程。验收使用确定性脚本模型，原研判 Agent 的资格核查能力尚未迁入。

在完成下面的开发依赖安装后，可从仓库根目录运行 `python -m examples.scenario_acceptance`，查看六组自动验收结果。示例使用临时合成数据库和进程内 HTTP 客户端，不需要密钥。说明见 [场景验收示例](examples/scenario_acceptance/README.md)。

## 本地运行

需要 Git 和 Python 3.11 或以上。先获取源码；也可在仓库页面选择 Code → Download ZIP，解压后进入项目目录。

```bash
git clone https://github.com/ZeHleven/juli-agent.git
cd juli-agent
```

以下命令在本仓库目录执行。

```bash
python -m venv .venv
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux 激活：

```bash
source .venv/bin/activate
```

安装与离线合同演示：

```bash
python -m pip install -e ".[api,documents,model,dev]"
python -m juli scenarios --directory scenarios
python -m juli validate scenarios/minimal
python -m juli demo --directory scenarios
python -m juli model-demo --directory scenarios
python -m juli runtime-demo --directory scenarios
python -m pytest
```

`demo` 只在内存中构造合成资料，检查多资源绑定、引用关系和状态转换。输出明确包含 `analysis_executed: false` 与 `model_calls: 0`，没有模型分析、文件解析或工具执行。也不会保存你的个人资料。

`model-demo` 使用临时合成数据库和确定性的假模型，实际经过工具网关执行“检索 → 回读 → 结果引用校验”。它不需要密钥，不发网络请求，明确输出 `real_model_calls: 0`；任务仍为 `prepared`，不宣称完成了 AI 分析。详见 [模型与工具接入指南](docs/models.md)。

`runtime-demo` 在临时合成库中验证持久追问、继续、检索、回读和结果保存；仍无真实模型或外部调用。用法、预算与中断语义见 [运行器指南](docs/runtime.md)。

如果只使用核心合同和场景加载器，可安装 `python -m pip install -e .`，无需 API 和开发依赖。CLI 可在任意目录运行，场景目录通过 `--directory` 显式传入；场景包不是硬编码在核心中的内置分支。

## 启动本地工作台与 API

先显式创建据理自己的数据库，再创建本地主体。PowerShell 示例将新令牌保存在当前终端变量中，不直接显示：

```powershell
python -m juli db migrate --database .local/juli.sqlite3
$identity = python -m juli identity create --database .local/juli.sqlite3 --name "Local owner" | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($identity.token)" }
python -m juli serve --directory scenarios --database .local/juli.sqlite3 --port 8790
```

`identity create` 本身会向标准输出返回一次新令牌；不要把输出、变量值或数据库提交进仓库。默认令牌有效期 30 天，可通过 CLI 撤销。启动只检查数据库，缺库、错库或迁移不匹配都会拒绝运行，不会自动建表。旧 M02/M03/M04 数据库需停止服务、备份后再次显式运行 `db migrate` 升至第 3 版；既有文本会补建索引，不改变版本 ID 或任务快照。

启动后访问 `http://127.0.0.1:8790/`，使用个人令牌连接工作台。默认模型关闭；在启动命令末尾添加 `--demo-model` 可体验明确标记的离线追问、检索和引用流程，不调用真实模型。操作步骤和模型装配见 [工作台指南](docs/workbench.md)。

服务固定监听 `127.0.0.1`。目前是单机本地部署方案，身份管理依赖本机管理员权限，没有账号密码登录或云端账号系统。网页仅在当前页面内存持有个人令牌。数据库明文保存资料，须由部署者保护文件访问权限。完整的请求示例、权限表与限制见 [持久化使用指南](docs/persistence.md)。

| 接口 | 用途 |
|---|---|
| `GET /health` | 基础状态与执行模式，默认模型关闭 |
| `GET /v1/me` | 当前令牌对应的主体 |
| `POST /v1/workspaces` | 创建空间并成为所有者 |
| `POST /v1/workspaces/{space}/resources` | 保存纯文本资料，生成版本与哈希 |
| `POST /v1/workspaces/{space}/files?filename=sample.pdf` | 上传二进制文件，保存原件和解析状态 |
| `POST /v1/workspaces/{space}/conversations` | 创建会话 |
| `POST /v1/workspaces/{space}/conversations/{conversation}/tasks` | 绑定资料版本，持久化待执行任务，需要 `Idempotency-Key` |
| `GET /v1/workspaces/{space}/tasks/{task}` | 读取任务快照与状态 |
| `POST /v1/workspaces/{space}/tasks/{task}/evidence/search` | 在绑定资料中检索候选片段 |
| `POST /v1/workspaces/{space}/tasks/{task}/evidence/read` | 授权回读原文并记录读取凭证 |
| `GET /v1/runtime` | 查看执行模式与启用状态 |
| `POST /v1/workspaces/{space}/tasks/{task}/step` | 带动作计数条件推进一个动作，默认禁用 |
| `POST /v1/workspaces/{space}/tasks/{task}/answer` | 按版本和请求键保存追问回答 |
| `GET /v1/workspaces/{space}/tasks/{task}/runtime` | 状态、预算、结果与游标事件 |
| `GET /v1/scenarios` | 场景声明与内容指纹 |
| `GET /v1/contracts/TaskSnapshot` | 查询合同 JSON Schema |

除健康检查外，上述接口需要个人访问令牌。客户端不能传入主体、权限上下文、资料哈希或完整资料描述来替代服务端记录。

文件请求使用 `application/octet-stream`，不用 multipart。**HTTP 201 仅表示资料已保存，须检查 `resource.status` 是否为 `ready`**；解析失败的原件仍保留，但不能绑定到新任务。文件支持范围、上传示例、引用定位与限制见 [文件与证据指南](docs/documents.md)。

### 可选：只读合同演示模式

在 PowerShell 生成当前终端会话的本地访问令牌，再启动服务：

```powershell
$env:JULI_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m juli serve --directory scenarios --port 8790
```

不传 `--database` 时仅开放健康检查、只读场景目录和合同 schema；使用 `Authorization: Bearer <JULI_API_TOKEN>`。这个静态演示令牌不能用于持久化模式，数据库认证失败也不会回退到该令牌。

## 新建场景

复制 `scenarios/minimal` 到新目录，修改 `scenario.json` 的 ID、资料角色、工具需求，以及分析指导和结果 schema，再运行 `juli validate <目录>`。

详细说明见 [场景开发指南](docs/scenarios.md)。场景包仅由部署者安装，不能把普通用户上传的文件自动当作可执行插件或系统指令。

## 工程结构

```text
backend/src/juli/
  core/          通用资源、任务、证据、工具与场景合同
  application/   上下文装配、受控网关、持久运行器与内存测试会话
  adapters/      场景/存储/解析/索引、受控本地工具、脚本模型与 HTTP 模型适配器
  bootstrap.py   显式装配与工具合同注册
  api.py         两种显式认证模式、合同和场景目录 API
  persistence_api.py  主体、空间、文本版本、会话和待执行任务 API
  document_api.py     文件上传、解析状态、原件下载、检索和回读 API
  runtime_api.py      服务端模型装配与单动作执行、追问、状态接口
  web/               无构建依赖的本地网页工作台
  cli.py         校验、演示、显式迁移、本地身份管理与服务命令
scenarios/       招投标、岗位匹配、自定义模板
tests/           离线合同、迁移、权限、持久化、解析定位与 API 检查
docs/            当前状态、架构和场景指南
experiments/     单独安装依赖、单独运行的框架对照，不进入正式安装包
examples/        合成场景数据、示例扩展包和可运行的 API 验收脚本
```

## 当前边界与后续

这是可运行的框架起点。持久化 API 已从数据库构造身份、权限上下文和资料描述；检索与证据回读已有真实本地实现。模型适配是受信 Python 程序可调用的库接口，`serve` 已开放受控执行端点，默认禁用模型；可显式使用 `--demo-model` 验证离线流程。模型输出通过引用校验仍不等于事实或结论正确，提示词分层也不保证完全抵御文档诱导。

持久运行器在数据库中协调动作、预算与同会话任务。重新装配后可继续已提交的步骤；到期且结果不明的动作会失败停止，不会盲目重放。旧内存会话仅用于连接测试，不能接管持久任务。外部模型必须显式配置目标、密钥并批准本次任务数据外发；不会自动读取旧项目凭据。本地保存资料不意味着使用外部模型时资料仍只留在本机。

目前只支持单机 SQLite；没有 OCR、向量语义检索、文件加密、自动备份、迁移回退、通用资源列表分页、后台任务队列或完整资料删除流程。撤下资料会阻止正文下载、检索回读与新任务绑定，但不会物理擦除历史数据。解析子进程设有超时和输入限制，没有操作系统级沙箱或硬内存配额，不用于公开接收任意不可信文件。详见 [持久化边界](docs/persistence.md) 与 [文件边界](docs/documents.md)。

M07 已完成 LangGraph 小范围离线对照，当前继续使用 `DurableRunner` 和据理自己的动作账本。LangGraph / LangChain 不进入正式依赖，也不增加多 Agent 调度。原型、故障恢复观察和重新评估条件见 [运行器决策](docs/decisions/0001-runtime.md)；需要复现实验时再安装 [独立实验依赖](experiments/m07_langgraph/README.md)。普通使用不需要这些依赖。

M09 已整理安装、部署、扩展、贡献与发布文档，并补充许可和依赖清单。验证环境与可发布范围见 [发布准备记录](docs/release-readiness.md)，实施状态见 [进度与验证](docs/status.md)，职责划分见 [架构说明](docs/architecture.md)。

## 许可与来源

据理代码采用 [MIT 许可证](LICENSE)。第三方依赖仍遵循各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md) 与 [设计来源](docs/provenance.md)。当前是本地部署的开发预览版，尚未发布 PyPI；安装请使用本仓库源码，不要假定包索引上的同名项目属于据理。

安全边界与报告方式见 [SECURITY.md](SECURITY.md)。发布前的检查及产物准备见 [发布指南](docs/releasing.md)。
