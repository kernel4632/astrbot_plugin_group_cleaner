"""调度/执行路径的回归测试。

重点回归：line 810 曾经在""提醒关闭 → 立即执行""分支里再次 `async with
self._run_lock`，而调用链 _tick -> _maybe_scheduled_scan -> _scheduled_scan
已经持有同一把锁。asyncio.Lock 不可重入，因此调度任务会永久死锁、静默停摆，
直到进程重启。本测试在"调用方已持锁"的前提下执行，必须在超时内完成。
"""

import asyncio
import time

from astrbot_plugin_group_cleaner import cleaner
from astrbot_plugin_group_cleaner.main import GroupCleanerPlugin
from astrbot_plugin_group_cleaner.state import CleanerState

GROUP = "1053824168"
BOT = "3083452120"


def _make_plugin(state_path, **overrides):
    cfg = {
        "enabled": True,
        "inactive_days": 30,
        "under_level": 10,
        "group_overrides": [],
        "require_bot_admin": False,
        "remind_enabled": False,
        "remind_before_hours": 1,
        "state_retention_days": 7,
        "max_list_members": 60,
        "dry_run": False,
        "confirm_keyword": "确认清理",
        "cancel_keyword": "取消清理",
        "auto_execute": True,
        "stagger_groups_seconds": 0,
    }
    cfg.update(overrides)
    plugin = object.__new__(GroupCleanerPlugin)
    plugin.context = None
    plugin.config = cfg
    plugin.state = CleanerState(state_path)
    plugin.state.load()
    plugin._scheduler_task = None
    plugin._run_lock = asyncio.Lock()
    plugin._clients_cache = {}
    plugin._stopping = False
    return plugin


def test_immediate_execute_does_not_deadlock(tmp_path, monkeypatch):
    """提醒关闭 + 有候选人时，在已持锁的情况下必须能跑完（不死锁）。"""
    plugin = _make_plugin(tmp_path / "state.json")
    seen = {}

    async def fake_client_for_group(bots, group_id):
        return ("client", BOT, "AKer")

    async def fake_fetch_members(client, group_id):
        return []

    def fake_select_candidates(*args, **kwargs):
        return ([{"user_id": "1001", "nickname": "潜水员", "level": 1}], {"total": 1})

    async def fake_execute_job(client, bot_id, group_id, job, dry_run):
        seen["dry_run"] = dry_run
        return "已执行"

    async def fake_send_group_text(client, group_id, text):
        seen["sent"] = text

    monkeypatch.setattr(plugin, "_client_for_group", fake_client_for_group)
    monkeypatch.setattr(cleaner, "fetch_members", fake_fetch_members)
    monkeypatch.setattr(cleaner, "select_candidates", fake_select_candidates)
    monkeypatch.setattr(cleaner, "send_group_text", fake_send_group_text)
    monkeypatch.setattr(plugin, "_execute_job", fake_execute_job)

    async def scenario():
        # 模拟 _tick 已经持有 _run_lock 的调用栈
        async with plugin._run_lock:
            await plugin._scan_group_for_schedule(
                GROUP, [(None, BOT, "AKer")], time.time()
            )

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert seen.get("sent") == "已执行"
    assert seen.get("dry_run") is False
    stages = [run.get("stage") for run in plugin.state.data.get("runs", [])]
    assert "scanned" in stages
    assert any(key for key in plugin.state.data.get("jobs", {}))


def test_scheduler_loop_survives_unexpected_cancel(monkeypatch):
    """调度循环被意外取消后应自愈继续，而不是静默退出。"""
    plugin = object.__new__(GroupCleanerPlugin)
    plugin.context = None
    plugin.config = {"sweep_interval_seconds": 30, "enabled": True}
    plugin.state = CleanerState("/tmp/_gc_unused_state.json")
    plugin.state.load()
    plugin._scheduler_task = None
    plugin._run_lock = asyncio.Lock()
    plugin._clients_cache = {}
    plugin._stopping = False

    calls = {"n": 0}

    async def fake_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次被"意外取消"
            raise asyncio.CancelledError()
        # 第二次正常执行后让循环自然退出
        plugin._stopping = True

    monkeypatch.setattr(plugin, "_tick", fake_tick)

    real_sleep = asyncio.sleep

    async def fast_sleep(*args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    asyncio.run(asyncio.wait_for(plugin._scheduler_loop(), timeout=5))

    # 第一次的取消没有让循环退出，第二次 tick 仍被执行
    assert calls["n"] >= 2
