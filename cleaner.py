"""定时清理群友的核心逻辑：阈值解析、候选筛选、报告文本与 OneBot 调用。

本模块只依赖标准库（测试友好），真正的 CQHttp client 以鸭子类型传入。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


DAY_SECONDS = 86400


# ---------------------------------------------------------------------------
# 基础转换
# ---------------------------------------------------------------------------

def normalize_id(value: Any) -> str:
    """只保留数字；QQ 号/群号场景下顺手兼容 "群123" 这类输入。"""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits


def as_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        norm = normalize_id(item)
        if norm and norm not in out:
            out.append(norm)
    return out


def coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "开", "开启", "启用", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "关", "关闭", "禁用", "否"}:
        return False
    return default


def coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


# ---------------------------------------------------------------------------
# 阈值解析（含群单独配置）
# ---------------------------------------------------------------------------

def resolve_group_override(
    overrides: Any, group_id: str
) -> dict[str, Any] | None:
    """从 template_list 形态的群单独配置里找到本群的条目。"""
    if not isinstance(overrides, (list, tuple)):
        return None
    for item in overrides:
        if not isinstance(item, dict):
            continue
        if normalize_id(item.get("group_id")) == group_id:
            return item
    return None


def resolve_thresholds(
    cfg: dict[str, Any], group_id: str
) -> dict[str, Any]:
    """返回 {"enabled", "inactive_days", "under_level}。

    overrides 中 inactive_days/under_level 为 -1（或缺失）表示跟随全局。
    """
    result = {
        "enabled": True,
        "inactive_days": coerce_int(cfg.get("inactive_days"), 30),
        "under_level": coerce_int(cfg.get("under_level"), 10),
    }
    item = resolve_group_override(cfg.get("group_overrides"), group_id)
    if item is None:
        return result
    if not coerce_bool(item.get("enabled"), True):
        result["enabled"] = False
        return result
    if not coerce_bool(item.get("inherit_global"), True):
        days = item.get("inactive_days", -1)
        level = item.get("under_level", -1)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = -1
        try:
            level = int(level)
        except (TypeError, ValueError):
            level = -1
        if days >= 0:
            result["inactive_days"] = days
        if level >= 0:
            result["under_level"] = level
    return result


def effective_groups(
    cfg: dict[str, Any], discovered: list[str] | None = None
) -> list[str]:
    """计算本次允许扫描的群。默认只走白名单；可选全群+黑名单。"""
    whitelist = as_id_list(cfg.get("group_whitelist"))
    blacklist = set(as_id_list(cfg.get("group_blacklist")))
    if coerce_bool(cfg.get("run_in_all_groups"), False):
        pool = as_id_list(discovered or [])
    else:
        pool = whitelist
    return [gid for gid in pool if gid and gid not in blacklist]


# ---------------------------------------------------------------------------
# 候选筛选
# ---------------------------------------------------------------------------

def member_name(member: dict[str, Any]) -> str:
    for key in ("card", "nickname", "nick", "user_id"):
        value = member.get(key) if isinstance(member, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return "（无昵称）"


def _member_int(member: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        try:
            return int(member.get(key, default))
        except (TypeError, ValueError):
            continue
    return default


def select_candidates(
    members: list[dict[str, Any]],
    *,
    now_ts: float,
    inactive_days: int,
    under_level: int,
    bot_id: str = "",
    protected_ids: list[str] | None = None,
    protect_recent_join_days: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """筛选清理候选。

    规则与 QQ群管插件一致并加强保护：
    - 候选条件：最后发言早于阈值 且 等级低于上限
    - 永远保护：群主、管理员、Bot 自身
    - 额外保护：protected_ids 名单、新入群宽限期内成员

    返回 (候选列表按最后发言升序, 跳过原因统计)。
    """
    protected = {normalize_id(x) for x in (protected_ids or []) if normalize_id(x)}
    threshold_ts = now_ts - max(0, inactive_days) * DAY_SECONDS
    grace_ts = (
        now_ts - max(0, protect_recent_join_days) * DAY_SECONDS
        if protect_recent_join_days > 0
        else None
    )

    candidates: list[dict[str, Any]] = []
    stats = {
        "total": 0,
        "candidate": 0,
        "skip_role": 0,
        "skip_self": 0,
        "skip_protected": 0,
        "skip_recent_join": 0,
        "skip_active": 0,
    }

    for raw in members:
        if not isinstance(raw, dict):
            continue
        user_id = normalize_id(raw.get("user_id"))
        if not user_id:
            continue
        stats["total"] += 1
        role = str(raw.get("role", "") or "").lower()
        if role in {"owner", "admin"}:
            stats["skip_role"] += 1
            continue
        if bot_id and user_id == normalize_id(bot_id):
            stats["skip_self"] += 1
            continue
        if user_id in protected:
            stats["skip_protected"] += 1
            continue
        join_ts = _member_int(raw, "join_time", default=0)
        if grace_ts is not None and join_ts > 0 and join_ts >= grace_ts:
            stats["skip_recent_join"] += 1
            continue
        last_sent = _member_int(raw, "last_sent_time", default=0)
        level = _member_int(raw, "level", default=0)
        if last_sent < threshold_ts and level < under_level:
            candidates.append(
                {
                    "user_id": user_id,
                    "nickname": member_name(raw),
                    "level": level,
                    "role": role or "member",
                    "last_sent_time": last_sent,
                    "join_time": join_ts,
                }
            )
        else:
            stats["skip_active"] += 1

    candidates.sort(key=lambda item: (item["last_sent_time"], item["user_id"]))
    stats["candidate"] = len(candidates)
    return candidates, stats


def format_date(ts: float) -> str:
    if not ts or ts <= 0:
        return "未知"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知"


def format_candidate_line(index: int, item: dict[str, Any]) -> str:
    return (
        f"{index}) {item.get('nickname', '')}({item.get('user_id', '')}) "
        f"最后发言 {format_date(item.get('last_sent_time', 0))} "
        f"等级{item.get('level', 0)}"
    )


def reconcile_execution(
    snapshot: list[dict[str, Any]],
    fresh_members: list[dict[str, Any]],
    bot_id: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """执行时对账：快照即权威，不复核活跃度。

    只做两件安全检查：人已离群（跳过）、被提为群主/管理员或 Bot 自身（豁免）。
    返回 (待踢出 QQ 列表, 已离群 QQ 列表, 豁免 QQ 列表)，待踢出保持快照原顺序。
    """
    snap_ids: list[str] = []
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        uid = normalize_id(item.get("user_id"))
        if uid and uid not in snap_ids:
            snap_ids.append(uid)
    fresh_map = {
        normalize_id(member.get("user_id")): member
        for member in fresh_members
        if isinstance(member, dict) and normalize_id(member.get("user_id"))
    }
    me = normalize_id(bot_id)
    to_kick: list[str] = []
    gone: list[str] = []
    exempted: list[str] = []
    for uid in snap_ids:
        raw = fresh_map.get(uid)
        if raw is None:
            gone.append(uid)
            continue
        role = str(raw.get("role", "") or "").lower()
        if role in {"owner", "admin"} or (me and uid == me):
            exempted.append(uid)
            continue
        to_kick.append(uid)
    return to_kick, gone, exempted


def build_reminder_segments(
    *,
    group_id: str,
    inactive_days: int,
    under_level: int,
    candidates: list[dict[str, Any]],
    max_list: int,
    remind_hours: int,
    confirm_keyword: str,
    cancel_keyword: str,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """构造提醒消息的 OneBot 段列表：文本 + 每个被展示候选人的 At。

    只 @ 名单里实际列出来的人（受 max_list 截断）；提醒对象就是可踢的人，
    不打扰群主/管理员。
    """
    shown = [
        item for item in candidates if isinstance(item, dict)
    ][: max(1, max_list)]
    head = (
        f"【定时清理预告】群 {group_id}"
        + ("【演习模式，不会真正踢人】" if dry_run else "")
        + "\n"
        + f"规则：{inactive_days} 天未发言 且 等级 < {under_level}\n"
        + f"以下 {len(candidates)} 人将在约 {remind_hours} 小时后被移出本群"
        + "（以本次扫描为准，期间发言不影响结果）：\n"
    )
    segments: list[dict[str, Any]] = [{"type": "text", "data": {"text": head}}]
    for index, item in enumerate(shown, 1):
        uid = normalize_id(item.get("user_id"))
        segments.append({"type": "at", "data": {"qq": uid}})
        segments.append(
            {
                "type": "text",
                "data": {
                    "text": (
                        f" {index}) {item.get('nickname', '')}({uid}) "
                        f"最后发言 {format_date(item.get('last_sent_time', 0))} "
                        f"等级{item.get('level', 0)}\n"
                    )
                },
            }
        )
    tail = ""
    if len(candidates) > len(shown):
        tail += f"…等共 {len(candidates)} 人（仅列出前 {len(shown)} 人）\n"
    tail += (
        f"管理员可回复“{confirm_keyword}”立即执行，"
        f"或回复“{cancel_keyword}”中止。"
    )
    segments.append({"type": "text", "data": {"text": tail}})
    return segments


def build_preview_text(
    *,
    group_id: str,
    inactive_days: int,
    under_level: int,
    candidates: list[dict[str, Any]],
    stats: dict[str, int],
    max_list: int,
    dry_run: bool = False,
    preview_only: bool = False,
) -> str:
    head = "【定时清理预览】" if preview_only else "【定时清理预告】"
    if dry_run:
        head += "【演习模式，不会真正踢人】"
    lines = [
        f"{head} 群 {group_id}",
        f"规则：{inactive_days} 天未发言 且 等级 < {under_level}",
        f"扫描 {stats.get('total', 0)} 人，候选 {len(candidates)} 人 "
        f"（跳过：身份 {stats.get('skip_role', 0)}，自身 {stats.get('skip_self', 0)}，"
        f"保护名单 {stats.get('skip_protected', 0)}，新人群友 {stats.get('skip_recent_join', 0)}）",
    ]
    if not candidates:
        lines.append("结论：无符合条件的群友，本次无需处理。")
        return "\n".join(lines)
    show = candidates[: max(1, max_list)]
    lines.append("候选名单：")
    lines.extend(format_candidate_line(i + 1, item) for i, item in enumerate(show))
    if len(candidates) > len(show):
        lines.append(f"…等共 {len(candidates)} 人")
    return "\n".join(lines)


def build_execute_text(
    *,
    group_id: str,
    kicked: list[str],
    failed: list[str],
    capped: int = 0,
    dry_run: bool = False,
) -> str:
    verb = "演习踢出" if dry_run else "已踢出"
    lines = [
        f"【定时清理执行结果】群 {group_id}",
        f"{verb} {len(kicked)} 人，失败 {len(failed)} 人"
        + (f"，达到单轮上限剩余 {capped} 人留待下轮" if capped else ""),
    ]
    lines.extend(f"✅ {item}" for item in kicked)
    lines.extend(f"❌ {item}" for item in failed)
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "【定时群友清理】命令：",
            "/清理预览 [天数] [等级] - 扫描本群并预览候选（不踢人）",
            "/清理执行 [群号] - 立即执行当前待处理清理（默认当前群）",
            "/清理取消 [群号] - 中止当前待处理清理（默认当前群）",
            "/清理状态 - 查看定时配置、待处理任务和最近运行记录",
            "/定时清理 - 显示本帮助",
            "待处理任务可用“确认清理”立即执行、“取消清理”中止。",
        ]
    )


# ---------------------------------------------------------------------------
# OneBot 调用（鸭子类型 client）
# ---------------------------------------------------------------------------

async def call_action(client: Any, action: str, **params: Any) -> Any:
    """兼容直接方法与 call_action 两种写法。"""
    direct = getattr(client, action, None)
    if callable(direct):
        return await direct(**params)
    api = getattr(client, "api", None)
    nested = getattr(api, "call_action", None) if api is not None else None
    if callable(nested):
        return await nested(action, **params)
    generic = getattr(client, "call_action", None)
    if callable(generic):
        return await generic(action, **params)
    raise RuntimeError(f"当前消息平台不支持 OneBot 动作：{action}")


def extract_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def extract_object(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            return data
        return result
    return {}


async def discover_groups(client: Any) -> list[str]:
    """返回该 Bot 能看到的群号列表。"""
    try:
        result = await call_action(client, "get_group_list")
    except Exception:
        return []
    groups = [
        normalize_id(item.get("group_id"))
        for item in extract_list(result)
        if normalize_id(item.get("group_id"))
    ]
    return sorted(set(groups))


async def fetch_members(client: Any, group_id: str) -> list[dict[str, Any]]:
    """拉取群成员列表；失败抛错，由调用方决定是否跳过该群。"""
    try:
        return extract_list(
            await call_action(client, "get_group_member_list", group_id=int(group_id))
        )
    except Exception:
        return extract_list(
            await client.call_action("get_group_member_list", group_id=int(group_id))
        )


async def fetch_bot_id(client: Any) -> str:
    for getter in (
        lambda: client.get_login_info(),
        lambda: client.call_action("get_login_info"),
    ):
        try:
            info = extract_object(await getter())
        except Exception:
            continue
        bot_id = normalize_id(info.get("user_id"))
        if bot_id:
            return bot_id
    return ""


async def fetch_member_role(
    client: Any, group_id: str, user_id: str
) -> str:
    """返回 owner/admin/member/unknown/'' 之一。"""
    try:
        info = extract_object(
            await call_action(
                client,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        )
    except Exception:
        return ""
    return str(info.get("role", "") or "").lower()


async def send_group_text(client: Any, group_id: str, text: str) -> None:
    await call_action(client, "send_group_msg", group_id=int(group_id), message=text)


async def kick_member(
    client: Any, group_id: str, user_id: str, reject_add_request: bool
) -> None:
    await call_action(
        client,
        "set_group_kick",
        group_id=int(group_id),
        user_id=int(user_id),
        reject_add_request=bool(reject_add_request),
    )


def parse_daily_time(value: str) -> tuple[int, int] | None:
    text = str(value or "").strip().replace("：", ":")
    try:
        hour_str, minute_str = text.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (TypeError, ValueError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def next_daily_run(now_ts: float, time_str: str, tz_name: str) -> float | None:
    """计算下一次每日定时（今天已过则明天）。"""
    from zoneinfo import ZoneInfoNotFoundError

    parsed = parse_daily_time(time_str)
    if parsed is None:
        return None
    hour, minute = parsed
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        tz = datetime.now().astimezone().tzinfo
    now = datetime.fromtimestamp(now_ts, tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target.timestamp()
