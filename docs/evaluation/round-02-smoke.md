# E02：三个真实模型任务的接入与观察

日期：2026-10-02。状态：首题首次真实响应被协议拒绝，本轮已停止；详见 [真实结果](round-02-result.md)。E01 冻结资料保持不变。

## 开始前记录

用户选择 `deepseek-v4-flash`，批准首批三个合成案例发送给所选模型服务，总费用上限人民币 10 元。没有批准发送真实资料、调用其他模型、重新跑完整测试集或推送变更。已有密钥由用户自行写入独立仓库 `.local/e02-deepseek-api-key.txt`，不发到聊天中、不进入源码或日志。

执行前用户明确将本轮总费用上限降为 **5 元**，并确认密钥已保存。本轮实际使用三题共享的 5 元账本，旧的 10 元授权已被替代；当下一次保守预留超出剩余额度时提前停止，即使尚未用满 36 次调用也不继续。

官方截至本次核对的说明：旧模型名 `deepseek-v4-flash` 会转到 DeepSeek-V4.1-Flash。本轮保留用户指定请求名称，同时记录响应模型字段，不把它报告为历史 V4 的独立成绩。价格按高峰未命中缓存的输入 2 元/百万 token、输出 8 元/百万 token 保守核算。来源：[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

现有适配器使用 `max_completion_tokens`，且不保留 DeepSeek 思考模式需要回传的 `reasoning_content`。这是协议适配差异，不能直接当模型推理失败。首轮仅评估非思考模式；评测目录中的适配层显式发送 `thinking.type=disabled` 与 `max_tokens`，不改变通用运行器、提示词、默认场景或冻结金标。来源：[官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 计划与验收

- 使用 E01 的 `career-01`、`career-02`、`tender-03`，分别观察充分资料判断、关键缺失追问、冲突澄清。每题只跑一次，不选择最好结果。
- 模型只能看到问题、判断日、清单中的资料和原场景包。入口不读取 grader；操作者看到实际追问后，才按原回答卡提供相关回答。
- 每题新建独立合成 SQLite 库并显式迁移，通过现有资料存储、证据网关、DurableRunner 执行。没有真实用户数据库或生产服务。
- 保留实际模型请求、HTTP 200 原始返回、结构化模型响应、工具输入输出、状态事件、追问与回答。密钥和请求头不进入这些记录；错误正文不公开或持久化。
- 每题至多 12 次模型调用、24 次动作、每次至多 4096 输出 token、64K 上下文准入和 300 秒累计活动执行。原运行器每动作 30 秒期限保持不变。等待操作者回答不消耗模型额度。
- 三题共享持久费用账本，按调用窗口与最大输出先保守预留；未知调用保留预留并停止，不自动重试，不因新进程重置费用。
- 先用 MockTransport 和合成脚本测试协议、预算、记录和追问闭环；离线通过仅证明入口可用。真实结果按原 rubric 人工核对，不能用任务 completed 代替质量通过。

## 当前结果

用户确认密钥保存并降至 5 元上限后，执行首题一次真实请求。HTTP 200 响应含两个工具调用和非空说明文字，被现有适配器拒绝；没有执行模型工具或产生分析结论。另两题未调用，没有自动重试。供应商报告 1324 输入、130 输出 tokens，保守估算 ¥0.003688；账本保留 ¥0.160768 预留并停止，未核对实际账单。分析质量分数保持空值。

本轮实际新增：`examples/quality_eval/smoke.py`、`deepseek_probe.py`、两份对应测试及操作说明。运行器、默认场景、核心合同和 E01 冻结目录不变。仅供本地评测，没有推送 GitHub。

离线验证共 61 项通过：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_deepseek_probe.py tests/test_chat_completions.py
# 41 passed：18 项新评测适配检查、23 项既有适配器检查
python -m pytest tests/test_quality_smoke.py tests/test_quality_suite.py tests/test_release_check.py
# 20 passed：7 项新入口检查、5 项 E01 检查、8 项发布边界检查
python -B -m examples.quality_eval.smoke --case career-01
python -B tools/check_quality_suite.py
python -B tools/release_check.py
```

检查覆盖上传后解析状态、真实本地检索/回读、追问不自动补答案、错误停止、禁止覆盖已有试次、协议字段、原始返回留存、密钥脱敏、共享费用预留、跨进程阻断未知调用和异常 usage。均为脚本/MockTransport 响应，不是 DeepSeek 的实际表现。

独立复核提示：入口仅支持同一进程中等待追问回答，十分钟超时后没有 `--resume` 命令；已在运行说明和超时输出明确，不将它包装为跨进程恢复验收。无权限或场景约束绕过发现；这不是完整安全扫描。适配层采用官方非思考字段，不放宽原有单工具、纯 JSON 或证据校验要求；这些严格协议是否影响实际模型输出需要实测记录。

原计划在密钥确认后按上述三题逐个执行，并补充真实响应、错误归因和人工评分。首轮三题不足以证明通用能力，思考模式及预留课程集都不在本轮实测范围。

上述待执行计划已被首次协议失败中断。执行前新增 5 元累计预算边界检查，19 项适配评测检查及 7 项入口检查共 26 项通过；静态预检查明确显示上限 5 元。失败后使用同一原始响应离线回放四个变体，确认两个独立拒绝条件，没有再发请求。实际原始记录、预留与未运行范围详见结果文档。下一轮先进行兼容修复与离线验收，保留本轮失败基线和费用；不能无声重跑或把协议错误填成模型质量零分。
