# M07：LangGraph 小范围对照实验

本目录不是正式运行器。它比较现有 `DurableRunner` 与由 LangGraph 调度同一运行器的候选，并单独验证原生图中断和进程恢复。当前决定继续使用现有实现，依据见 [运行器决策](../../docs/decisions/0001-runtime.md)。

## 复现

在仓库根目录创建独立 Python 3.11+ 环境，避免把实验依赖安装到现有服务环境。以下为 Windows PowerShell 示例：

```powershell
python -m venv .local/m07-venv
& .local/m07-venv/Scripts/python.exe -m pip install -e ".[api,documents,model,dev]" -r experiments/m07_langgraph/requirements.txt
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
& .local/m07-venv/Scripts/python.exe -m pytest experiments/m07_langgraph -o addopts= -q -p no:cacheprovider --tb=short
& .local/m07-venv/Scripts/python.exe -m pytest -o addopts= -q -p no:cacheprovider --tb=short
```

macOS / Linux 使用 `.local/m07-venv/bin/python`，并以 `export` 设置相同环境变量。依赖安装需要网络；测试不需要模型密钥或外部服务。

正常的 `python -m pytest` 只发现 `tests/`，不会自动运行本实验。正式包不导入本目录，wheel 不包含这些文件；`requirements.txt` 只固定关键实验依赖，不是所有传递依赖的完整锁文件。

## 测试内容和文件

- `candidate.py`：图节点调用据理动作，图状态只包含动作计数和状态投影。权限、预算和结果仍由原账本处理。
- `fixtures.py`：全新合成资料、确定性假模型、任务装配；不读取原业务系统数据。
- `test_comparison.py`：24 项测试，覆盖相同输入/结果、重建、预算、取消/撤权/撤下资料、并发、防重、引用和工具边界，以及跨进程中断和提交间隙。
- `process_probe.py`：独立子进程中的检查点、中断恢复和故障注入。合成令牌只经 stdin 传入，不使用命令行参数或公开结果文件传递。
- `conftest.py`：关闭追踪，并拦截父进程测试中的 socket 连接。子进程也独立设置同样边界。
- `results.json`：2026-09-22 实际测试结果及版本记录。测试生成的 SQLite 文件位于 pytest 临时目录，不随项目交付。

故障注入只使用测试新建的 SQLite 文件，并在独立子进程提交合成动作后以退出码 23 结束进程。测试结束后，不会留下据理 HTTP 服务或后台运行器。

## 如何理解结果

正常流程的两组完整模型请求和输出相同，最终各完成 6 个动作、4 次假模型调用。原生 LangGraph 中断可以跨进程恢复；中断节点重入符合其公开语义。

候选仍调用现有运行器，因此通过权限和预算测试不代表 LangGraph 独立实现了这些业务能力。故障测试显示，图检查点与应用账本各自提交时需要协调：旧动作被拒绝后，测试通过明确读取业务状态并新建调度继续，不宣称已经实现原图自动修复。

没有测试真实模型质量、OCR、公网部署、完整 LangGraph 替代方案或多 Agent，也没有性能比较。测试时出现一项已有 requests 依赖兼容警告，24 项断言通过；当前记录使用既有 Python 测试环境和隔离的 saver 依赖目录，以上独立环境命令用于复现。
