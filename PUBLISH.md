# 首次上传 GitHub

目标仓库为 `xmlans/astrbot_plugin_usage_limiter`，许可证为 MIT。本地源码与安装包已
使用此仓库身份；上传前请确认你在 GitHub 上登录的是有权限管理该仓库的账号。

## 网页上传

1. 在 GitHub 创建名为 `astrbot_plugin_usage_limiter` 的 **Public** 仓库。
2. 解压 `astrbot_plugin_usage_limiter-v1.0.0-source.zip`。
3. 将解压目录内部的文件上传到仓库根目录，保留 `.github/`、`.gitignore` 和
   `.gitattributes`；根目录应直接出现 `main.py` 与 `metadata.yaml`。
4. 提交到 `main` 分支，等待 CI 通过。
5. 在 AstrBot 中使用仓库地址进行一次实际安装和额度拦截检查。

## Git 上传

在源码目录运行以下命令。Git 的提交身份需事先配置；如果仓库已有 Git 历史或
`origin`，请使用现有设置。

```bash
git init -b main
git add .
git commit -m "Release dual-period usage limiter 1.0.0"
git remote add origin https://github.com/xmlans/astrbot_plugin_usage_limiter.git
git push -u origin main
```

## 创建 Release

CI 和实际安装检查通过后，可推送版本标签：

```bash
git tag v1.0.0
git push origin v1.0.0
```

`release.yml` 会核对标签和元数据版本，运行检查，然后生成两份 ZIP 与校验文件并
创建 GitHub Release。工作流只有发布任务具有仓库内容写入权限，不需要另设个人令牌。

也可以先在 GitHub Actions 中禁用 `Release` 工作流，再通过 GitHub Releases
网页手动创建 `v1.0.0`，使用 `RELEASE_NOTES.md` 的正文，上传 `dist/` 内的安装
ZIP、源码 ZIP 和 `SHA256SUMS.txt`，避免自动工作流重复创建同一 Release。

## 提交到 AstrBot 插件市场

1. 确保上述 GitHub 仓库已经公开，仓库根目录直接包含 `main.py`、
   `metadata.yaml`、`_conf_schema.json`、`README.md`、`LICENSE` 和 `logo.png`。
   请上传解压后的文件，不能只把 ZIP 本身放进仓库。
2. 在 AstrBot 中通过仓库地址进行实际安装，检查 `/usage`、双周期拦截与
   Telegram 跨 Bot 共享行为。
3. 注册或登录 [AstrBot Cloud 插件发布页面](https://cloud.astrbot.app/publish)，
   提交仓库地址 `https://github.com/xmlans/astrbot_plugin_usage_limiter`。
4. 核对从 `metadata.yaml` 解析的名称“双周期对话额度”、作者 `xmlans`、
   版本 `1.0.0` 和功能介绍，按照页面要求完成提交，查看发布状态。

`logo.png` 是用户提供的正方形封面，已放在仓库根目录，并在 README 顶部展示。
代码 ZIP 大小需低于 16MB；当前安装包与源码包均已包含封面。
详细规则以 [AstrBot 官方发布指南](https://docs.astrbot.app/dev/star/plugin-publish.html)
和 [Logo 说明](https://docs.astrbot.app/dev/star/plugin-new.html) 为准。
