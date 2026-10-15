import datetime
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_usage_limiter"
PLUGIN_VERSION = "1.0.0"
PLUGIN_AUTHOR = "xmlans"
PLUGIN_REPO = "https://github.com/xmlans/astrbot_plugin_usage_limiter"
DEFAULT_WEEKLY_LIMIT = 200
DEFAULT_5H_LIMIT = 30
WINDOW_SECONDS = 5 * 60 * 60
LEGACY_EXHAUSTED_MSG = (
    "⚠️ 您的本周对话额度已用尽（{used}/{limit} 次）。\n"
    "本周剩余可用额度为 0。\n"
    "额度将于 {reset_time} 自动重置，请稍后再试或联系管理员提升额度。"
)
DEFAULT_EXHAUSTED_MSG = (
    "⚠️ {period}对话额度已用尽。\n"
    "5 小时：{five_hour_usage}；本周：{weekly_usage}。\n"
    "预计可再次使用：{reset_time}。\n"
    "请稍后再试或联系管理员提升额度。"
)
STORAGE_UNAVAILABLE_MSG = "⚠️ 用量记录暂时不可用，本次操作已暂停，请联系管理员。"


def make_user_key(platform_id: str, user_id: str) -> str:
    """使用平台实例 ID 隔离用户；转义分隔符，避免标识发生碰撞。"""
    if not isinstance(platform_id, str) or user_id is None:
        raise ValueError("平台 ID 和用户 ID 无效")
    platform_id, user_id = str(platform_id).strip(), str(user_id).strip()
    if not platform_id or not user_id:
        raise ValueError("平台 ID 和用户 ID 不能为空")
    return f"{quote(platform_id, safe='')}:{quote(user_id, safe='')}"


def resolve_user_key(target: str, platform_id: str = "") -> str:
    """完整标识可跨平台管理，裸 ID 仅属于显式指定的平台。"""
    if not isinstance(target, (str, int)) or isinstance(target, bool):
        raise ValueError("用户标识必须是字符串或整数")
    target = str(target).strip()
    if ":" in target:
        platform, _, user = target.partition(":")
        return make_user_key(unquote(platform), unquote(user))
    if not platform_id:
        raise ValueError("裸用户 ID 需要配置 legacy_platform_id，或改用平台ID:用户ID")
    return make_user_key(platform_id, target)


