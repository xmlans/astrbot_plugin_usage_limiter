"""Regression checks for the real plugin with minimal AstrBot API bindings."""

import asyncio
import datetime
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_plugin():
    class Star:
        # No get_data_dir() or instance logger: directory access belongs to StarTools.
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            raise AssertionError("Supply an isolated directory in each test")

    def decorator(*args, **kwargs):
        return lambda function: function

    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.star",
            "astrbot.api.event",
            "astrbot.api.provider",
        )
    }
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].StarTools = StarTools
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        on_llm_request=decorator,
        command=decorator,
        permission_type=decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    modules["astrbot.api.provider"].ProviderRequest = object
    modules["astrbot.api"].AstrBotConfig = dict
    logger = logging.getLogger("usage_limiter_tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    modules["astrbot.api"].logger = logger
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("usage_limiter_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()


class Event:
    def __init__(self, platform="tg_bot", user="123", send_error=False, platform_name="unknown"):
        self.platform, self.user, self.send_error = platform, user, send_error
        self.platform_name = platform_name
        self.stopped = False
        self.sent = []
        self.stopped_at_send = []

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return self.platform_name

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return "Test user"

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.stopped_at_send.append(self.stopped)
        if self.send_error:
            raise RuntimeError("Platform send failed")
        self.sent.append(text)

    def stop_event(self):
        self.stopped = True


class UsageLimiterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="usage-limiter-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "usage_data.json"

    def plugin(self, config=None, context=None):
        with patch.object(PLUGIN.StarTools, "get_data_dir", return_value=self.root) as get_dir:
            plugin = PLUGIN.UsageLimiterPlugin(
                context or object(), config if config is not None else {}
            )
        get_dir.assert_called_once_with(PLUGIN.PLUGIN_NAME)
        return plugin

    async def request(self, plugin, event=None):
        event = event or Event()
        await plugin.intercept_llm_request(event, object())
        return event

    async def outputs(self, handler, *args):
        return [result async for result in handler(*args)]

    def legacy_record(self):
        manager = PLUGIN.UsageManager(self.path)
        record = manager.get_user_record("123")
        record.update(used=2, custom_limit=2, notified_100=True)
        manager.save()

    async def test_directory_api_and_lifecycle(self):
        plugin = self.plugin()
        self.assertEqual(plugin.data_dir, self.root)
        await plugin.initialize()
        await plugin.terminate()

    async def test_thresholds_and_exhaustion(self):
        plugin = self.plugin({"default_weekly_limit": 50, "default_5h_limit": 0})
        notices = []
        for number in range(1, 51):
            event = await self.request(plugin)
            self.assertFalse(event.stopped)
            if event.sent:
                notices.append(number)
        self.assertEqual(notices, [25, 45, 50])
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertEqual(plugin.manager.get_user_record("tg_bot:123")["used"], 50)

    async def test_bad_templates_block_before_sending_and_use_default(self):
        for user, template in enumerate(("{remaining}", "quota {", "{}", "{used:bad}")):
            with self.subTest(template=template):
                plugin = self.plugin({"default_weekly_limit": 1, "exhausted_msg": template})
                await self.request(plugin, Event(user=str(user)))
                event = await self.request(plugin, Event(user=str(user)))
                self.assertTrue(event.stopped)
                self.assertEqual(event.stopped_at_send, [True])
                self.assertIn("1/1", event.sent[0])

    async def test_send_failure_cannot_unblock_user(self):
        plugin = self.plugin({"default_weekly_limit": 1})
        await self.request(plugin)
        event = await self.request(plugin, Event(send_error=True))
        self.assertTrue(event.stopped)
        self.assertEqual(event.stopped_at_send, [True])

    async def test_invalid_custom_values_use_finite_default(self):
        for user, value in enumerate(("abc", None, True, 1.5, {}, [])):
            with self.subTest(value=value):
                plugin = self.plugin(
                    {
                        "default_weekly_limit": 1,
                        "custom_limits": {f"tg_bot:{user}": value},
                    }
                )
                self.assertFalse((await self.request(plugin, Event(user=str(user)))).stopped)
                self.assertTrue((await self.request(plugin, Event(user=str(user)))).stopped)

    async def test_invalid_default_and_collections_are_safe(self):
        for config in (
            None,
            [],
            {"default_weekly_limit": "abc", "whitelist": "123", "custom_limits": []},
        ):
            with self.subTest(config=config):
                normalized = PLUGIN.normalize_config(config)
                self.assertEqual(normalized["whitelist"], [])
                self.assertEqual(normalized["custom_limits"], {})
                self.assertEqual(normalized["default_weekly_limit"], 200)

    async def test_invalid_persisted_limit_does_not_bypass(self):
        plugin = self.plugin({"default_weekly_limit": 1})
        plugin.manager.get_user_record("tg_bot:123")["custom_limit"] = "abc"
        self.assertFalse((await self.request(plugin)).stopped)
        self.assertTrue((await self.request(plugin)).stopped)

    async def test_json_text_custom_limits_are_applied_and_scoped(self):
        plugin = self.plugin(
            {
                "default_weekly_limit": 1,
                "custom_limits": '{"tg_bot:123": 2, "qq_bot:123": 0}',
            }
        )
        for _ in range(2):
            self.assertFalse((await self.request(plugin)).stopped)
        self.assertTrue((await self.request(plugin)).stopped)
        for _ in range(3):
            self.assertFalse((await self.request(plugin, Event("qq_bot"))).stopped)
        self.assertEqual(plugin.manager.get_effective_limit("other:123", plugin.config), 1)

    async def test_invalid_json_custom_limits_cannot_bypass_default(self):
        for user, value in enumerate(("broken", "[]", "null", "true", '{"tg_bot:123": "bad"}')):
            with self.subTest(value=value):
                plugin = self.plugin({"default_weekly_limit": 1, "custom_limits": value})
                self.assertFalse((await self.request(plugin, Event(user=str(user)))).stopped)
                self.assertTrue((await self.request(plugin, Event(user=str(user)))).stopped)

    async def test_schema_defaults_load_without_runtime_dependencies(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        defaults = {key: field["default"] for key, field in schema.items()}
        plugin = self.plugin(defaults)
        self.assertEqual(plugin.config["default_weekly_limit"], 200)
        self.assertEqual(plugin.config["custom_limits"], {})
        self.assertFalse((await self.request(plugin)).stopped)

    async def test_numeric_strings_and_explicit_unlimited(self):
        plugin = self.plugin({"custom_limits": {"tg_bot:123": "2", "qq_bot:123": 0}})
        self.assertEqual(plugin.manager.get_effective_limit("tg_bot:123", plugin.config), 2)
        self.assertEqual(plugin.manager.get_effective_limit("qq_bot:123", plugin.config), -1)

    async def test_same_raw_id_has_independent_quotas(self):
        plugin = self.plugin({"default_weekly_limit": 1})
        for platform in ("tg_bot", "qq_bot"):
            self.assertFalse((await self.request(plugin, Event(platform))).stopped)
        for platform in ("tg_bot", "qq_bot"):
            self.assertTrue((await self.request(plugin, Event(platform))).stopped)

    async def test_whitelist_and_custom_limits_are_scoped(self):
        plugin = self.plugin(
            {
                "default_weekly_limit": 1,
                "whitelist": ["tg_bot:123"],
                "custom_limits": {"qq_bot:123": 2},
            }
        )
        for _ in range(3):
            self.assertFalse((await self.request(plugin, Event("tg_bot"))).stopped)
        for _ in range(2):
            self.assertFalse((await self.request(plugin, Event("qq_bot"))).stopped)
        self.assertTrue((await self.request(plugin, Event("qq_bot"))).stopped)
        self.assertEqual(plugin.manager.get_effective_limit("other:123", plugin.config), 1)

    async def test_bare_config_ids_need_explicit_platform(self):
        config = {"whitelist": ["123"], "custom_limits": {"456": 7}}
        normalized = PLUGIN.normalize_config(config)
        self.assertEqual(normalized["whitelist"], [])
        self.assertEqual(normalized["custom_limits"], {})
        normalized = PLUGIN.normalize_config({**config, "legacy_platform_id": "tg_bot"})
        self.assertEqual(normalized["whitelist"], ["tg_bot:123"])
        self.assertEqual(normalized["custom_limits"], {"tg_bot:456": 7})

    async def test_key_encoding_prevents_collisions(self):
        first, second = PLUGIN.make_user_key("a:b", "c"), PLUGIN.make_user_key("a", "b:c")
        self.assertNotEqual(first, second)
        self.assertEqual(PLUGIN.resolve_user_key(first), first)
        self.assertEqual(PLUGIN.resolve_user_key(second), second)

    async def test_legacy_migration_preserves_quota_and_settings(self):
        self.legacy_record()
        plugin = self.plugin({"legacy_platform_id": "tg_bot"})
        self.assertFalse((await self.request(plugin, Event("qq_bot"))).stopped)
        self.assertTrue((await self.request(plugin, Event("tg_bot"))).stopped)
        record = plugin.manager.get_user_record("tg_bot:123")
        self.assertEqual((record["used"], record["custom_limit"]), (2, 2))
        self.assertTrue(record["notified_100"])
        self.assertNotIn("123", plugin.manager.data["users"])

    async def test_unassigned_legacy_data_is_preserved_and_blocks(self):
        self.legacy_record()
        original = self.path.read_bytes()
        plugin = self.plugin()
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(plugin.manager.data["users"]["123"]["used"], 2)
        output = await self.outputs(plugin.cmd_usage, Event())
        self.assertIn("tg_bot:123", output[0])

    async def test_missing_identity_blocks_without_using_shared_record(self):
        plugin = self.plugin()
        for platform, user in ((None, "123"), ("", "123"), ("tg_bot", "")):
            with self.subTest(platform=platform, user=user):
                self.assertTrue((await self.request(plugin, Event(platform, user))).stopped)
        self.assertEqual(plugin.manager.data["users"], {})

    async def test_admin_targets_current_or_explicit_platform(self):
        plugin = self.plugin()
        await self.outputs(plugin.cmd_usage_set, Event("tg_bot"), "123", 2)
        await self.outputs(plugin.cmd_usage_set, Event("qq_bot"), "123", 7)
        self.assertEqual(plugin.manager.get_effective_limit("tg_bot:123", plugin.config), 2)
        self.assertEqual(plugin.manager.get_effective_limit("qq_bot:123", plugin.config), 7)
        await self.request(plugin)
        await self.outputs(plugin.cmd_usage_reset, Event("qq_bot"), "tg_bot:123")
        self.assertEqual(plugin.manager.get_user_record("tg_bot:123")["used"], 0)
        output = await self.outputs(plugin.cmd_usage_check, Event(), "qq_bot:123")
        self.assertIn("7 次", output[0])

    async def test_usage_displays_user_key(self):
        output = await self.outputs(self.plugin().cmd_usage, Event())
        self.assertIn("tg_bot:123", output[0])

    async def test_atomic_replace_failure_preserves_committed_data(self):
        plugin = self.plugin({"default_weekly_limit": 1})
        await self.request(plugin)
        original = self.path.read_bytes()
        with patch.object(PLUGIN.os, "replace", side_effect=OSError("Disk error")):
            with self.assertRaises(PLUGIN.UsageStorageError):
                plugin.manager.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertTrue((await self.request(self.plugin({"default_weekly_limit": 1}))).stopped)

    async def test_flush_failure_preserves_committed_data(self):
        plugin = self.plugin()
        await self.request(plugin)
        original = self.path.read_bytes()
        with patch.object(PLUGIN.os, "fsync", side_effect=OSError("Flush error")):
            with self.assertRaises(PLUGIN.UsageStorageError):
                plugin.manager.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    async def test_backup_recovers_latest_quota_and_preserves_damaged_file(self):
        self.legacy_record()
        self.path.write_text('{"users":', encoding="utf-8")
        manager = PLUGIN.UsageManager(self.path)
        self.assertIsNone(manager.storage_error)
        record = manager.get_user_record("123")
        self.assertEqual((record["used"], record["custom_limit"]), (2, 2))
        damaged = list(self.root.glob("usage_data.json.corrupt-*"))
        self.assertEqual(len(damaged), 1)
        self.assertEqual(damaged[0].read_text(encoding="utf-8"), '{"users":')
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), manager.data)

    async def test_missing_main_file_recovers_backup(self):
        self.legacy_record()
        self.path.unlink()
        manager = PLUGIN.UsageManager(self.path)
        self.assertEqual(manager.get_user_record("123")["used"], 2)

    async def test_unrecoverable_corruption_blocks_without_overwriting(self):
        self.path.write_text("broken", encoding="utf-8")
        self.path.with_suffix(".json.bak").write_text("broken backup", encoding="utf-8")
        plugin = self.plugin()
        self.assertTrue((await self.request(plugin, Event(send_error=True))).stopped)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken")
        with self.assertRaises(PLUGIN.UsageStorageError):
            plugin.manager.get_user_record("tg_bot:123")

    async def test_invalid_record_is_not_silently_reset(self):
        data = {"users": {"tg_bot:123": {"week_key": "2026-W40", "used": "abc"}}}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        plugin = self.plugin()
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertIsNotNone(plugin.manager.storage_error)

    async def test_save_failure_blocks_request_and_admin_reports_failure(self):
        plugin = self.plugin()
        plugin.manager.get_user_record("tg_bot:123")
        with patch.object(PLUGIN.os, "replace", side_effect=OSError("Disk error")):
            self.assertTrue((await self.request(plugin)).stopped)
        output = await self.outputs(plugin.cmd_usage_set, Event(), "123", 100)
        self.assertIn("失败", output[0])
        self.assertNotIn("✅", output[0])

    async def test_rollover_keeps_custom_quota(self):
        plugin = self.plugin()
        record = plugin.manager.get_user_record("tg_bot:123")
        record.update(week_key="2000-W01", used=50, custom_limit=7, notified_100=True)
        record = plugin.manager.get_user_record("tg_bot:123")
        self.assertEqual((record["used"], record["custom_limit"]), (0, 7))
        self.assertFalse(record["notified_100"])

    async def test_disabled_plugin_does_not_consume(self):
        plugin = self.plugin({"enable": False})
        self.assertFalse((await self.request(plugin)).stopped)
        self.assertEqual(plugin.manager.data["users"], {})

    async def test_concurrent_requests_respect_quota(self):
        plugin = self.plugin({"default_weekly_limit": 5})
        events = await asyncio.gather(*(self.request(plugin) for _ in range(12)))
        self.assertEqual(sum(not event.stopped for event in events), 5)
        self.assertEqual(plugin.manager.get_user_record("tg_bot:123")["used"], 5)

    def freeze_clock(self, moment=None):
        moment = moment or datetime.datetime(2026, 10, 7, 12)
        clock_patch = patch.object(PLUGIN.time, "time", return_value=moment.timestamp())
        self.addCleanup(clock_patch.stop)
        return clock_patch.start()

    @staticmethod
    def telegram_context():
        return types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(
                platforms_config=[
                    {"type": "telegram", "id": "tg_a"},
                    {"type": "telegram", "id": "tg_b", "enable": False},
                    {"type": "aiocqhttp", "id": "qq_bot"},
                ],
                platform_insts=[],
            )
        )

    def saved_config(self, values, fail=False):
        path = self.root / "plugin_config.json"

        class SavedConfig(dict):
            def save_config(config):
                if fail:
                    raise OSError("Configuration storage failed")
                path.write_text(json.dumps(config), encoding="utf-8")

        return SavedConfig(values)

    async def test_five_hour_cap_and_exact_reset_boundary(self):
        clock = self.freeze_clock()
        plugin = self.plugin()
        for _ in range(30):
            self.assertFalse((await self.request(plugin)).stopped)
        self.assertTrue((await self.request(plugin)).stopped)
        record = plugin.manager.data["users"]["tg_bot:123"]
        self.assertEqual((record["window_used"], record["used"]), (30, 30))
        start = record["window_started_at"]
        clock.return_value = start + PLUGIN.WINDOW_SECONDS - 1
        self.assertTrue((await self.request(plugin)).stopped)
        clock.return_value += 1
        self.assertFalse((await self.request(plugin)).stopped)
        self.assertEqual((record["window_used"], record["used"]), (1, 31))
        self.assertEqual(record["window_started_at"], clock.return_value)

    async def test_query_does_not_start_window_and_inactivity_starts_fresh_window(self):
        clock = self.freeze_clock()
        plugin = self.plugin()
        await self.outputs(plugin.cmd_usage, Event())
        record = plugin.manager.data["users"]["tg_bot:123"]
        self.assertIsNone(record["window_started_at"])
        clock.return_value += 3600
        await self.request(plugin)
        self.assertEqual(record["window_started_at"], clock.return_value)
        clock.return_value += PLUGIN.WINDOW_SECONDS + 123
        await self.outputs(plugin.cmd_usage, Event())
        self.assertEqual((record["window_started_at"], record["window_used"]), (None, 0))
        self.assertEqual(record["used"], 1)
        await self.request(plugin)
        self.assertEqual(record["window_started_at"], clock.return_value)

    async def test_weekly_block_does_not_charge_or_start_new_short_window(self):
        clock = self.freeze_clock()
        plugin = self.plugin({"default_weekly_limit": 1, "default_5h_limit": 10})
        await self.request(plugin)
        clock.return_value += PLUGIN.WINDOW_SECONDS
        event = await self.request(plugin)
        self.assertTrue(event.stopped)
        record = plugin.manager.data["users"]["tg_bot:123"]
        self.assertEqual((record["used"], record["window_used"]), (1, 0))
        self.assertIsNone(record["window_started_at"])
        self.assertIn("本周", event.sent[0])

    async def test_week_rollover_preserves_active_short_window(self):
        clock = self.freeze_clock(datetime.datetime(2026, 10, 11, 23, 30))
        plugin = self.plugin({"default_5h_limit": 2})
        await self.request(plugin)
        await self.request(plugin)
        record = plugin.manager.data["users"]["tg_bot:123"]
        start = record["window_started_at"]
        clock.return_value += 3600
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertEqual((record["used"], record["window_used"]), (0, 2))
        self.assertEqual(record["window_started_at"], start)

    async def test_both_exhausted_report_later_reset(self):
        self.freeze_clock()
        plugin = self.plugin({"default_weekly_limit": 1, "default_5h_limit": 1})
        await self.request(plugin)
        event = await self.request(plugin)
        status = plugin.manager.get_usage_status("tg_bot:123", plugin.config)
        self.assertTrue(event.stopped)
        self.assertEqual(len(status["blocked"]), 2)
        self.assertEqual(status["reset_time"], status["quotas"]["week"]["reset_time"])
        self.assertIn("5 小时、本周", event.sent[0])
        self.assertIn(status["reset_time"], event.sent[0])

    async def test_restart_cannot_refresh_active_window(self):
        self.freeze_clock()
        plugin = self.plugin({"default_5h_limit": 1})
        await self.request(plugin)
        start = plugin.manager.data["users"]["tg_bot:123"]["window_started_at"]
        plugin = self.plugin({"default_5h_limit": 1})
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertEqual(plugin.manager.data["users"]["tg_bot:123"]["window_started_at"], start)

    async def test_invalid_short_limits_fall_back_to_finite_default(self):
        for value in ("bad", None, True, 1.5):
            normalized = PLUGIN.normalize_config(
                {
                    "default_5h_limit": value,
                    "custom_5h_limits": {"tg_bot:123": value},
                }
            )
            self.assertEqual(normalized["default_5h_limit"], 30)
            self.assertEqual(normalized["custom_5h_limits"], {})
        plugin = self.plugin(
            {
                "default_5h_limit": 1,
                "custom_5h_limits": '{"tg_bot:123": "invalid"}',
            }
        )
        await self.request(plugin)
        self.assertTrue((await self.request(plugin)).stopped)

    async def test_unlimited_short_period_still_enforces_weekly_cap(self):
        plugin = self.plugin({"default_5h_limit": 0, "default_weekly_limit": 2})
        await self.request(plugin)
        await self.request(plugin)
        self.assertTrue((await self.request(plugin)).stopped)
        self.assertEqual(plugin.manager.get_effective_limit("tg_bot:123", plugin.config, "5h"), -1)

    async def test_unlimited_week_still_enforces_short_period(self):
        plugin = self.plugin({"default_5h_limit": 2, "default_weekly_limit": 0})
        await self.request(plugin)
        await self.request(plugin)
        self.assertTrue((await self.request(plugin)).stopped)

    async def test_short_notices_reset_independently(self):
        clock = self.freeze_clock()
        plugin = self.plugin({"default_5h_limit": 10, "default_weekly_limit": 0})
        notices = []
        for number in range(1, 11):
            event = await self.request(plugin)
            if event.sent:
                notices.append(number)
                self.assertIn("5 小时", event.sent[0])
        self.assertEqual(notices, [5, 9, 10])
        clock.return_value += PLUGIN.WINDOW_SECONDS
        for _ in range(4):
            self.assertEqual((await self.request(plugin)).sent, [])
        self.assertIn("50%", (await self.request(plugin)).sent[0])
        self.assertEqual(plugin.manager.data["users"]["tg_bot:123"]["used"], 15)

    async def test_telegram_bots_share_both_caps_but_other_users_and_platforms_do_not(self):
        self.freeze_clock()
        plugin = self.plugin({"default_5h_limit": 2}, self.telegram_context())
        a = Event("tg_a", platform_name="telegram")
        b = Event("tg_b", platform_name="telegram")
        self.assertFalse((await self.request(plugin, a)).stopped)
        self.assertFalse((await self.request(plugin, b)).stopped)
        self.assertTrue(
            (await self.request(plugin, Event("tg_a", platform_name="telegram"))).stopped
        )
        self.assertFalse(
            (
                await self.request(plugin, Event("tg_b", user="456", platform_name="telegram"))
            ).stopped
        )
        self.assertFalse(
            (await self.request(plugin, Event("qq_bot", platform_name="aiocqhttp"))).stopped
        )
        shared = plugin.manager.data["users"]["telegram:123"]
        self.assertEqual((shared["window_used"], shared["used"]), (2, 2))
        self.assertEqual(
            set(plugin.manager.data["users"]), {"telegram:123", "telegram:456", "qq_bot:123"}
        )

    async def test_concurrent_telegram_bots_cannot_exceed_shared_short_cap(self):
        self.freeze_clock()
        plugin = self.plugin(context=self.telegram_context())
        events = await asyncio.gather(
            *(
                self.request(
                    plugin, Event("tg_a" if number % 2 else "tg_b", platform_name="telegram")
                )
                for number in range(45)
            )
        )
        self.assertEqual(sum(not event.stopped for event in events), 30)
        record = plugin.manager.data["users"]["telegram:123"]
        self.assertEqual((record["used"], record["window_used"]), (30, 30))

    async def test_telegram_whitelist_and_old_config_aliases_are_shared(self):
        plugin = self.plugin(
            {
                "default_5h_limit": 1,
                "whitelist": ["tg_a:123"],
                "custom_limits": '{"tg_a:456": 50, "tg_b:456": 10}',
                "custom_5h_limits": '{"tg_b:456": 2}',
            },
            self.telegram_context(),
        )
        for platform in ("tg_a", "tg_b", "tg_b"):
            self.assertFalse(
                (await self.request(plugin, Event(platform, platform_name="telegram"))).stopped
            )
        self.assertEqual(
            plugin.manager.get_effective_limit("telegram:123", plugin.config, "5h"), -1
        )
        self.assertEqual(plugin.manager.get_effective_limit("telegram:456", plugin.config), 10)
        self.assertEqual(plugin.manager.get_effective_limit("telegram:456", plugin.config, "5h"), 2)

    async def test_admin_short_and_weekly_limits_and_resets_apply_across_bots(self):
        self.freeze_clock()
        plugin = self.plugin(context=self.telegram_context())
        admin = Event("tg_a", platform_name="telegram")
        await self.outputs(plugin.cmd_usage_set, admin, "123", 7)
        await self.outputs(plugin.cmd_usage_set5h, admin, "123", 2)
        await self.request(plugin, Event("tg_b", platform_name="telegram"))
        await self.request(plugin, Event("tg_a", platform_name="telegram"))
        self.assertTrue(
            (await self.request(plugin, Event("tg_b", platform_name="telegram"))).stopped
        )
        await self.outputs(plugin.cmd_usage_resetweek, admin, "tg_b:123")
        record = plugin.manager.data["users"]["telegram:123"]
        self.assertEqual((record["used"], record["window_used"]), (0, 2))
        await self.outputs(plugin.cmd_usage_reset5h, admin, "telegram:123")
        self.assertEqual((record["used"], record["window_used"]), (0, 0))
        self.assertEqual((record["custom_limit"], record["custom_5h_limit"]), (7, 2))

    async def test_upgrade_old_telegram_records_merges_counts_once(self):
        self.freeze_clock()
        manager = PLUGIN.UsageManager(self.path)
        week = manager.get_current_week_info()["week_key"]
        manager.data = {
            "users": {
                "tg_a:123": {"week_key": week, "used": 3, "custom_limit": 10, "notified_50": True},
                "tg_b:123": {"week_key": week, "used": 4, "custom_limit": 20},
            }
        }
        manager.save()
        plugin = self.plugin(context=self.telegram_context())
        await self.outputs(plugin.cmd_usage, Event("tg_a", platform_name="telegram"))
        record = plugin.manager.data["users"]["telegram:123"]
        self.assertEqual(
            (record["used"], record["window_used"], record["custom_limit"]), (7, 0, 10)
        )
        self.assertTrue(record["notified_50"])
        self.assertIsNone(record["window_started_at"])
        self.assertEqual(set(plugin.manager.data["users"]), {"telegram:123"})
        plugin = self.plugin(context=self.telegram_context())
        await self.outputs(plugin.cmd_usage, Event("tg_b", platform_name="telegram"))
        self.assertEqual(plugin.manager.data["users"]["telegram:123"]["used"], 7)

    async def test_telegram_legacy_raw_id_is_not_lost_when_shared_record_exists(self):
        self.freeze_clock()
        manager = PLUGIN.UsageManager(self.path)
        manager.get_user_record("telegram:123")["used"] = 3
        manager.get_user_record("123")["used"] = 2
        manager.save()
        plugin = self.plugin({"legacy_platform_id": "tg_a"}, self.telegram_context())
        await self.outputs(plugin.cmd_usage, Event("tg_b", platform_name="telegram"))
        self.assertEqual(plugin.manager.data["users"]["telegram:123"]["used"], 5)
        self.assertNotIn("123", plugin.manager.data["users"])

    async def test_multiple_active_telegram_windows_merge_conservatively(self):
        clock = self.freeze_clock()
        manager = PLUGIN.UsageManager(self.path)
        for platform, offset in (("tg_a", 100), ("tg_b", 50)):
            record = manager.get_user_record(platform + ":123")
            record.update(used=1, window_used=1, window_started_at=clock.return_value - offset)
        manager.save()
        plugin = self.plugin({"default_5h_limit": 2}, self.telegram_context())
        self.assertTrue(
            (await self.request(plugin, Event("tg_a", platform_name="telegram"))).stopped
        )
        record = plugin.manager.data["users"]["telegram:123"]
        self.assertEqual(record["window_used"], 2)
        self.assertEqual(record["window_started_at"], clock.return_value - 50)

    async def test_malformed_window_data_blocks_instead_of_refreshing(self):
        week = PLUGIN.UsageManager.get_current_week_info()["week_key"]
        for fields in (
            {"window_used": 0},
            {"window_used": 1, "window_started_at": None},
            {"window_used": True, "window_started_at": 1},
            {"window_used": 1, "window_started_at": "bad"},
            {"window_used": 1, "window_started_at": float("nan")},
        ):
            self.path.write_text(
                json.dumps(
                    {
                        "users": {
                            "tg_bot:123": {
                                "week_key": week,
                                "used": 1,
                                **fields,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            plugin = self.plugin()
            self.assertTrue((await self.request(plugin)).stopped)
            self.assertIsNotNone(plugin.manager.storage_error)

    async def test_clock_rollback_does_not_grant_new_short_quota(self):
        clock = self.freeze_clock()
        plugin = self.plugin({"default_5h_limit": 1})
        await self.request(plugin)
        clock.return_value -= 3600
        self.assertTrue((await self.request(plugin)).stopped)

    async def test_config_reset_all_is_one_shot_and_preserves_limits(self):
        self.freeze_clock()
        plugin = self.plugin()
        for user in ("123", "456"):
            await self.request(plugin, Event(user=user))
            record = plugin.manager.data["users"][f"tg_bot:{user}"]
            record.update(
                custom_limit=8, custom_5h_limit=3, notified_90=True, window_notified_50=True
            )
        plugin.manager.save()
        config = self.saved_config({"reset_all_usage": True, "whitelist": ["tg_bot:789"]})
        reset_plugin = self.plugin(config)
        for record in reset_plugin.manager.data["users"].values():
            self.assertEqual((record["used"], record["window_used"]), (0, 0))
            self.assertIsNone(record["window_started_at"])
            self.assertEqual((record["custom_limit"], record["custom_5h_limit"]), (8, 3))
            self.assertFalse(record["notified_90"])
            self.assertFalse(record["window_notified_50"])
        self.assertEqual(reset_plugin.config["whitelist"], ["tg_bot:789"])
        saved = json.loads((self.root / "plugin_config.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["reset_all_usage"])
        await self.request(reset_plugin)
        reloaded = self.plugin(self.saved_config(saved))
        self.assertEqual(reloaded.manager.data["users"]["tg_bot:123"]["used"], 1)

    async def test_config_reset_save_failure_keeps_usage_unchanged(self):
        plugin = self.plugin()
        await self.request(plugin)
        original = self.path.read_bytes()
        config = self.saved_config({"reset_all_usage": True}, fail=True)
        with self.assertRaisesRegex(PLUGIN.UsageStorageError, "未重置"):
            self.plugin(config)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(config["reset_all_usage"])

    async def test_config_reset_on_corrupt_storage_is_refused(self):
        self.path.write_text("broken", encoding="utf-8")
        config = self.saved_config({"reset_all_usage": True})
        with self.assertRaises(PLUGIN.UsageStorageError):
            self.plugin(config)
        self.assertTrue(config["reset_all_usage"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken")

    async def test_config_reset_data_write_failure_is_reported(self):
        plugin = self.plugin()
        await self.request(plugin)
        original = self.path.read_bytes()
        config = self.saved_config({"reset_all_usage": True})
        with patch.object(PLUGIN.os, "replace", side_effect=OSError("Data write failed")):
            with self.assertRaises(PLUGIN.UsageStorageError):
                self.plugin(config)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(config["reset_all_usage"])


if __name__ == "__main__":
    unittest.main()
