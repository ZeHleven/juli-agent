# 准备本地发布产物

本指南只准备源码和 Python 安装包，不创建远程仓库、不推送 GitHub、不上传 PyPI。当前为开发预览版；公开介绍必须保留真实模型效果尚未验收、本地部署与数据外发边界。

## 验证与导出

先按 [安装指南](getting-started.md) 建立独立虚拟环境并安装全部开发 extras，然后在仓库根目录执行：

```bash
python -m pytest
python -m examples.scenario_acceptance
python tools/release_check.py
python tools/release_check.py --manifest .local/release/source-manifest.json --archive .local/release/juli-agent-source.zip
```

发布检查使用明确的源码目录和扩展名清单；忽略 `.local`、虚拟环境、Git 元数据和构建缓存，拒绝未列入范围的文件、链接文件、数据库、环境文件及若干高置信度密钥格式。诊断只显示路径、行号和规则，不回显匹配的凭据。它不覆盖 Git 历史、所有秘密格式、依赖漏洞或完整安全审计。

源码归档包括文档、场景、示例、测试与独立实验，可解压后按安装指南使用。输出限于本地 `.local`，已有输出文件会拒绝覆盖；重新生成时选择新文件名。两个输出都由同一份已检查的文件内容生成，清单记录逐文件 SHA-256。

## Python 包

需要构建包时，在自己的虚拟环境安装构建工具：

```bash
python -m pip install build
python -m build --outdir .local/dist
```

wheel 只携带正式 `juli` 包、网页静态文件和 MIT 文本，不内置场景、示例或实验依赖。完整源码分发 sdist 包含这些开发材料；使用 wheel 的程序须另外指定受信场景目录。不要把 wheel 描述为独立的全部示例工程。

安装包验证应从源码目录之外执行，核对实际导入来自安装位置、六组示例完成且没有真实模型调用。更换依赖或实现后需重新运行相关测试，并更新 [发布准备记录](release-readiness.md) 与依赖清单；历史 M07/M08 结果保留原版本和环境。

## 对外发布时

独立公开仓库为 [ZeHleven/juli-agent](https://github.com/ZeHleven/juli-agent)，私密漏洞报告入口见 [SECURITY.md](../SECURITY.md)。每次发布前明确版本和发布说明，再核对 Git 将提交的文件。根目录 MIT 与第三方声明应随源码保留。

本地源码导出通过不等于可以安全公开已有 Git 历史。若之后从旧仓库迁入提交，需单独检查历史中是否包含凭据、真实资料或未经授权内容。当前工程尚无公开发布和维护时限承诺。