def parse_limit(value: Any) -> Optional[int]:
    """只接受整数或整数字符串，避免布尔值、浮点数和非法值绕过限制。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else -1


def restrictive_limit(values) -> int:
    """合并共享用户的重复额度时，使用最小的有限额度。"""
    finite = [value for value in values if value is not None and value > 0]
    return min(finite) if finite else -1


def canonical_user_key(target: str, platform_id: str = "", telegram_platform_ids=()) -> str:
    key = resolve_user_key(target, platform_id)
    platform, _, user = key.partition(":")
    if unquote(platform) in telegram_platform_ids:
        return make_user_key("telegram", unquote(user))
    return key


def normalize_config(config: Any) -> Dict[str, Any]:
    """验证双周期配置，并将已知 Telegram 实例标识归并为共享标识。"""
    if not isinstance(config, Mapping):
        logger.error("[UsageLimiter] 配置不是字典，使用默认限制")
        config = {}
    result = dict(config)
    platform = config.get("legacy_platform_id", "")
    result["legacy_platform_id"] = platform.strip() if isinstance(platform, str) else ""
    ids = config.get("telegram_platform_ids", [])
    result["telegram_platform_ids"] = (
        sorted({value.strip() for value in ids if isinstance(value, str) and value.strip()})
        if isinstance(ids, (list, tuple, set))
        else []
    )
    for option in ("enable", "enable_50_notice", "enable_90_notice", "enable_100_notice"):
        value = config.get(option)
        result[option] = value if isinstance(value, bool) else True
    result["reset_all_usage"] = config.get("reset_all_usage") is True
    for default_field, custom_field, fallback in (
        ("default_weekly_limit", "custom_limits", DEFAULT_WEEKLY_LIMIT),
        ("default_5h_limit", "custom_5h_limits", DEFAULT_5H_LIMIT),
    ):
        default = parse_limit(config.get(default_field, fallback))
        if default is None:
            logger.warning(f"[UsageLimiter] {default_field} 无效，回退为 {fallback} 次")
        result[default_field] = default if default is not None else fallback
        result[custom_field] = {}
        custom = config.get(custom_field, {})
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except (ValueError, TypeError):
                logger.warning(f"[UsageLimiter] {custom_field} JSON 无效，使用默认限制")
                custom = {}
        if isinstance(custom, Mapping):
            for target, value in custom.items():
                limit = parse_limit(value)
                if limit is None:
                    logger.warning(f"[UsageLimiter] 忽略用户 {target} 的无效额度")
                    continue
                try:
                    key = canonical_user_key(
                        target, result["legacy_platform_id"], result["telegram_platform_ids"]
                    )
                    previous = result[custom_field].get(key)
                    result[custom_field][key] = (
                        restrictive_limit((previous, limit)) if previous is not None else limit
                    )
                except ValueError as exc:
                    logger.warning(f"[UsageLimiter] 忽略无效自定义额度标识: {exc}")
    result["whitelist"] = []
    whitelist = config.get("whitelist", [])
    if isinstance(whitelist, (list, tuple)):
        for target in whitelist:
            try:
                result["whitelist"].append(
                    canonical_user_key(
                        target, result["legacy_platform_id"], result["telegram_platform_ids"]
                    )
                )
            except ValueError as exc:
                logger.warning(f"[UsageLimiter] 忽略无效白名单标识: {exc}")
    template = config.get("exhausted_msg")
    if not isinstance(template, str) or template == LEGACY_EXHAUSTED_MSG:
        result["exhausted_msg"] = DEFAULT_EXHAUSTED_MSG
    return result


class UsageStorageError(RuntimeError):
    """无法可靠读取或保存额度时，暂停请求，避免错误放行。"""


class UsageManager:
    """负责周周期计算、用户用量数据持久化与阈值状态管理"""

    def __init__(self, data_file: Path, legacy_platform_id: str = ""):
        self.data_file = data_file
        self.backup_file = data_file.with_suffix(data_file.suffix + ".bak")
        self.legacy_platform_id = legacy_platform_id
        self.telegram_platform_ids = set()
        self.storage_error: Optional[str] = None
        self.data: Dict[str, Any] = {"users": {}}
        self.load()

    @staticmethod
    def _read_data(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise ValueError("用量文件缺少有效的 users 字典")
        for record in data["users"].values():
            if not isinstance(record, dict) or not isinstance(record.get("week_key"), str):
                raise ValueError("用户记录格式无效")
            used = record.get("used")
            if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                raise ValueError("已用额度必须是非负整数")
            if "window_started_at" in record or "window_used" in record:
                if "window_started_at" not in record or "window_used" not in record:
                    raise ValueError("5 小时窗口记录不完整")
                started = record.get("window_started_at")
                window_used = record.get("window_used")
                if (
                    isinstance(window_used, bool)
                    or not isinstance(window_used, int)
                    or window_used < 0
                ):
                    raise ValueError("5 小时已用额度必须是非负整数")
                if started is None:
                    if window_used != 0:
                        raise ValueError("未开始的 5 小时窗口不能存在已用额度")
                elif (
                    isinstance(started, bool)
                    or not isinstance(started, (int, float))
                    or not math.isfinite(started)
                    or started < 0
                ):
                    raise ValueError("5 小时窗口时间无效")
        return data

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self) -> None:
        self.storage_error = None
        if not self.data_file.exists() and not self.backup_file.exists():
            self.data = {"users": {}}
            return
        try:
            self.data = self._read_data(self.data_file)
            return
        except Exception as exc:
            logger.error(f"[UsageLimiter] 加载用量数据失败，将尝试备份: {exc}")
        try:
            recovered = self._read_data(self.backup_file)
            if self.data_file.exists():
                damaged = self.data_file.with_name(
                    self.data_file.name + f".corrupt-{uuid.uuid4().hex}"
                )
                shutil.copyfile(self.data_file, damaged)
            self._atomic_write(self.data_file, json.dumps(recovered, ensure_ascii=False, indent=2))
            self.data = recovered
            logger.warning("[UsageLimiter] 已从备份恢复用量，损坏文件已保留")
        except Exception as exc:
            self.storage_error = str(exc)
            logger.error(f"[UsageLimiter] 无法恢复用量数据，暂停 LLM 请求: {exc}")

    def ensure_available(self) -> None:
        if self.storage_error is not None:
            raise UsageStorageError(self.storage_error)

    def save(self) -> None:
        self.ensure_available()
        try:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            self._atomic_write(self.data_file, payload)
            self._atomic_write(self.backup_file, payload)
        except Exception as exc:
            self.storage_error = str(exc)
            logger.error(f"[UsageLimiter] 保存用量失败，暂停请求直至重载: {exc}")
            raise UsageStorageError("用量保存失败，请修复存储后重载插件") from exc

    def _migrate_legacy_user(self, user_key: str) -> bool:
        """旧裸 ID 数据只迁移到管理员明确指定的平台，且保留其已用额度。"""
        platform, separator, user_id = user_key.partition(":")
        if not separator or user_key in self.data["users"]:
            return False
        user_id = unquote(user_id)
        if user_id not in self.data["users"]:
            return False
        if not self.legacy_platform_id:
            raise UsageStorageError("存在未迁移的旧用量，请配置 legacy_platform_id 后重载")
        if unquote(platform) == self.legacy_platform_id or (
            platform == "telegram" and self.legacy_platform_id in self.telegram_platform_ids
        ):
            self.data["users"][user_key] = self.data["users"].pop(user_id)
            return True
        return False

    @staticmethod
    def get_current_week_info(now: Optional[float] = None) -> Dict[str, Any]:
        """获取当前自然周（周一至周日）的 ISO 标识、起止日期及下周一刷新时间"""
        today = datetime.datetime.fromtimestamp(time.time() if now is None else now).date()
        year, week_num, weekday = today.isocalendar()
        week_key = f"{year}-W{week_num:02d}"

        # 本周周一与周日
        monday = today - datetime.timedelta(days=weekday - 1)
        sunday = monday + datetime.timedelta(days=6)
        # 下周一 00:00
        next_monday = monday + datetime.timedelta(days=7)

        return {
            "week_key": week_key,
            "year": year,
            "week_num": week_num,
            "start_date": monday.strftime("%Y-%m-%d"),
            "end_date": sunday.strftime("%Y-%m-%d"),
            "reset_time_str": f"{next_monday.strftime('%Y-%m-%d')} 00:00 (周一)",
            "reset_at": datetime.datetime.combine(next_monday, datetime.time()).timestamp(),
        }

    @staticmethod
    def reset_period(record: Dict[str, Any], period: str) -> None:
        prefix = "" if period == "week" else "window_"
        record["used" if period == "week" else "window_used"] = 0
        if period == "5h":
            record["window_started_at"] = None
        for threshold in ("50", "90", "100"):
            record[f"{prefix}notified_{threshold}"] = False

    def get_user_record(self, user_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """周周期和 5 小时窗口独立重置；查询不启动短期窗口。"""
        self.ensure_available()
        now = time.time() if now is None else now
        user_id = str(user_id)
        changed = self._migrate_legacy_user(user_id)
        current_week = self.get_current_week_info(now)["week_key"]
        record = self.data["users"].get(user_id)
        if record is None:
            record = {
                "week_key": current_week,
                "custom_limit": None,
                "custom_5h_limit": None,
                "last_active": None,
            }
            self.reset_period(record, "week")
            self.reset_period(record, "5h")
            self.data["users"][user_id] = record
            changed = True
        elif record["week_key"] != current_week:
            record["week_key"] = current_week
            self.reset_period(record, "week")
            changed = True
        if "window_used" not in record:
            self.reset_period(record, "5h")
            changed = True
        started = record["window_started_at"]
        if started is not None and now >= started + WINDOW_SECONDS:
            self.reset_period(record, "5h")
            changed = True
        if changed:
            self.save()
        return record

    def merge_telegram_records(self, user_key: str, now: Optional[float] = None) -> None:
        """合并已知 Telegram Bot 的旧记录；删除别名，避免重复累计。"""
        now = time.time() if now is None else now
        self.ensure_available()
        if self._migrate_legacy_user(user_key):
            self.save()
        _, _, user = user_key.partition(":")
        aliases = [
            make_user_key(platform, unquote(user))
            for platform in self.telegram_platform_ids
            if make_user_key(platform, unquote(user)) != user_key
            and make_user_key(platform, unquote(user)) in self.data["users"]
        ]
        raw_user = unquote(user)
        if raw_user in self.data["users"]:
            if not self.legacy_platform_id:
                raise UsageStorageError("存在未迁移的旧用量，请配置 legacy_platform_id 后重载")
            if (
                self.legacy_platform_id in self.telegram_platform_ids
                or self.legacy_platform_id == "telegram"
            ):
                aliases.append(raw_user)
        if not aliases:
            return
        # 先完成各记录的周期维护，再一次性合并并提交。
        target = self.get_user_record(user_key, now)
        records = [dict(target)] + [dict(self.get_user_record(key, now)) for key in aliases]
        target["used"] = sum(record["used"] for record in records)
        active = [record for record in records if record["window_started_at"] is not None]
        target["window_used"] = sum(record["window_used"] for record in active)
        target["window_started_at"] = max(
            (record["window_started_at"] for record in active), default=None
        )
        for field in ("custom_limit", "custom_5h_limit"):
            limits = [parse_limit(record.get(field)) for record in records]
            limits = [value for value in limits if value is not None]
            target[field] = restrictive_limit(limits) if limits else None
        for threshold in ("50", "90", "100"):
            target[f"notified_{threshold}"] = any(
                record.get(f"notified_{threshold}", False) for record in records
            )
            target[f"window_notified_{threshold}"] = any(
                record.get(f"window_notified_{threshold}", False) for record in active
            )
        target["last_active"] = max(
            (
                record["last_active"]
                for record in records
                if isinstance(record.get("last_active"), str)
            ),
            default=None,
        )
        for key in aliases:
            del self.data["users"][key]
        self.save()

    def get_effective_limit(
        self, user_id: str, config: Dict[str, Any], period: str = "week"
    ) -> int:
        """白名单优先；动态额度、配置额度、默认额度按周期分别生效。"""
        self.ensure_available()
        config = normalize_config(config)
        if user_id in config["whitelist"]:
            return -1
        if period == "week":
            saved, custom, default = "custom_limit", "custom_limits", "default_weekly_limit"
        elif period == "5h":
            saved, custom, default = "custom_5h_limit", "custom_5h_limits", "default_5h_limit"
        else:
            raise ValueError("未知额度周期")
        record = self.data["users"].get(user_id, {})
        limit = parse_limit(record.get(saved))
        if limit is not None:
            return limit
        return config[custom].get(user_id, config[default])

    def reset_all_usage(self) -> int:
        """清空所有记录的双周期用量，保留用户和持久化额度。"""
        self.ensure_available()
        week_key = self.get_current_week_info()["week_key"]
        for record in self.data["users"].values():
            record["week_key"] = week_key
            self.reset_period(record, "week")
            self.reset_period(record, "5h")
        self.save()
        return len(self.data["users"])

    @staticmethod
    def render_progress_bar(used: int, limit: int, length: int = 10) -> Tuple[str, float]:
        """渲染字符进度条与百分比"""
        if limit <= 0:
            return "[∞ 无限制]", 0.0

        percent = (used / limit) * 100
        capped_percent = min(100.0, percent)
        filled_length = int(round(length * (capped_percent / 100.0)))
        filled_length = min(length, max(0, filled_length))
        empty_length = length - filled_length

        bar = "█" * filled_length + "░" * empty_length
        return f"[{bar}] {percent:.1f}%", percent

    def get_usage_status(
        self, user_id: str, config: Dict[str, Any], now: Optional[float] = None
    ) -> Dict[str, Any]:
        now = time.time() if now is None else now
        record = self.get_user_record(user_id, now)
        week = self.get_current_week_info(now)
        window_reset = (
            record["window_started_at"] + WINDOW_SECONDS
            if record["window_started_at"] is not None
            else None
        )
        quotas = {}
        for period, label, used, reset_at, reset_text in (
            (
                "5h",
                "5 小时",
                record["window_used"],
                window_reset,
                datetime.datetime.fromtimestamp(window_reset).strftime("%Y-%m-%d %H:%M:%S")
                if window_reset is not None
                else "首次请求后 5 小时",
            ),
            ("week", "本周", record["used"], week["reset_at"], week["reset_time_str"]),
        ):
            limit = self.get_effective_limit(user_id, config, period)
            quotas[period] = {
                "period": period,
                "label": label,
                "used": used,
                "limit": limit,
                "reset_at": reset_at,
                "reset_time": reset_text,
                "blocked": limit > 0 and used >= limit,
            }
        blocked = [quota for quota in quotas.values() if quota["blocked"]]
        return {
            "quotas": quotas,
            "blocked": blocked,
            "allowed": not blocked,
            "reset_time": max(blocked, key=lambda q: q["reset_at"])["reset_time"]
            if blocked
            else None,
            "notices": [],
        }

    def check_and_consume(self, user_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """同步检查两个周期，全部允许时一起扣除一次，再原子保存。"""
        now = time.time()
        status = self.get_usage_status(user_id, config, now)
        if not status["allowed"]:
            return status
        record = self.data["users"][user_id]
        if record["window_started_at"] is None:
            record["window_started_at"] = now
        record["window_used"] += 1
        record["used"] += 1
        record["last_active"] = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        status = self.get_usage_status(user_id, config, now)
        # 最后一份额度仍可使用；只有下一次请求才被拦截。
        status["allowed"] = True
        for period, quota in status["quotas"].items():
            limit, used = quota["limit"], quota["used"]
            if limit <= 0:
                continue
            threshold = (
                "100"
                if used >= limit
                else "90"
                if used >= limit * 0.9
                else "50"
                if used >= limit * 0.5
                else None
            )
            prefix = "" if period == "week" else "window_"
            if threshold and not record.get(f"{prefix}notified_{threshold}", False):
                record[f"{prefix}notified_{threshold}"] = True
                status["notices"].append({**quota, "threshold": threshold})
        self.save()
        return status


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    "5 小时与每周双额度，Telegram 用户跨 Bot 共享，支持查询、提醒与管理",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class UsageLimiterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self._raw_config = dict(config) if isinstance(config, Mapping) else {}
        self.config = normalize_config(self._raw_config)

        # 数据与插件代码分离，升级插件不会覆盖用量。
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manager = UsageManager(
            self.data_dir / "usage_data.json", self.config["legacy_platform_id"]
        )
        self._refresh_telegram_ids()
        self._apply_config_reset(config)

    def _apply_config_reset(self, config: AstrBotConfig) -> None:
        if not self.config["reset_all_usage"]:
            return
        self.manager.ensure_available()
        if not callable(getattr(config, "save_config", None)):
            raise UsageStorageError("全量重置需要能够保存的一次性配置，请使用 AstrBot 配置页")
        # 先持久化关闭开关，再清空用量；后续重载不会误重置新产生的用量。
        config["reset_all_usage"] = False
        try:
            config.save_config()
        except Exception as exc:
            config["reset_all_usage"] = True
            raise UsageStorageError("无法关闭重置开关，未重置用量") from exc
        self._raw_config["reset_all_usage"] = False
        self.config["reset_all_usage"] = False
        count = self.manager.reset_all_usage()
        logger.info(f"[UsageLimiter] 已重置全部 {count} 位用户的 5 小时与每周用量")

    async def initialize(self) -> None:
        logger.info("[UsageLimiter] 用户用量控制插件已成功加载！")

    async def terminate(self) -> None:
        logger.info("[UsageLimiter] 用户用量控制插件正在卸载...")

    def _refresh_telegram_ids(self, event: Optional[AstrMessageEvent] = None) -> None:
        ids = set(normalize_config(self._raw_config)["telegram_platform_ids"])
        manager = getattr(self.context, "platform_manager", None)
        # 配置包含暂时禁用的 Bot，保证它们的旧记录也参与合并。
        for item in getattr(manager, "platforms_config", []):
            if isinstance(item, Mapping) and item.get("type") == "telegram" and item.get("id"):
                ids.add(str(item["id"]))
        for platform in getattr(manager, "platform_insts", []):
            meta = platform.meta()
            if meta.name == "telegram" and meta.id:
                ids.add(str(meta.id))
        if event is not None and event.get_platform_name() == "telegram":
            platform_id = event.get_platform_id()
            if not isinstance(platform_id, str) or not platform_id.strip():
                raise ValueError("Telegram 平台实例 ID 无效")
            ids.add(platform_id)
        self._raw_config["telegram_platform_ids"] = sorted(ids)
        self.config = normalize_config(self._raw_config)
        self.manager.telegram_platform_ids = ids

    def _target_key(self, event: AstrMessageEvent, target: str) -> str:
        self._refresh_telegram_ids(event)
        platform_id = event.get_platform_id()
        if event.get_platform_name() == "telegram":
            platform_id = "telegram"
        elif platform_id == "telegram":
            raise ValueError("非 Telegram 平台不能使用保留的实例 ID telegram")
        key = canonical_user_key(target, platform_id, self.manager.telegram_platform_ids)
        if key.startswith("telegram:"):
            self.manager.merge_telegram_records(key)
        return key

    def _user_key(self, event: AstrMessageEvent) -> str:
        # make_user_key 验证原始身份，防止空值成为共享记录。
        make_user_key(event.get_platform_id(), event.get_sender_id())
        return self._target_key(
            event,
            make_user_key(
                "telegram" if event.get_platform_name() == "telegram" else event.get_platform_id(),
                event.get_sender_id(),
            ),
        )

    @staticmethod
    async def _send_notice(event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:
            logger.warning(f"[UsageLimiter] 发送用量提示失败: {exc}")

    @filter.on_llm_request()
    async def intercept_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enable", True):
            return
        try:
            user_key = self._user_key(event)
            status = self.manager.check_and_consume(user_key, self.config)
        except Exception as exc:
            event.stop_event()
            logger.error(f"[UsageLimiter] 用量检查失败，已阻止本次请求: {exc}")
            await self._send_notice(event, STORAGE_UNAVAILABLE_MSG)
            return
        if not status["allowed"]:
            event.stop_event()
            lead = max(status["blocked"], key=lambda quota: quota["reset_at"])
            values = {
                "used": lead["used"],
                "limit": lead["limit"],
                "period": "、".join(quota["label"] for quota in status["blocked"]),
                "reset_time": status["reset_time"],
                "weekly_used": status["quotas"]["week"]["used"],
                "weekly_limit": status["quotas"]["week"]["limit"],
                "five_hour_used": status["quotas"]["5h"]["used"],
                "five_hour_limit": status["quotas"]["5h"]["limit"],
            }
            for period, field in (("5h", "five_hour_usage"), ("week", "weekly_usage")):
                quota = status["quotas"][period]
                values[field] = (
                    f"{quota['used']}/{quota['limit']} 次"
                    if quota["limit"] > 0
                    else f"{quota['used']} 次（无限制）"
                )
            try:
                text = self.config["exhausted_msg"].format(**values)
            except Exception as exc:
                logger.warning(f"[UsageLimiter] 拦截提示模板无效，使用默认提示: {exc}")
                text = DEFAULT_EXHAUSTED_MSG.format(**values)
            await self._send_notice(event, text)
            return
        notices = []
        for quota in status["notices"]:
            threshold = quota["threshold"]
            if self.config[f"enable_{threshold}_notice"]:
                remaining = max(0, quota["limit"] - quota["used"])
                notices.append(
                    f"⚠️ {quota['label']}用量达到 {threshold}% 阈值"
                    f"（{quota['used']}/{quota['limit']} 次，剩余 {remaining} 次）。\n"
                    f"该周期重置时间：{quota['reset_time']}。"
                )
        if notices:
            await self._send_notice(event, "\n\n".join(notices))

    def _usage_lines(self, user_key: str) -> list:
        status = self.manager.get_usage_status(user_key, self.config)
        lines = [f"🔑 用户标识：{user_key}"]
        for quota in status["quotas"].values():
            used, limit = quota["used"], quota["limit"]
            bar, _ = self.manager.render_progress_bar(used, limit)
            limit_text = f"{limit} 次" if limit > 0 else "无限制"
            remaining = f"{max(0, limit - used)} 次" if limit > 0 else "无限制"
            lines.extend(
                [
                    f"\n📊 {quota['label']}已用：{used} / {limit_text}",
                    f"🎯 剩余：{remaining}",
                    f"📈 进度：{bar}",
                    f"⏳ 重置：{quota['reset_time']}",
                ]
            )
        if not self.config["enable"]:
            lines.append("\n用量控制已关闭，以上为已记录的统计。")
        elif status["blocked"]:
            labels = "、".join(quota["label"] for quota in status["blocked"])
            lines.append(f"\n🔴 {labels}额度已用尽，预计可再次使用：{status['reset_time']}")
        else:
            lines.append("\n🟢 两个周期均有可用额度")
        return lines

    @filter.command("usage", alias={"用量", "额度", "查用量"})
    async def cmd_usage(self, event: AstrMessageEvent):
        user_key = ""
        try:
            user_key = self._user_key(event)
            lines = [
                "📊 【对话用量统计】",
                f"👤 用户：{event.get_sender_name() or event.get_sender_id()}",
            ]
            lines.extend(self._usage_lines(user_key))
        except (ValueError, UsageStorageError) as exc:
            logger.error(f"[UsageLimiter] 查询用量失败: {exc}")
            identity = f"\n🔑 用户标识：{user_key}" if user_key else ""
            yield event.plain_result(STORAGE_UNAVAILABLE_MSG + identity)
            return
        yield event.plain_result("\n".join(lines))

    async def _set_limit(self, event: AstrMessageEvent, target: str, value: int, period: str):
        try:
            key = self._target_key(event, target)
            limit = parse_limit(value)
            if limit is None:
                raise ValueError("额度必须是整数")
            record = self.manager.get_user_record(key)
            record["custom_limit" if period == "week" else "custom_5h_limit"] = limit
            self.manager.save()
        except (ValueError, UsageStorageError) as exc:
            yield event.plain_result(f"⚠️ 设置额度失败：{exc}")
            return
        label = "每周" if period == "week" else "5 小时"
        description = f"{limit} 次" if limit > 0 else "无限制"
        yield event.plain_result(f"✅ 已将用户 {key} 的{label}额度调整为：{description}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_set")
    async def cmd_usage_set(self, event: AstrMessageEvent, target_user: str, new_limit: int):
        """设置每周额度：/usage_set <用户标识> <整数额度>"""
        async for result in self._set_limit(event, target_user, new_limit, "week"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_set5h")
    async def cmd_usage_set5h(self, event: AstrMessageEvent, target_user: str, new_limit: int):
        """设置 5 小时额度：/usage_set5h <用户标识> <整数额度>"""
        async for result in self._set_limit(event, target_user, new_limit, "5h"):
            yield result

    async def _reset_usage(self, event: AstrMessageEvent, target: str, periods: tuple):
        try:
            key = self._target_key(event, target)
            record = self.manager.get_user_record(key)
            for period in periods:
                self.manager.reset_period(record, period)
            self.manager.save()
        except (ValueError, UsageStorageError) as exc:
            yield event.plain_result(f"⚠️ 重置额度失败：{exc}")
            return
        label = "、".join("每周" if period == "week" else "5 小时" for period in periods)
        yield event.plain_result(f"✅ 已重置用户 {key} 的{label}用量与提醒状态，保留自定义额度。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_reset")
    async def cmd_usage_reset(self, event: AstrMessageEvent, target_user: str):
        """同时重置两个周期：/usage_reset <用户标识>"""
        async for result in self._reset_usage(event, target_user, ("5h", "week")):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_reset5h")
    async def cmd_usage_reset5h(self, event: AstrMessageEvent, target_user: str):
        """仅重置 5 小时窗口。"""
        async for result in self._reset_usage(event, target_user, ("5h",)):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_resetweek")
    async def cmd_usage_resetweek(self, event: AstrMessageEvent, target_user: str):
        """仅重置每周用量。"""
        async for result in self._reset_usage(event, target_user, ("week",)):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("usage_check")
    async def cmd_usage_check(self, event: AstrMessageEvent, target_user: str):
        try:
            key = self._target_key(event, target_user)
            lines = [f"🔍 用户 {key} 用量信息"]
            lines.extend(self._usage_lines(key))
            lines.append(
                f"\n最后活跃：{self.manager.data['users'][key].get('last_active') or '无'}"
            )
        except (ValueError, UsageStorageError) as exc:
            yield event.plain_result(f"⚠️ 查询额度失败：{exc}")
            return
        yield event.plain_result("\n".join(lines))
