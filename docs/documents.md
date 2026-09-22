# 文件、检索与证据（M03）

文件可以属于任何场景，解析器不提取预设的“企业资质”或“个人能力”字段。系统先保存原件、提取可定位文本，再允许任务按场景角色绑定资源版本。

## 文件支持与限制

| 类型 | 提取内容 | 位置与限制 |
|---|---|---|
| TXT / Markdown | UTF-8 文本，允许 UTF-8 BOM；不渲染 Markdown | 保留解码后的换行和空白；BOM 从提取文本中移除，原件字节保持不变 |
| DOCX | 正文及表格单元格中的段落文本、制表符与换行 | 正文遍历段落序号从 1 开始；不是 Word 页码。不提取页眉、页脚、脚注、图片、表格结构与排版；始终返回范围提示 |
| PDF | 文本层，由可选依赖 pypdf 提取 | 页码从 1 开始；不识别图片。无文本页逐页提示，整份无文本返回 `ocr_required`；拒绝加密 PDF |

不支持旧 `.doc`、图片、音视频、Excel、OCR 或联网抓取。安装 `.[documents]` 启用 PDF 依赖；TXT、Markdown、DOCX 不需要 pypdf。

单文件最多 8 MiB，PDF 最多 200 页，提取文本最多 250,000 个 Unicode 字符、10,000 个位置区间。DOCX 最多 2,000 个 ZIP 条目，条目声明解压总量不超过 32 MiB，正文 XML 不超过 8 MiB；拒绝重复成员、加密压缩成员、过高压缩比、宏文件、DOCTYPE 和 ENTITY 声明。不会解压到用户文件名指定的路径，也不会跟随文档中的链接。

解析在独立子进程运行，最长 15 秒，单个服务进程同时最多两个解析工作；子进程不继承模型密钥等任意环境变量。输入、输出和解析器有大小限制，但没有操作系统级网络沙箱或硬内存配额。复杂 PDF 可能在超时前占用较多内存；本阶段定位为受控本地使用，不能直接变成公网文件处理服务。

PDF 的读取顺序与复杂排版、DOCX 的修订和嵌入内容可能影响提取效果。位置指向**规范化的解析文本**，不是原文件的字节偏移或视觉区域。需要对照原件判断提取是否完整，不能因为状态为 `ready` 就认定内容无缺失。

## 升级数据库

停止使用该库的服务，先自行备份数据库文件，再执行：

```powershell
python -m juli db migrate --database .local/juli.sqlite3
python -m juli db check --database .local/juli.sqlite3
```

第 2 版增加原件、解析记录、证据片段、关键词和读取记录表，保持第 1 版 DDL 与校验和不变。旧 M02 文本在同一迁移事务内补建原件记录与索引，保留原来的资源 ID、版本 ID 和任务。若旧内容与已有哈希不符或任何迁移步骤失败，整体回滚，不自动修复内容。旧库未升级时新服务拒绝启动。

原件和解析文本、索引都保存在同一 SQLite 中；这避免跨文件系统的部分提交，但数据库体积会增加。没有去重、分布式对象存储或全库空间配额。迁移当前为一次事务，不适合无准备地处理大型线上库。

## 上传与绑定

沿用 [持久化指南](persistence.md) 的 `$headers`、`$base`、`$prefix` 和 `$conversation`。下面的本地路径由你显式选择，服务只收到文件字节与展示文件名：

```powershell
$file = (Resolve-Path './samples/profile.docx').Path # 换成你自己的文件
$filename = [System.Uri]::EscapeDataString([System.IO.Path]::GetFileName($file))
$upload = Invoke-RestMethod -Method Post -Uri "$base$prefix/files?filename=$filename" `
    -Headers $headers -ContentType 'application/octet-stream' -InFile $file
