# 本地持久化与身份管理

当前持久化适配器使用 Python 标准库 SQLite，面向单机自部署。它保存文本和文件原件、解析版本、工作空间成员、会话、场景包快照与待执行任务，不执行 AI 分析。文件解析与证据访问详见 [文件指南](documents.md)。

## 1. 显式初始化

在仓库根目录、已安装项目依赖的 Python 环境执行：

```powershell
python -m juli db migrate --database .local/juli.sqlite3
python -m juli db check --database .local/juli.sqlite3
```

初始迁移使用事务创建表，记录据理应用标识、数据库版本和迁移校验和。重复运行同一迁移返回 `changed: false`。已有其他表、其他应用标识或不兼容迁移的数据库会被拒绝，不会尝试改造为据理数据库。不要指定原报价系统或其他应用的数据库。

启动和每次存储事务都会检查当前数据库标识、版本、迁移记录及表集合；这不等于对所有列、索引或磁盘文件的完整性鉴定。当前最新为第 3 版；第 1、2 版旧库需要显式升级，没有降级命令或自动修复。数据库管理员和可读写文件的本机用户属于受信边界。

## 2. 创建身份并启动

```powershell
$identity = python -m juli identity create --database .local/juli.sqlite3 --name "Local owner" --ttl-days 30 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($identity.token)" }
python -m juli serve --directory scenarios --database .local/juli.sqlite3 --port 8790
```

令牌是高熵随机值，数据库只保存 SHA-256 摘要。CLI 创建或补发时返回一次明文令牌；上例将输出捕获到变量，避免显示到终端。勿分享变量内容或把输出重定向到仓库文件。默认有效期 30 天，允许 1–90 天，不支持永久令牌。

服务在当前终端前台运行，可用另一个终端发送请求；身份变量不会自动传到另一个终端。可以先在服务终端执行启动命令，在调用终端用 `identity issue` 生成一个新令牌并捕获到 `$identity`，再构造 `$headers`。同一身份的多个令牌相互独立。

```powershell
# 将占位符换成已创建的 principal_id 或 token_id；这些 ID 不是令牌。
python -m juli identity issue --database .local/juli.sqlite3 --principal-id <principal-id>
python -m juli identity revoke --database .local/juli.sqlite3 --token-id <token-id>
python -m juli identity disable --database .local/juli.sqlite3 --principal-id <principal-id>
python -m juli identity enable --database .local/juli.sqlite3 --principal-id <principal-id>
```

`disable` 同时撤销该主体全部令牌；`enable` 不会复活旧令牌，需重新 `issue`。这些管理命令依赖本机文件权限，没有公开身份创建、登录或发令牌的 HTTP 端点。

每次读取或写入都在事务中重新验证令牌状态与空间成员身份。撤权提交后开始的新操作会被拒绝；不承诺追回已经返回给调用者的内容，也不会中断撤权之前已获得数据库读取快照的事务。

## 3. 保存资料并准备任务

以下 PowerShell 在有 `$headers` 的调用终端执行，服务须已经启动。仅使用合成文本：

```powershell
$base = 'http://127.0.0.1:8790'
function Invoke-Juli($method, $path, $body) {
    $parameters = @{ Method = $method; Uri = "$base$path"; Headers = $headers }
    if ($null -ne $body) {
        $parameters.ContentType = 'application/json; charset=utf-8'
        $parameters.Body = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 10))
    }
    Invoke-RestMethod @parameters
}
$space = Invoke-Juli 'POST' '/v1/workspaces' @{ name = '合成样例空间' }
$prefix = "/v1/workspaces/$($space.workspace_id)"
$resource = Invoke-Juli 'POST' "$prefix/resources" @{ display_name = '合成背景'; text = '有 Python 开发经验。' }
$conversation = Invoke-Juli 'POST' "$prefix/conversations" $null
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
$task = Invoke-Juli 'POST' "$prefix/conversations/$($conversation.conversation_id)/tasks" @{
    question = '请结合资料分析这个合成问题。'
    scenario_id = 'minimal'
    scenario_version = '0.1.0'
    bindings = @(@{ role = 'subject'; version_id = $resource.version_id })
}
$task.state
```

结果状态为 `prepared`，`execution_enabled` 为 `false`；创建接口本身不会生成分析答案；持久运行器可从 Python 接口另行推进。同一主体、空间、会话下，相同 `Idempotency-Key` 和请求内容只生成一条任务；相同键改变内容返回 409。这个保证只针对任务创建，不涵盖创建空间、资料版本或会话。重试同一创建请求时保留原键，新意图使用新键。

