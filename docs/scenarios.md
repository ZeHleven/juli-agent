# 场景开发指南

## 最小包

```text
my-scenario/
  scenario.json
  instructions.md
  result.schema.json
```

只需复制仓库中的 `scenarios/minimal` 并修改声明。完整 manifest 是 JSON；第一步设计文档中的 YAML 是概念草案，当前加载器不支持 YAML。

```json
{
  "api_version": "juli.scenario.v1",
  "id": "equipment-choice",
  "version": "0.1.0",
  "name": "设备选择",
  "description": "结合候选设备和自己的约束整理依据。",
  "resource_roles": [
    {"name": "options", "label": "候选设备", "min_count": 1, "max_count": 20},
    {"name": "constraints", "label": "自己的约束", "min_count": 0, "max_count": 20}
  ],
  "instructions": "instructions.md",
  "result_schema": "result.schema.json",
  "tools": [
    {"name": "evidence.search", "version": "1.0.0"},
    {"name": "evidence.read", "version": "1.0.0"}
  ]
}
```

角色 ID 自行定义，核心不会根据这些名字改变业务行为。一个角色允许多个资源，同一资源也可承担多个不同角色。必需资料缺失会在任务准入时拒绝；允许后续追问的背景可以设为 `min_count: 0`。

可选的 `max_resources` 控制整个场景的资源数量，最大 128；每个角色 `max_count` 也不得超过 128。所有角色绑定总数上限为 256。这些限制是运行边界，不是只有两份资料的业务限制。

角色可声明 `media_types`，例如 `["text/plain", "application/pdf"]`。当前这里只检查声明，不能据此声称对应解析器已经实现。

## 分析方法与事实分离

`instructions.md` 说明如何分析、如何处理缺失和冲突信息。它不能声明自己是用户事实，也不提供证据回读记录。用户提供的 PDF、简历等待分析文件，不得自动加载到场景指导位置。

## 结果结构

结果 schema 使用 JSON Schema draft 2020-12，根类型必须为 object。支持 `#/$defs/...` 等同文档引用，禁止跨文件或 HTTP `$ref` 和改变引用基准的 `$id`。

通用结果块区分事实、推断、建议和限制；场景的结构数据放在 `AnalysisResult.scenario_payload_json`。事实块需要证据 ID，发布前仍须校验这些证据确实属于当前任务并已回读。仅通过场景 JSON Schema 校验，不能证明答案真实。

## 校验与装配

```bash
python -m juli validate path/to/my-scenario
python -m juli scenarios --directory path/to/installed-scenarios
```

目录命令读取下一层目录中的 `scenario.json`；不导入包中的 Python 文件。加载时计算 manifest、方法文本和结果 schema 的内容指纹。已加载对象固定内容，运行期间不动态替换版本。

程序方式：

```python
from pathlib import Path
from juli.adapters.scenario_files import load_scenario
from juli.bootstrap import tool_contracts
from juli.core.scenarios import ScenarioRegistry

registry = ScenarioRegistry(tool_contracts())
registry.register(load_scenario(Path("scenarios/minimal")))
package = registry.get("minimal", "0.1.0")
package.validate_result({"summary": "这只是结构校验示例。"})
```

`tool_contracts()` 提供两个证据工具的声明，现有装配器已将它们接到真实本地检索和回读。需要额外工具的包必须先注册对应 `ToolDefinition`，否则包注册会失败。注册不是授权；网关继续检查数据范围、外发、写入与确认条件。

默认 `create_durable_runner` 和 HTTP 装配只提供这两个工具。安装一个声明了新工具的包，不会自动得到工具实现；开发者需要在受信 Python 装配中提供注册表、执行器和 `ToolGateway`，再装配 `DurableRunner`。M08 验证的是新资料角色和结果结构，不宣称已提供任意工具的自动安装或沙箱。

## 可运行的扩展验收

见 [场景验收示例](../examples/scenario_acceptance/README.md)。设备选择包在示例目录而非核心中，声明了 `options`、`constraints`、`usage-context` 三种角色，并要求结果包含 `checks`。它与招投标、岗位匹配使用同一 HTTP API 和持久运行器；测试还将包和三种角色全部重命名后运行，检查没有场景名分支。

方法文本或 manifest 改变后必须增加场景版本。当前招投标和岗位匹配为 `0.1.1`，自定义模板仍为 `0.1.0`。已有任务保存完整场景快照，继续时不重新加载磁盘上的最新版；新任务则使用请求中明确指定的已安装版本。

这些接口在 `0.1.0.dev6` 开发期可能调整，调整会同时更新示例和测试。还没有承诺稳定的第三方插件兼容版本。
