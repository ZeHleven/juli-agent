# 安装并体验据理

当前是本地开发预览版。准备 Python 3.11 或以上。从 [GitHub 仓库](https://github.com/ZeHleven/juli-agent) 下载源码压缩包并解压，或运行以下命令获取源码：

```bash
git clone https://github.com/ZeHleven/juli-agent.git
cd juli-agent
```

在包含 `pyproject.toml` 的目录执行下文命令。下面使用自己的新虚拟环境，不依赖原报价项目、循练或作者电脑里的环境。

## 安装

Windows PowerShell：

```powershell
python -m venv .venv
& .venv/Scripts/python.exe -m pip install -e '.[api,documents,model,dev]'
& .venv/Scripts/python.exe -m juli scenarios --directory scenarios
& .venv/Scripts/python.exe -m juli runtime-demo --directory scenarios
& .venv/Scripts/python.exe -m examples.scenario_acceptance
& .venv/Scripts/python.exe -m pytest
```

macOS / Linux 将 `.venv/Scripts/python.exe` 替换为 `.venv/bin/python`。安装需要从包索引下载依赖；随后的上述演示和测试使用合成资料，不需要 API key。`.[documents]` 包含 PDF 依赖，完整测试安装不得省略。

`runtime-demo` 展示追问、回答、检索、引用及持久结果；`examples.scenario_acceptance` 自动验证三个场景的完整资料和背景缺失路径。它们使用确定性脚本模型，输出不代表真实 AI 判断质量。

## 打开本地工作台

在同一目录使用以下 PowerShell 命令。数据库路径为新建的据理专用文件：

```powershell
& .venv/Scripts/python.exe -m juli db migrate --database .local/juli.sqlite3
$juliIdentity = & .venv/Scripts/python.exe -m juli identity create --database .local/juli.sqlite3 --name 'Local owner' | ConvertFrom-Json
```

新令牌保存在 `$juliIdentity.token`。在自己的本机终端读取这个变量，妥善保存以便稍后粘贴到页面；不要放入截图、网址或共享记录。完成后再启动服务：

```powershell
& .venv/Scripts/python.exe -m juli serve --directory scenarios --database .local/juli.sqlite3 --port 8790 --demo-model
```

访问 `http://127.0.0.1:8790/` 并输入令牌。

页面内创建空间与会话，上传资料，选择 `minimal` 场景并绑定资料，输入问题，点击“开始 / 继续”。回答关键词追问后继续，可查看引用原文。网页离线模型仅支持 `minimal`，三个业务场景的自动验收通过上述示例命令运行。

要只管理资料而不执行模型，去掉 `--demo-model`。真实模型需要受信 Python 装配，不能在网页上传入端点和密钥；见 [模型指南](models.md) 与 [工作台指南](workbench.md)。

## 保存与复用背景

背景资料保存在据理数据库中，后续任务通过角色绑定已有版本，不需要每次重新上传。新的任务仍需要明确选择资料；不会自动读取整个工作空间。修改资料会产生新版本，已有任务保持原快照。

按 Ctrl+C 停止服务；数据库保留，下一次直接启动服务即可。不要反复创建新身份来代替保存自己的令牌信息。令牌默认有有效期，失效后由本机管理员使用身份 CLI 重新签发。

## 常见问题

| 现象 | 检查方式 |
|---|---|
| 启动提示数据库未准备好 | 确认路径正确，先显式运行 `db migrate`；不要指向别的项目数据库 |
| 401 | 检查令牌是否完整、过期、撤销或对应的主体被禁用 |
| 403 | 检查空间成员、任务创建者及服务端允许执行的场景 |
| 默认模式 step 返回 503 | 模型未启用；可选择离线演示，或由部署者显式装配真实模型 |
| 上传成功却不能绑定 | 查看解析状态；201 表示已保存，只有 `ready` 的版本可以绑定 |
| PDF 没有文本 | 当前不支持 OCR；扫描件不能作为文本 PDF 成功处理 |
| 多资料任务因预算停止 | 由部署者在新任务创建时配置预算，参见工作台指南；重试不会自动增加额度 |

当前完整安装命令的验证环境与结果见 [发布准备记录](release-readiness.md)。其他操作系统或 Python 版本不能仅凭这里的命令写法视为已经验收。