```powershell
# 更新资料会新建版本，不改写旧任务的版本绑定。
$newVersion = Invoke-Juli 'POST' "$prefix/resources/$($resource.resource_id)/versions" @{ display_name = '合成背景'; text = '有 Python 和 SQL 开发经验。' }
$saved = Invoke-Juli 'GET' "$prefix/tasks/$($task.snapshot.task_id)" $null
# 取消必须携带当前状态版本；过时版本返回 409。
$cancelled = Invoke-Juli 'POST' "$prefix/tasks/$($task.snapshot.task_id)/cancel" @{ expected_version = $saved.state.version }
```

空间归属、当前主体、资源描述、内容哈希和场景内容均由服务端查验。任务保存完整场景包与内容指纹；同一场景 ID/version 在库中不能被另一份内容覆盖，修改场景须增加版本。资料角色仍由场景声明，没有招投标或求职专用数据库分支。

## 4. 成员权限与接口

| 操作 | 所有者 owner | 编辑者 editor | 只读成员 viewer |
|---|---|---|---|
| 读取空间资料、会话和任务 | 可以 | 可以 | 可以 |
| 创建资料版本、会话、待执行任务，取消任务或撤下资料 | 可以 | 可以 | 不可以 |
| 查看、增删和修改成员 | 可以 | 不可以 | 不可以 |
| 转移所有权、删除所有者 | 暂不提供 | 不可以 | 不可以 |

当前粒度为整个工作空间：成员能读取空间内所有有效资料和历史任务，不提供文档级或个人私有会话权限。所有者通过 `PUT /v1/workspaces/{space}/members/{principal}` 和 `{"role":"viewer"}` 或 `{"role":"editor"}` 添加已由 CLI 创建的主体；`DELETE` 同一路径移除成员。

除 README 中列出的接口外：

| 路径（均以 `/v1/workspaces/{space}` 开头） | 方法与含义 |
|---|---|
| `/members` | GET 成员列表，仅所有者 |
| `/resources` | GET 有效资源的版本列表 |
| `/resources/{resource}/versions` | POST 新文本版本 |
| `/resource-versions/{version}` | GET 某个有效资源版本及解析文本 |
| `/resources/{resource}/withdraw` | POST 撤下逻辑资源的所有版本 |
| `/conversations` | GET 会话列表 |
| `/conversations/{conversation}/tasks` | GET 该会话任务列表 |
| `/tasks/{task}/cancel` | POST 取消任务，正文需有 `expected_version` |

`GET /v1/workspaces` 返回当前主体加入的空间。常见错误：401 令牌失效；403 无空间权限；404 当前空间内没有对应有效记录；409 幂等键、场景版本或状态版本冲突；422 输入不符合合同；503 数据库不可用或完整性检查失败。

## 5. 当前限制

- JSON 手动文本单条最多 65,536 字符 / 256 KiB UTF-8。另有二进制文件上传端点，支持范围及解析限制见 [文件指南](documents.md)；没有 OCR 或外部对象存储。
- 启动命令只监听回环地址；未实现 TLS、网页登录、访问限流、总存储配额或多节点部署，不应直接公开到公网。
- SQLite 文件保存明文资料；令牌摘要不等于数据加密。数据库文件、备份及本地账户的保护由部署者负责。
- 列表除成员列表外最多返回 1,000 条，当前没有分页。资源列表包含全部有效版本，不只是最新版本。
- 撤下资料会阻止正文读取、原件下载、检索回读、版本新增及新任务绑定；历史任务仍可展示原快照中的名称、哈希和版本等元数据，原文仍保存在数据库。撤下不是物理删除或隐私擦除。既有任务的幂等重试仍可返回原任务。
- 审计表保存动作、主体、目标和时间，不记录令牌明文或资料正文；未提供审计浏览、保留期限、导出和自动清理功能。
- 有事务、外键、幂等创建与状态版本检查，但没有 Agent 后台执行、租约、事件重放、崩溃后自动续跑、自动备份或迁移降级能力。

文件处理、证据读取和 M04 模型工具网关已接入这些身份与版本边界；M05 已实现持久 Agent 运行与动作协调，并显式升级至第 3 版。见 [运行器指南](runtime.md)。
