# AstrBot 双周期对话额度

![插件封面](logo.png)

同时限制每位用户的 **5 小时额度与每周额度**，任一用尽即暂停模型请求。
同一 Telegram 用户 ID 在同一 AstrBot 实例的所有 Telegram Bot 中共享额度。

Five-hour and weekly LLM quotas for AstrBot, shared across Telegram bots per user.

- 作者：xmlans
- 版本：1.0.0
- 许可证：[MIT](LICENSE)
- 仓库：[xmlans/astrbot_plugin_usage_limiter](https://github.com/xmlans/astrbot_plugin_usage_limiter)

## 安装

要求 **AstrBot >= 4.0.0**，Python 版本遵循所用 AstrBot 的要求；插件代码支持 Python >= 3.10。
插件无需额外运行依赖。

在 AstrBot 插件管理中使用以下仓库地址安装：

```text
https://github.com/xmlans/astrbot_plugin_usage_limiter
```

也可以从 GitHub Releases 下载 `astrbot_plugin_usage_limiter-v1.0.0.zip`，
解压后将 `astrbot_plugin_usage_limiter` 文件夹放入 AstrBot 的 `data/plugins/`，
重载插件。安装目录中应直接包含 `main.py`、`metadata.yaml` 和 `_conf_schema.json`。

## 快速开始

1. 安装后，在聊天中发送 `/usage`，确认能够显示用量和完整用户标识。
2. 在 WebUI 插件配置中设置额度，默认 **5 小时 30 次、每周 200 次**。
3. 按需填写白名单和自定义额度，然后重载插件。

Telegram 使用共享标识 **`telegram:用户ID`**，例如 `telegram:10001`；
该用户在 Bot A 消耗的额度会同步影响 Bot B，包括私聊与群聊。
其他平台使用 **`平台实例ID:用户ID`**，仍按实例隔离。
请复制 `/usage` 显示的完整标识用于配置和指令，特殊字符会进行 URL 转义。
`telegram` 是共享标识的保留名称，其他平台实例不要使用此 ID。
共享范围是同一个 AstrBot 实例及其用量文件，不跨服务器同步。

## 指令

指令前缀以你的 AstrBot 配置为准，下表使用默认的 `/`。

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/usage` | 所有用户 | 查看两个周期的用量、剩余额度与重置时间；别名：`/用量`、`/额度`、`/查用量` |
| `/usage_set <用户标识> <额度>` | AstrBot 管理员 | 设置每周额度 |
| `/usage_set5h <用户标识> <额度>` | AstrBot 管理员 | 设置 5 小时额度 |
| `/usage_reset <用户标识>` | AstrBot 管理员 | 重置两个周期的用量与提醒 |
| `/usage_reset5h <用户标识>` | AstrBot 管理员 | 只重置 5 小时窗口 |
| `/usage_resetweek <用户标识>` | AstrBot 管理员 | 只重置本周用量 |
| `/usage_check <用户标识>` | AstrBot 管理员 | 查询指定用户的用量 |

设置额度时，0 或负整数表示仅取消该周期的限制。
管理员可使用当前平台的裸用户 ID，例如 `/usage_set 10001 200`。
在 Telegram 执行时，裸 ID 指向跨 Bot 共享的 Telegram 用户；
完整标识可跨平台管理，例如 `/usage_set5h telegram:10001 30`。

额度优先级：**白名单 > 管理指令设置的持久化额度 > WebUI 自定义额度 > 默认额度**。
两个周期分别遵循上述优先级。管理指令设置的额度跨周期保留；
再次执行对应设置指令可修改它。重置指令不会清除持久化额度。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 启用额度控制 |
| `default_weekly_limit` | `200` | 每周请求上限；0 或负整数为无限制 |
| `default_5h_limit` | `30` | 5 小时请求上限；0 或负整数为无限制 |
| `whitelist` | `[]` | 完整用户标识列表，白名单用户不受限制 |
| `custom_limits` | `"{}"` | 每周自定义额度，JSON 文本 |
| `custom_5h_limits` | `"{}"` | 5 小时自定义额度，JSON 文本 |
| `telegram_platform_ids` | `[]` | 补充已经删除的旧 Telegram Bot 实例 ID，以迁移其记录 |
| `legacy_platform_id` | 空字符串 | 旧版裸 ID 数据所属的实际平台实例 ID |
| `enable_50_notice` | `true` | 达到 50% 时提醒 |
| `enable_90_notice` | `true` | 达到 90% 时预警 |
| `enable_100_notice` | `true` | 最后一次额度消耗后提醒 |
| `exhausted_msg` | 内置提示 | 超额提示，支持 `{period}`、`{used}`、`{limit}`、`{reset_time}` |
| `reset_all_usage` | `false` | 开启并保存后，一次性重置所有用户的双周期用量 |

在 WebUI 的每周或 5 小时自定义额度文本框中填写 JSON 对象：

```json
{
  "telegram:10001": 100,
  "qq_bot:10002": 30
}
```

文本配置用于兼容 AstrBot 4.0+ 的配置类型；已有字典格式的配置仍可读取。
如果直接编辑整个配置文件，两个自定义额度字段都应写成 JSON 字符串，
参见 [完整配置示例](examples/config.example.json)。

额度只接受整数或整数字符串。无效自定义值忽略并使用默认额度，
无效默认值分别回退到每 5 小时 30 次、每周 200 次；无效 JSON 使用默认额度。
耗尽模板还支持 `{weekly_used}`、`{weekly_limit}`、`{five_hour_used}`、
`{five_hour_limit}`。`reset_time` 为所有已耗尽周期中较晚的重置时间。
`used` / `limit` 表示阻塞且结束最晚的那个周期；`five_hour_usage` / `weekly_usage`
提供可读的双周期统计（包括无限制状态），内置提示会分别显示两项用量。
保存配置会触发插件热重载；旧版本需确认重载完成。

## 配置页重置所有用户

在插件配置中开启 **“重置所有用户用量（一次性）”**，点击 **保存配置**。
保存触发的插件重载会将所有用户的两个周期用量和提醒状态清零，
保留白名单、自定义额度和用户记录。5 小时窗口从该用户的下一次请求重新开始。

AstrBot 原生配置页没有插件自定义操作按钮，因此此入口使用一次性开关。
执行前会先持久化关闭开关，之后普通重载不会再次重置新产生的用量；
重新打开配置页可看到关闭状态，完成数量记录在插件日志中。
如果无法保存关闭状态，不会清空用量；如果数据写入失败，修复存储后需要重新开启并保存。

## 计数规则与范围

- 5 小时采用从该用户首次获准请求开始的固定窗口，例如 12:00 首次使用，17:00 到期；
  到期后下一次请求开启新的 5 小时窗口。不是全天固定整点分段，也不是每条请求分别滚动过期。
  查询 `/usage` 不启动窗口，插件重启不会清空尚未到期的窗口。
- 两个周期独立维护。5 小时恢复不会清零周用量，跨周也不会清零尚未结束的 5 小时窗口。
  任何一个周期用尽都暂停请求；拒绝的请求不会额外消耗任一周期的次数。
- 采用 ISO 自然周，周一 00:00 重置。日期使用 AstrBot 进程所在系统的本地时区；
  改变服务器时区会改变周期边界。
- 在 `on_llm_request` 钩子中同时扣除两个周期各一次额度，再交给模型。
  后续模型请求失败、取消或发送失败时不会自动退还额度。
- 普通指令只要没有进入该钩子，就不计数；这不是 Token 用量或模型费用统计。
- 50%、90%、100% 阈值在各周期内各最多提醒一次；单个周期同时达到多个阈值时只提醒最高阈值。
- 超额或无法可靠保存用量时，先停止事件再发送提示；提示模板错误或发送失败不会放行请求。
- 限制范围是进入此钩子的请求。直接调用 provider 或绕过该钩子的第三方插件不受此插件控制。
- 数据文件用于单个 AstrBot 进程；不支持多个进程共享同一份用量文件。

## 数据、升级与恢复

数据使用 AstrBot 官方插件数据目录，与源码分开：

```text
data/plugin_data/astrbot_plugin_usage_limiter/
├── usage_data.json
└── usage_data.json.bak
```

升级前备份数据。从此前仅有周额度的本地版本升级时保留已有周计数、管理额度和配置；
新增 5 小时窗口从升级后的首次获准请求开始。
**已有配置的周额度不会被自动覆盖，请按需改为 200，5 小时额度设置为 30。**

Telegram 旧实例记录会在首次访问时合并为 `telegram:用户ID`，同周用量相加，
重复自定义额度取最小的有限值，旧标识删除，避免重复累计。
当前配置中的 Telegram Bot（包括禁用的 Bot）自动识别；
已从配置删除的旧 Bot，请在 `telegram_platform_ids` 中补充其原实例 ID。
若合并已有多个有效短期窗口，用量相加并以较晚的结束时间重置，以防切换 Bot 绕过额度。
旧实例白名单转换后对所有 Telegram Bot 生效。

旧版只有裸用户 ID 的记录，需要填写 `legacy_platform_id`
并重载插件；该值使用实际平台实例 ID，无需 URL 转义。
旧记录在对应用户首次访问时迁移，保留用量、自定义额度和通知状态。
归属 Telegram 的旧记录会并入共享记录；其他平台仍按实例隔离。
旧白名单和自定义配置中的裸 ID 也按此归属处理。

未指定迁移平台时，存在旧记录的用户请求会暂停，原记录会保留。
若旧记录曾由多个平台共用，原文件无法区分各平台贡献，需要管理员决定归属。
旧数据若保存在其他目录，先备份，再复制到上述目录。

保存采用临时文件、磁盘刷新和原子替换，并在主文件提交后更新备份。
主文件损坏时尝试恢复备份，损坏文件保留为 `usage_data.json.corrupt-*`。
如果进程在主文件与备份两次提交之间中断，备份可能落后一次状态更新。

两个文件都无法读取或保存失败时，请求会暂停。处理方法是修复目录写入权限、
磁盘空间或从可信备份恢复文件，再重载插件；不要通过删除用量文件来“恢复”，
这会丢失已有额度记录。

## 开发与发布

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
python tools/release.py check
python tools/release.py build
```

构建输出位于 `dist/`：安装 ZIP、源码 ZIP 和 SHA-256 校验文件。
打包采用明确文件清单，排除实际用量、凭据、本机配置和缓存。
推送与版本一致的标签（例如 `v1.0.0`）可触发 GitHub Release 工作流。

CI 配置覆盖 Windows / Linux 与 Python 3.10、3.12、3.13。
单元测试使用最小 AstrBot API 替身执行原始插件代码，
覆盖双周期边界、跨 Bot 并发、配置页重置、异常拦截、平台隔离、迁移、存储恢复和发布包结构。
真实平台适配器的安装、消息收发和模型拦截仍需在部署环境中验证。

贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)，
首次公开上传步骤见 [PUBLISH.md](PUBLISH.md)，
版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目采用 [MIT License](LICENSE)，允许使用、修改、分发和商业使用，
需保留版权声明及许可证文本。
