# 参与开发

请先通过 GitHub Issue 描述问题或改动目的。错误报告请注明 AstrBot / Python
版本、平台类型、复现步骤和经过脱敏的配置；请勿提交实际用户用量或访问凭据。

## 本地检查

在 Python >= 3.10 环境中，从仓库根目录运行：

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
python tools/release.py check
python tools/release.py build
```

插件运行只依赖 AstrBot 与 Python 标准库。`requirements-dev.txt` 中的工具用于
开发与发布检查。测试中的 AstrBot API 替身只用于测试，不会在真实插件中加载。

提交前请使用 `python -m ruff format .` 格式化代码。修改计数、隔离、迁移或恢复
逻辑时，应增加对应回归检查。在真实 AstrBot 中至少确认插件能加载、查询指令
可用、额度耗尽后模型不再收到新请求。

## 发布流程

1. 更新 `main.py` 的 `PLUGIN_VERSION`、`metadata.yaml`、更新记录及发布说明。
2. 通过以上本地检查，检查 `dist/` 内的安装包和源码包。
3. 推送提交，然后推送与版本一致的标签，例如 `v1.0.0`。
4. GitHub 的 Release 工作流会再次检查、打包并创建带附件的 Release。

安装包使用明确文件清单，排除开发工具、缓存、用量数据库和本机配置；源码包
包含贡献与发布所需的文件。不要通过压缩整个工作目录来替代发布脚本。
