# 双周期对话额度 1.0.0

AstrBot 用户对话额度管理插件的首个正式公开版本，采用 MIT 许可证。

## 功能

- 默认 5 小时 30 次、每周 200 次，任何一个周期耗尽都会暂停请求。
- 5 小时从用户首次获准请求起计算，重启保持窗口；周额度在周一 00:00 重置。
- 同一 Telegram 用户 ID 在同一 AstrBot 实例的所有 Telegram Bot 间共享额度。
- 查询 /usage 同时显示两个周期；管理员使用 /usage_set5h、/usage_reset5h、/usage_resetweek 管理短期额度与分周期重置。
- 插件配置中开启“重置所有用户用量（一次性）”并保存，可清空所有人的双周期计数。
  开关执行前会自动关闭，用户自定义额度和白名单保留。

## 安装与旧数据迁移

仓库：https://github.com/xmlans/astrbot_plugin_usage_limiter

要求 AstrBot >= 4.0.0，无额外运行依赖。也可将安装 ZIP 中的插件目录解压至 data/plugins/。

升级会保留周记录与已有配置；已有周额度请按需改为 200，新短期额度设置为 30。
Telegram 旧实例用量会合并，当前周用量相加，重复额度采用最小的有限值。
已删除的旧 Bot 实例需在 telegram_platform_ids 中补充 ID；裸 ID 数据需指定 legacy_platform_id。

额度在请求钩子中预扣，模型失败不自动退款。控制范围是进入 on_llm_request 的请求。

## 附件

- astrbot_plugin_usage_limiter-v1.0.0.zip：安装包。
- astrbot_plugin_usage_limiter-v1.0.0-source.zip：包含测试与工作流的仓库源码。
- SHA256SUMS.txt：当前两份 ZIP 的 SHA-256 校验值。

测试使用 AstrBot API 替身，真实平台的消息收发仍需部署验证。详细说明见
[README](https://github.com/xmlans/astrbot_plugin_usage_limiter#readme)。
