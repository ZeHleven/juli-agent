# 第三方依赖与声明

据理自身代码采用 [MIT](LICENSE)；以下依赖通过包管理器安装，不把依赖源码复制进据理。它们保留各自的版权、许可证及声明。分发包含依赖的环境或镜像时，需要一并保留对应发行包中的许可文件，不能用据理的 MIT 覆盖它们。

| 直接依赖 | 用途 | 发行包声明的许可 |
|---|---|---|
| pydantic | 数据合同和校验 | MIT |
| jsonschema、referencing | 场景结果 schema 与引用 | MIT |
| fastapi | 可选 API 服务 | MIT |
| uvicorn | 可选 HTTP 服务入口 | BSD-3-Clause |
| pypdf | 可选文本 PDF 解析 | BSD-3-Clause |
| httpx | 可选模型 HTTP 适配及测试客户端 | BSD-3-Clause |
| pytest | 开发测试 | MIT |
| setuptools | 构建工具 | MIT |

已安装的直接和传递依赖及许可元数据、许可文件哈希见 [dependencies.json](docs/dependencies.json)。该清单记录本次 Windows / Python 3.12 完整开发安装的具体版本，不是适用于所有平台的依赖锁文件，也不是漏洞扫描报告。部分依赖采用 BSD、Apache、PSF 或 MPL 等许可；以对应发行版附带的原文为准。

`experiments/m07_langgraph` 的 LangGraph、LangChain Core 及 SQLite 适配依赖只用于独立对照，不属于正式安装依赖。具体版本见 [实验 requirements](experiments/m07_langgraph/requirements.txt)，依赖会随各自包安装。该实验环境的传递依赖没有纳入上述正式开发环境清单。

测试文件和示例资料由本工程生成，不含真实企业或个人材料。设计参考及许可范围见 [来源说明](docs/provenance.md)。
