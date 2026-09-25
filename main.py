"""定时群友清理插件。

从 astrbot_plugin_qqadmin 的清理群友逻辑独立而来，增加：
定时扫描、群白名单/黑名单、Bot 管理员校验、清理前提醒、
管理员确认/取消、执行报告与状态持久化。

默认只扫描和提醒，不自动踢人需要同时满足：
enabled=true 且群在白名单里 且 auto_execute=true。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
    AiocqhttpMessageEvent,
)
from astrbot.core.star.star_tools import StarTools

from . import cleaner
from .state import CleanerState

PLUGIN_NAME = "astrbot_plugin_group_cleaner"
STATE_FILE = "state.json"
MAX_MESSAGE_CHARS = 3900
SWEEP_FALLBACK_SECONDS = 60


class GroupCleanerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.state = CleanerState(
            StarTools.get_data_dir(PLUGIN_NAME) / STATE_FILE
        )
        self._scheduler_task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._clients_cache: dict[str, tuple[Any, str]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self.state.load()
        if self.state.corrupted:
            logger.warning("[群友清理] 状态文件损坏，已重置为空状态")
        now = time.time()
        expired = self.state.expire_old_jobs(
            now, cleaner.coerce_int(self._get("state_retention_days"), 7)
        )
        for job in expired:
            self.state.add_run(
                {
                    "ts": now,
                    "group_id": job.get("group_id", ""),
                    "stage": "expired",
                    "trigger": job.get("trigger", "schedule"),
                    "note": "重启/加载时发现已过期，自动作废",
                }
            )
        try:
            self.state.save()
        except OSError as exc:
            logger.warning(f"[群友清理] 状态保存失败：{exc}")
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[群友清理] 定时群友清理插件已加载")

    async def terminate(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        try:
            self.state.save()
        except OSError as exc:
            logger.warning(f"[群友清理] 状态保存失败：{exc}")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _operator_min_role(self) -> str:
        text = cleaner.coerce_str(self._get("operator_min_role"), "管理员")
        return text if text in {"群主", "管理员"} else "管理员"

    # ------------------------------------------------------------------
    # Bot 与群发现
    # ------------------------------------------------------------------

    async def _bots(self) -> list[tuple[Any, str, str]]:
        """返回 [(client, bot_id, platform_id)]，兼容不同 CQHttp 写法。"""
        out: list[tuple[Any, str, str]] = []
        manager = getattr(self.context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None) or []
        for inst in insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            getter = getattr(inst, "get_client", None)
            if not callable(getter):
                continue
            try:
                client = getter()
            except Exception:
                continue
            if client is None:
                continue
            bot_id = await cleaner.fetch_bot_id(client)
            if not bot_id:
                continue
            try:
                meta = getattr(inst, "metadata", None)
                platform_id = str(getattr(meta, "id", "") or "")
            except Exception:
                platform_id = ""
            out.append((client, bot_id, platform_id))
        return out

    async def _send_segments(
        self,
        platform_id: str,
        group_id: str,
        segments: list[dict[str, Any]],
        client: Any = None,
    ) -> bool:
        """发送提醒消息：优先 AstrBot 原生链（At 由适配器编码），失败回退 CQ 码直发。"""
        if platform_id:
            try:
                from astrbot.api import message_components as Comp
                from astrbot.api.event import MessageChain

                components = []
                for seg in segments:
                    if seg.get("type") == "at":
                        components.append(Comp.At(qq=str(seg["data"]["qq"])))
                    else:
                        components.append(Comp.Plain(str(seg["data"].get("text", ""))))
                try:
                    chain = MessageChain(chain=components)
                except TypeError:
                    chain = MessageChain()
                    chain.chain.extend(components)
                if await self.context.send_message(
                    f"{platform_id}:GroupMessage:{group_id}", chain
                ):
                    return True
            except Exception as exc:
                logger.warning(f"[群友清理] 原生链发送失败，尝试 CQ 码直发：{exc}")
        if client is None:
            return False
        parts = []
        for seg in segments:
            if seg.get("type") == "at":
                parts.append(f"[CQ:at,qq={seg['data']['qq']}]")
            else:
                parts.append(str(seg["data"].get("text", "")))
        try:
            await cleaner.send_group_text(client, group_id, "".join(parts))
            return True
        except Exception as exc:
            logger.error(f"[群友清理] CQ 码直发失败 {group_id}：{exc}")
            return False

    @staticmethod
    def _job_key(bot_id: str, group_id: str) -> str:
        return f"{bot_id}:{group_id}"

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    async def _is_operator(
        self, client: Any, group_id: str, user_id: str, event: Any = None
    ) -> bool:
        if event is not None:
            try:
                if event.is_admin():
                    return True
            except Exception:
                pass
        role = await cleaner.fetch_member_role(client, group_id, user_id)
        if self._operator_min_role() == "群主":
            return role == "owner"
        return role in {"owner", "admin"}

    async def _bot_role(self, client: Any, group_id: str, bot_id: str) -> str:
        return await cleaner.fetch_member_role(client, group_id, bot_id)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @filter.command("定时清理")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_help(self, event: AiocqhttpMessageEvent):
        """定时清理"""
        yield event.plain_result(cleaner.help_text() + "\n" + self._status_text())

    @filter.command("清理状态")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_status(self, event: AiocqhttpMessageEvent):
        """清理状态"""
        yield event.plain_result(self._status_text())

    @filter.command("清理预览")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_preview(
        self,
        event: AiocqhttpMessageEvent,
        inactive_days: str | None = None,
        under_level: str | None = None,
    ):
        """清理预览 [天数] [等级]"""
        if event.get_group_id() in (None, "", "0"):
            yield event.plain_result("清理预览只能在群聊里使用")
            return
        client = event.bot
        group_id = cleaner.normalize_id(event.get_group_id())
        sender_id = cleaner.normalize_id(event.get_sender_id())
        if not await self._is_operator(client, group_id, sender_id, event):
            yield event.plain_result("只有群主/管理员（或 AstrBot 管理员）可以使用清理预览")
            return
        try:
            days = cleaner.coerce_int(inactive_days, -1) if inactive_days else -1
            level = cleaner.coerce_int(under_level, -1) if under_level else -1
            result = await self._preview_group(
                client, group_id, days_override=days, level_override=level
            )
        except Exception as exc:
            logger.error(f"[群友清理] 预览失败 {group_id}：{exc}")
            yield event.plain_result(f"预览失败：{exc}")
            return
        yield event.plain_result(result)

    @filter.command("清理取消")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_cancel(
        self, event: AiocqhttpMessageEvent, group_id: str | None = None
    ):
        """清理取消 [群号]"""
        target = cleaner.normalize_id(group_id) or cleaner.normalize_id(
            event.get_group_id()
        )
        if not target:
            yield event.plain_result("用法：/清理取消 [群号]，群内直接用 /清理取消")
            return
        client = event.bot
        sender_id = cleaner.normalize_id(event.get_sender_id())
        if not await self._is_operator(client, target, sender_id, event):
            yield event.plain_result("只有群主/管理员（或 AstrBot 管理员）可以取消清理")
            return
        found = self._find_job(cleaner.normalize_id(event.get_self_id()), target)
        if found is None:
            yield event.plain_result(f"群 {target} 当前没有待处理的清理任务")
            return
        _key, job = found
        job["stage"] = "cancelled"
        self._persist(job, "cancelled", trigger=job.get("trigger", "manual"))
        text = (
            f"【定时清理】群 {target} 的待处理清理已取消 "
            f"（原定 {len(job.get('candidates', []))} 人）"
        )
        await event.send(event.plain_result(text))
        event.stop_event()

    @filter.command("清理执行")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_execute(
        self, event: AiocqhttpMessageEvent, group_id: str | None = None
    ):
        """清理执行 [群号]"""
        target = cleaner.normalize_id(group_id) or cleaner.normalize_id(
            event.get_group_id()
        )
        if not target:
            yield event.plain_result("用法：/清理执行 [群号]，群内直接用 /清理执行")
            return
        client = event.bot
        sender_id = cleaner.normalize_id(event.get_sender_id())
        if not await self._is_operator(client, target, sender_id, event):
            yield event.plain_result("只有群主/管理员（或 AstrBot 管理员）可以执行清理")
            return
        found = self._find_job(cleaner.normalize_id(event.get_self_id()), target)
        if found is None:
            yield event.plain_result(f"群 {target} 当前没有待处理的清理任务，先用 /清理预览 生成")
            return
        _key, job = found
        resolved = await self._resolve_job_client(job)
        if resolved is None:
            yield event.plain_result(f"群 {target} 当前找不到可用的 Bot，无法执行")
            return
        client, bot_id, target = resolved
        await event.send(event.plain_result(f"【定时清理】开始执行群 {target} 的清理…"))
        result = await self._execute_job(client, bot_id, target, job, dry_run=False)
        await event.send(event.plain_result(result))
        event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def watch_confirm_keywords(self, event: AiocqhttpMessageEvent):
        """监听 确认清理 / 取消清理 关键词。"""
        try:
            text = str(getattr(event, "message_str", "") or "").strip()
        except Exception:
            return
        confirm = cleaner.coerce_str(self._get("confirm_keyword"), "确认清理")
        cancel = cleaner.coerce_str(self._get("cancel_keyword"), "取消清理")
        matched = ""
        if text in {confirm, cancel}:
            matched = text
        else:
            # 防抖合并后文本可能变成"上一句\n确认清理"
            for keyword in (confirm, cancel):
                if text.endswith("\n" + keyword):
                    matched = keyword
                    break
        if not matched:
            return
        if str(getattr(event, "get_self_id", lambda: "")() or "") == str(
            getattr(event, "get_sender_id", lambda: "")() or ""
        ):
            return
        group_id = cleaner.normalize_id(event.get_group_id())
        if not group_id:
            return
        found = self._find_job(cleaner.normalize_id(event.get_self_id()), group_id)
        if found is None:
            return
        _key, job = found
        if job.get("stage") not in {"reminded", "previewed"}:
            return
        client = event.bot
        sender_id = cleaner.normalize_id(event.get_sender_id())
        if not await self._is_operator(client, group_id, sender_id, event):
            return
        if matched == cancel:
            job["stage"] = "cancelled"
            self._persist(job, "cancelled", trigger=job.get("trigger", "schedule"))
            await event.send(
                event.plain_result(
                    f"【定时清理】群 {group_id} 的待处理清理已取消 "
                    f"（原定 {len(job.get('candidates', []))} 人）"
                )
            )
            event.stop_event()
            return
        await event.send(event.plain_result(f"【定时清理】收到确认，开始执行群 {group_id} 的清理…"))
        job_bot = str(job.get("bot_id") or "") or cleaner.normalize_id(
            event.get_self_id()
        )
        result = await self._execute_job(client, job_bot, group_id, job, dry_run=False)
        await event.send(event.plain_result(result))
        event.stop_event()

    # ------------------------------------------------------------------
    # 定时器
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"[群友清理] 定时循环出错：{exc}")
                await asyncio.sleep(
                    max(30, cleaner.coerce_int(self._get("sweep_interval_seconds"), 60))
                )
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        if not cleaner.coerce_bool(self._get("enabled"), True):
            return
        now = time.time()
        expired = self.state.expire_old_jobs(
            now, cleaner.coerce_int(self._get("state_retention_days"), 7)
        )
        for job in expired:
            logger.info(
                f"[群友清理] 任务过期作废：{job.get('bot_id')}:{job.get('group_id')}"
            )
            self.state.add_run(
                {
                    "ts": now,
                    "group_id": job.get("group_id", ""),
                    "stage": "expired",
                    "trigger": job.get("trigger", "schedule"),
                }
            )
        if expired:
            self._save_state_quietly()
        async with self._run_lock:
            await self._process_due_executions(now)
            await self._maybe_scheduled_scan(now)

    async def _process_due_executions(self, now: float) -> None:
        for key, job in list(self.state.data.get("jobs", {}).items()):
            if not isinstance(job, dict) or job.get("stage") != "reminded":
                continue
            if job.get("trigger") != "schedule":
                continue
            execute_at = job.get("execute_at")
            if not isinstance(execute_at, (int, float)) or now < float(execute_at):
                continue
            if not cleaner.coerce_bool(self._get("auto_execute"), True):
                continue
            client, bot_id, group_id = await self._resolve_job_client(job)
            if client is None:
                logger.warning(f"[群友清理] 执行时找不到 Bot，任务保留：{key}")
                continue
            job["stage"] = "executing"
            self._save_state_quietly()
            text = await self._execute_job(
                client, bot_id, group_id, job, dry_run=False
            )
            try:
                await cleaner.send_group_text(client, group_id, text)
            except Exception as exc:
                logger.error(f"[群友清理] 执行报告发送失败 {group_id}：{exc}")

    async def _maybe_scheduled_scan(self, now: float) -> None:
        next_run = self.state.data.get("next_run_at")
        if next_run is None:
            next_run = self._compute_initial_next_run(now)
            self.state.data["next_run_at"] = next_run
            self._save_state_quietly()
        if not isinstance(next_run, (int, float)) or now < float(next_run):
            return
        await self._scheduled_scan(now)
        self.state.data["next_run_at"] = self._compute_next_after(now)
        self._save_state_quietly()

    def _compute_initial_next_run(self, now: float) -> float:
        if cleaner.coerce_bool(self._get("run_on_startup"), False):
            return float(now)
        return self._compute_next_after(now)

    def _compute_next_after(self, now: float) -> float:
        mode = cleaner.coerce_str(self._get("schedule_mode"), "interval")
        if mode == "daily":
            nxt = cleaner.next_daily_run(
                now,
                cleaner.coerce_str(self._get("daily_time"), "04:00"),
                cleaner.coerce_str(self._get("timezone"), "Asia/Shanghai"),
            )
            if nxt is not None:
                return float(nxt)
            logger.warning("[群友清理] 每日时间格式非法，已回退为间隔模式")
        interval = max(1800, cleaner.coerce_int(self._get("interval_seconds"), 604800))
        return float(now) + interval

    # ------------------------------------------------------------------
    # 扫描与执行
    # ------------------------------------------------------------------

    async def _scheduled_scan(self, now: float) -> None:
        bots = await self._bots()
        if not bots:
            logger.warning("[群友清理] 没有可用的 aiocqhttp Bot，跳过本轮扫描")
            return
        discovered: list[str] = []
        for _, client, _pid in bots:
            try:
                groups = await cleaner.discover_groups(client)
            except Exception as exc:
                logger.warning(f"[群友清理] 获取群列表失败：{exc}")
                continue
            discovered.extend(groups)
        targets = cleaner.effective_groups(
            {
                "group_whitelist": self._get("group_whitelist", []),
                "group_blacklist": self._get("group_blacklist", []),
                "run_in_all_groups": self._get("run_in_all_groups", False),
            },
            discovered,
        )
        if not targets:
            logger.debug("[群友清理] 本轮没有可扫描的群（白名单为空或全被排除）")
            self.state.add_run(
                {"ts": now, "stage": "idle", "trigger": "schedule", "note": "无目标群"}
            )
            self._save_state_quietly()
            return
        stagger = max(0, cleaner.coerce_int(self._get("stagger_groups_seconds"), 30))
        first = True
        for group_id in sorted(set(targets)):
            if not first and stagger > 0:
                await asyncio.sleep(stagger)
            first = False
            try:
                await self._scan_group_for_schedule(group_id, bots, now)
            except Exception as exc:
                logger.error(f"[群友清理] 群 {group_id} 扫描失败：{exc}")
                self.state.add_run(
                    {
                        "ts": time.time(),
                        "group_id": group_id,
                        "stage": "scan_failed",
                        "trigger": "schedule",
                        "note": str(exc)[:200],
                    }
                )
        self._save_state_quietly()

    async def _client_for_group(
        self, bots: list[tuple[Any, str, str]], group_id: str
    ) -> tuple[Any, str, str] | None:
        for client, bot_id, platform_id in bots:
            try:
                groups = await cleaner.discover_groups(client)
            except Exception:
                continue
            if group_id in groups:
                return client, bot_id, platform_id
        return None

    async def _resolve_job_client(
        self, job: dict[str, Any]
    ) -> tuple[Any, str, str] | None:
        bots = await self._bots()
        group_id = str(job.get("group_id", ""))
        bot_id = str(job.get("bot_id", ""))
        for client, candidate_bot, _pid in bots:
            if bot_id and candidate_bot != bot_id:
                continue
            try:
                groups = await cleaner.discover_groups(client)
            except Exception:
                continue
            if group_id in groups:
                return client, candidate_bot, group_id
        return None

    async def _scan_group_for_schedule(
        self,
        group_id: str,
        bots: list[tuple[Any, str, str]],
        now: float,
    ) -> None:
        resolved = await self._client_for_group(bots, group_id)
        if resolved is None:
            logger.warning(f"[群友清理] 找不到群 {group_id} 的 Bot，跳过本轮")
            self.state.add_run(
                {
                    "ts": now,
                    "group_id": group_id,
                    "stage": "skipped",
                    "trigger": "schedule",
                    "note": "找不到该群的 Bot",
                }
            )
            return
        client, bot_id, platform_id = resolved
        thresholds = cleaner.resolve_thresholds(
            {
                "inactive_days": self._get("inactive_days", 30),
                "under_level": self._get("under_level", 10),
                "group_overrides": self._get("group_overrides", []),
            },
            group_id,
        )
        if not thresholds["enabled"]:
            self.state.add_run(
                {
                    "ts": now,
                    "group_id": group_id,
                    "stage": "skipped",
                    "trigger": "schedule",
                    "note": "该群单独配置为关闭",
                }
            )
            return
        if cleaner.coerce_bool(self._get("require_bot_admin"), True):
            role = await cleaner.fetch_member_role(client, group_id, bot_id)
            if role not in {"owner", "admin"}:
                logger.info(
                    f"[群友清理] Bot 在群 {group_id} 不是管理员（{role}），跳过"
                )
                self.state.add_run(
                    {
                        "ts": now,
                        "group_id": group_id,
                        "stage": "skipped",
                        "trigger": "schedule",
                        "note": f"Bot 无管理权限（{role or '未知'}）",
                    }
                )
                return
        try:
            members = await cleaner.fetch_members(client, group_id)
        except Exception as exc:
            raise RuntimeError(f"拉取群成员失败：{exc}") from exc
        candidates, stats = cleaner.select_candidates(
            members,
            now_ts=now,
            inactive_days=thresholds["inactive_days"],
            under_level=thresholds["under_level"],
            bot_id=bot_id,
            protected_ids=cleaner.as_id_list(self._get("protected_user_ids", [])),
            protect_recent_join_days=cleaner.coerce_int(
                self._get("protect_recent_join_days"), 7
            ),
        )
        self.state.add_run(
            {
                "ts": now,
                "group_id": group_id,
                "stage": "scanned",
                "trigger": "schedule",
                "candidate_count": len(candidates),
            }
        )
        if not candidates:
            return
        remind_on = cleaner.coerce_bool(self._get("remind_enabled"), True)
        remind_hours = max(0, cleaner.coerce_int(self._get("remind_before_hours"), 24))
        key = self._job_key(bot_id, group_id)
        job = self.state.get_job(key) or {}
        job.update(
            {
                "group_id": group_id,
                "bot_id": bot_id,
                "platform_id": platform_id,
                "stage": "reminded",
                "trigger": "schedule",
                "params": {
                    "inactive_days": thresholds["inactive_days"],
                    "under_level": thresholds["under_level"],
                },
                "candidates": candidates,
                "created_at": now,
                "execute_at": now + remind_hours * 3600 if remind_on and remind_hours > 0 else now,
                "expires_at": now
                + max(remind_hours * 3600 if remind_on else 0, 3600)
                + cleaner.coerce_int(self._get("state_retention_days"), 7) * 86400,
            }
        )
        self.state.put_job(key, job)
        max_list = max(10, cleaner.coerce_int(self._get("max_list_members"), 60))
        dry_run = cleaner.coerce_bool(self._get("dry_run"), False)
        confirm = cleaner.coerce_str(self._get("confirm_keyword"), "确认清理")
        cancel = cleaner.coerce_str(self._get("cancel_keyword"), "取消清理")
        if remind_on and remind_hours > 0:
            segments = cleaner.build_reminder_segments(
                group_id=group_id,
                inactive_days=thresholds["inactive_days"],
                under_level=thresholds["under_level"],
                candidates=candidates,
                max_list=max_list,
                remind_hours=remind_hours,
                confirm_keyword=confirm,
                cancel_keyword=cancel,
                dry_run=dry_run,
            )
            try:
                sent = await self._send_segments(
                    platform_id, group_id, segments, client
                )
            except Exception as exc:
                sent = False
                logger.error(f"[群友清理] 预告发送异常 {group_id}：{exc}")
            if not sent:
                job["stage"] = "failed"
                self.state.put_job(key, job)
                self.state.add_run(
                    {
                        "ts": time.time(),
                        "group_id": group_id,
                        "stage": "failed",
                        "trigger": "schedule",
                        "note": "预告发送失败，已中止本次任务",
                    }
                )
                self._save_state_quietly()
                return
            self.state.add_run(
                {
                    "ts": time.time(),
                    "group_id": group_id,
                    "stage": "reminded",
                    "trigger": "schedule",
                    "candidate_count": len(candidates),
                }
            )
            self._save_state_quietly()
            return
        # 提醒关闭（或提前量为 0）：扫描到就直接执行，只发执行报告
        async with self._run_lock:
            text = await self._execute_job(client, bot_id, group_id, job, dry_run)
        try:
            await cleaner.send_group_text(client, group_id, self._clip(text))
        except Exception as exc:
            logger.error(f"[群友清理] 执行报告发送失败 {group_id}：{exc}")

    async def _preview_group(
        self,
        client: Any,
        group_id: str,
        days_override: int = -1,
        level_override: int = -1,
    ) -> str:
        if cleaner.normalize_id(group_id) in cleaner.as_id_list(
            self._get("group_blacklist", [])
        ):
            return f"群 {group_id} 在黑名单中，不执行清理相关操作"
        thresholds = cleaner.resolve_thresholds(
            {
                "inactive_days": self._get("inactive_days", 30),
                "under_level": self._get("under_level", 10),
                "group_overrides": self._get("group_overrides", []),
            },
            group_id,
        )
        if days_override >= 0:
            thresholds["inactive_days"] = days_override
        if level_override >= 0:
            thresholds["under_level"] = level_override
        try:
            login_id = await cleaner.fetch_bot_id(client)
        except Exception:
            login_id = ""
        members = await cleaner.fetch_members(client, group_id)
        candidates, stats = cleaner.select_candidates(
            members,
            now_ts=time.time(),
            inactive_days=thresholds["inactive_days"],
            under_level=thresholds["under_level"],
            bot_id=login_id,
            protected_ids=cleaner.as_id_list(self._get("protected_user_ids", [])),
            protect_recent_join_days=cleaner.coerce_int(
                self._get("protect_recent_join_days"), 7
            ),
        )
        dry_run = cleaner.coerce_bool(self._get("dry_run"), False)
        text = cleaner.build_preview_text(
            group_id=group_id,
            inactive_days=thresholds["inactive_days"],
            under_level=thresholds["under_level"],
            candidates=candidates,
            stats=stats,
            max_list=max(10, cleaner.coerce_int(self._get("max_list_members"), 60)),
            dry_run=dry_run,
            preview_only=True,
        )
        if candidates:
            confirm = cleaner.coerce_str(self._get("confirm_keyword"), "确认清理")
            cancel = cleaner.coerce_str(self._get("cancel_keyword"), "取消清理")
            minutes = max(1, cleaner.coerce_int(self._get("manual_confirm_minutes"), 10))
            key = self._job_key(login_id or "manual", group_id)
            self.state.put_job(
                key,
                {
                    "group_id": group_id,
                    "bot_id": login_id or "manual",
                    "stage": "previewed",
                    "trigger": "manual",
                    "params": {
                        "inactive_days": thresholds["inactive_days"],
                        "under_level": thresholds["under_level"],
                    },
                    "candidates": candidates,
                    "created_at": time.time(),
                    "execute_at": None,
                    "expires_at": time.time() + minutes * 60,
                },
            )
            self._save_state_quietly()
            text += (
                f"\n已生成待处理任务：{minutes} 分钟内回复“{confirm}”执行，"
                f"回复“{cancel}”放弃；超时自动作废。"
            )
        return self._clip(text)

    async def _execute_job(
        self,
        client: Any,
        bot_id: str,
        group_id: str,
        job: dict[str, Any],
        dry_run: bool | None = None,
    ) -> str:
        """按快照执行：提醒名单即最终名单，等待期间发言不改变结果。

        只做两项安全检查：群是否被新加入黑名单/单独关闭、本群 Bot 是否仍有管理权限。
        """
        cfg_dry = cleaner.coerce_bool(self._get("dry_run"), False)
        dry_run = cfg_dry if dry_run is None else (dry_run or cfg_dry)
        if cleaner.normalize_id(group_id) in cleaner.as_id_list(
            self._get("group_blacklist", [])
        ):
            job["stage"] = "cancelled"
            self._persist(job, "cancelled", trigger=job.get("trigger", "schedule"))
            return f"【定时清理】群 {group_id} 已被加入黑名单，本次清理自动中止"
        thresholds = cleaner.resolve_thresholds(
            {
                "inactive_days": self._get("inactive_days", 30),
                "under_level": self._get("under_level", 10),
                "group_overrides": self._get("group_overrides", []),
            },
            group_id,
        )
        if not thresholds["enabled"]:
            job["stage"] = "cancelled"
            self._persist(job, "cancelled", trigger=job.get("trigger", "schedule"))
            return f"【定时清理】群 {group_id} 已被单独关闭，本次清理自动中止"
        if cleaner.coerce_bool(self._get("require_bot_admin"), True):
            role = await cleaner.fetch_member_role(client, group_id, bot_id)
            if role not in {"owner", "admin"}:
                job["stage"] = "failed"
                self._persist(job, "failed", trigger=job.get("trigger", "schedule"))
                return f"【定时清理】群 {group_id} 执行中止：Bot 当前不是管理员（{role or '未知'}）"
        members = await cleaner.fetch_members(client, group_id)
        to_kick, gone, exempted = cleaner.reconcile_execution(
            job.get("candidates") or [], members, bot_id=bot_id
        )
        cap = max(1, cleaner.coerce_int(self._get("max_kick_per_run"), 50))
        batch, capped = to_kick[:cap], max(0, len(to_kick) - cap)
        kicked: list[str] = []
        failed: list[str] = []
        delay = max(0, cleaner.coerce_int(self._get("kick_delay_seconds"), 2))
        reject = cleaner.coerce_bool(self._get("reject_add_request"), False)
        first = True
        name_of = {}
        for item in job.get("candidates") or []:
            if isinstance(item, dict):
                name_of[cleaner.normalize_id(item.get("user_id"))] = item.get(
                    "nickname", item.get("user_id")
                )
        for uid in batch:
            if not first and delay > 0:
                await asyncio.sleep(delay)
            first = False
            label = f"{name_of.get(uid, uid)}({uid})"
            if dry_run:
                kicked.append(label + "（演习）")
                continue
            try:
                await cleaner.kick_member(client, group_id, uid, reject)
                kicked.append(label)
            except Exception as exc:
                logger.error(f"[群友清理] 踢出失败 {group_id}/{uid}：{exc}")
                failed.append(f"{label}：{str(exc)[:80]}")
        job["stage"] = "done"
        job["result"] = {
            "kicked": kicked,
            "failed": failed,
            "exempted": exempted,
            "gone": gone,
            "capped": capped,
            "dry_run": dry_run,
        }
        self._persist(job, "done", trigger=job.get("trigger", "schedule"))
        text = cleaner.build_execute_text(
            group_id=group_id,
            kicked=kicked,
            failed=failed,
            capped=capped,
            dry_run=dry_run,
        )
        if exempted:
            text += f"\n（另有 {len(exempted)} 人已升为管理或离群豁免）"
        if gone:
            text += f"\n（另有 {len(gone)} 人已不在群内，无需处理）"
        return self._clip(text)

    # ------------------------------------------------------------------
    # 状态与工具
    # ------------------------------------------------------------------

    def _persist(self, job: dict[str, Any], stage: str, trigger: str) -> None:
        key = self._job_key(
            str(job.get("bot_id", "")), str(job.get("group_id", ""))
        )
        self.state.put_job(key, job)
        self.state.add_run(
            {
                "ts": time.time(),
                "group_id": job.get("group_id", ""),
                "stage": stage,
                "trigger": trigger,
                "candidate_count": len(job.get("candidates", [])),
                "result": job.get("result"),
            }
        )
        self._save_state_quietly()

    def _find_jobs_for_group(
        self, group_id: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """按群号找待处理任务（跨 Bot 兜底），返回 [(key, job)]。"""
        out = []
        for key, job in self.state.data.get("jobs", {}).items():
            if isinstance(job, dict) and str(job.get("group_id", "")) == group_id:
                out.append((key, job))
        return out

    def _find_job(
        self, bot_id: str, group_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        """优先同 Bot，找不到则取该群任意待处理任务。"""
        exact = self._job_key(bot_id, group_id)
        job = self.state.get_job(exact)
        if job is not None:
            return exact, job
        others = self._find_jobs_for_group(group_id)
        return others[0] if others else None

    def _save_state_quietly(self) -> None:
        try:
            self.state.save()
        except OSError as exc:
            logger.warning(f"[群友清理] 状态保存失败：{exc}")

    @staticmethod
    def _clip(text: str) -> str:
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
        return text[:MAX_MESSAGE_CHARS] + "\n…（内容过长已截断）"

    def _status_text(self) -> str:
        cfg_lines = [
            "【定时群友清理·状态】",
            f"定时总开关：{'开' if cleaner.coerce_bool(self._get('enabled'), True) else '关'}",
            f"模式：{cleaner.coerce_str(self._get('schedule_mode'), 'interval')}"
            f"（间隔 {cleaner.coerce_int(self._get('interval_seconds'), 604800)}s / 每日 {cleaner.coerce_str(self._get('daily_time'), '04:00')} {cleaner.coerce_str(self._get('timezone'), 'Asia/Shanghai')}）",
            f"白名单群：{', '.join(cleaner.as_id_list(self._get('group_whitelist', []))) or '（空）'}",
            f"黑名单群：{', '.join(cleaner.as_id_list(self._get('group_blacklist', []))) or '（空）'}",
            f"全群模式：{'开（高风险）' if cleaner.coerce_bool(self._get('run_in_all_groups'), False) else '关'}",
            f"规则：{cleaner.coerce_int(self._get('inactive_days'), 30)} 天未发言 且 等级 < {cleaner.coerce_int(self._get('under_level'), 10)}",
            f"提醒：{'开' if cleaner.coerce_bool(self._get('remind_enabled'), True) else '关'}"
            f"（{cleaner.coerce_int(self._get('remind_before_hours'), 24)} 小时，快照执行）"
            f"；到期自动执行：{'开' if cleaner.coerce_bool(self._get('auto_execute'), True) else '关'}",
        ]
        nxt = self.state.data.get("next_run_at")
        cfg_lines.append(
            "下次扫描："
            + (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nxt))
                if isinstance(nxt, (int, float))
                else "未安排"
            )
        )
        jobs = [
            job
            for job in self.state.data.get("jobs", {}).values()
            if isinstance(job, dict)
        ]
        if jobs:
            cfg_lines.append("待处理任务：")
            for job in jobs[:10]:
                execute_at = job.get("execute_at")
                when = (
                    time.strftime("%m-%d %H:%M", time.localtime(execute_at))
                    if isinstance(execute_at, (int, float))
                    else "需手动执行"
                )
                cfg_lines.append(
                    f"- 群 {job.get('group_id')}（{job.get('trigger')}，{job.get('stage')}）："
                    f"{len(job.get('candidates', []))} 人，执行时间 {when}"
                )
            if len(jobs) > 10:
                cfg_lines.append(f"…等共 {len(jobs)} 个")
        else:
            cfg_lines.append("待处理任务：无")
        runs = [r for r in self.state.data.get("runs", []) if isinstance(r, dict)][-5:]
        if runs:
            cfg_lines.append("最近运行：")
            for item in reversed(runs):
                ts = item.get("ts", 0)
                when = (
                    time.strftime("%m-%d %H:%M", time.localtime(ts))
                    if isinstance(ts, (int, float))
                    else "?"
                )
                cfg_lines.append(
                    f"- {when} 群 {item.get('group_id', '-')} {item.get('stage', '?')} "
                    f"({item.get('trigger', '?')})"
                )
        return self._clip("\n".join(cfg_lines))