$upload.resource.status
$upload.parse
```

此示例不提供 `samples/profile.docx`，请换成自己的文件。文件名只作展示，不允许目录路径。更换现有逻辑资料时，在 URL 追加 `&resource_id=<已有资源ID>`，服务追加版本，旧任务继续绑定旧版本。没有文件创建幂等键，重试成功的上传会再生成一个版本。

HTTP 201 只说明原件和最终解析记录成功保存。检查 `resource.status`：

- `ready`：可绑定任务；仍应查看 `parse.warnings`。
- `failed`：查看 `parse.error_code`，原件仍可下载，不能用于新任务。修复文件或安装缺失解析依赖后，重新上传形成新版本；不原地改写旧版本。

请求同步等待本次解析完成，不创建持久后台处理队列。服务若在提交前退出，该次上传没有保存，客户端需检查记录后重试。授权会在解析前和提交前各检查一次；解析期间撤权或撤下目标资料会阻止保存。

绑定方式与文本资料相同，将 `$upload.resource.version_id` 传给任务的 `bindings`。支持多份文件、多种资料角色；服务从数据库读取实际资源描述，客户端不能注入哈希或解析内容。

## 检索、回读与引用

```powershell
$taskPath = "$prefix/tasks/$($task.snapshot.task_id)"
$candidates = Invoke-Juli 'POST' "$taskPath/evidence/search" @{ query = 'Python 数据分析'; limit = 10 }
if ($candidates.items.Count -gt 0) {
    $read = Invoke-Juli 'POST' "$taskPath/evidence/read" @{ evidence_id = $candidates.items[0].evidence_id }
    $read.items[0].locator
    $read.items[0].text
}
```

这是本地词项匹配，不是语义检索。词项做 Unicode NFKC 与大小写归一化；中文基本汉字按单字和相邻双字索引，其他连续词项保留最多前 80 字符。查询最多 4,000 字符、64 个不同词项，每次返回最多 20 个候选；按匹配的不同词项数量排序，再按证据 ID 稳定排序。未命中就是空列表，不调用模型补答案。该排序是基础实现，没有 BM25、向量相似度或重排效果承诺。

每个片段最多 1,600 字符，同一位置区间内相邻片段重叠 160 字符；不会跨 PDF 页或 DOCX 段落拼接。`char_start` 从 0 开始、`char_end` 为右开区间，以该版本的完整解析文本为基准。`page` / `paragraph` 从 1 开始。查询归一化不修改保存的片段原文。

搜索严格限制为该任务绑定的资源版本，排除已撤下资料。搜索返回候选 ID、版本和片段，不创建读取记录。回读再次校验当前权限、绑定版本、原件哈希、解析文本、定位和片段哈希，然后持久化当前主体在该任务中的读取凭证。只读成员也可以回读，产生的凭证仅记录读取事实，不修改任务状态。

`SQLiteStore.validate_task_result` 是 M04 模型会话已使用的服务端校验入口：它从数据库重取证据和当前主体的读取记录，不接受客户端提供的 receipt；已有读取记录不能绕过成员撤权或资料撤下。另一个主体的读取记录不能代替当前主体的回读。校验结构与来源不证明语义真实性，M06 已提供受控单动作执行接口，但不接受客户端直接提交最终结果。

## 资料查看接口

| 路径（前缀 `/v1/workspaces/{space}`） | 含义 |
|---|---|
| `GET /resource-versions/{version}` | 资源描述和解析文本；失败版本文本为空 |
| `GET /resource-versions/{version}/parse` | 解析器版本、状态、错误码、提示和片段数量 |
| `GET /resource-versions/{version}/original` | 经授权下载原件字节，以 `original.bin` 附件返回，可另存为原扩展名 |

原件不会以 HTML 内联展示。所有端点重查权限，无可公开访问的原件 URL。逻辑资料撤下后，下载、正文读取、检索与回读均不可用；原件、片段和读取记录仍存于数据库，不是物理删除。

`resource.content_hash` 对文件表示原件字节哈希；解析文本另有哈希，证据还有片段哈希。解析器版本随解析记录保存，升级 Python 依赖不重解析旧版本；需要新解析结果时创建新的资源版本。暂未提供索引重建、解析历史切换或位置跨版本映射。
