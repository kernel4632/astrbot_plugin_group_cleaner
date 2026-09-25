import time

from astrbot_plugin_group_cleaner import cleaner
from astrbot_plugin_group_cleaner.state import CleanerState


def _members():
    now = time.time()
    day = 86400
    return [
        {"user_id": "1001", "nickname": "潜水A", "role": "member", "level": 5,
         "last_sent_time": now - 40 * day, "join_time": now - 200 * day},
        {"user_id": "1002", "nickname": "活跃B", "role": "member", "level": 5,
         "last_sent_time": now - 1 * day, "join_time": now - 200 * day},
        {"user_id": "1003", "nickname": "高等级", "role": "member", "level": 80,
         "last_sent_time": now - 100 * day, "join_time": now - 400 * day},
        {"user_id": "1004", "nickname": "群主", "role": "owner", "level": 1,
         "last_sent_time": 0, "join_time": now - 400 * day},
        {"user_id": "1005", "nickname": "管理", "role": "admin", "level": 1,
         "last_sent_time": 0, "join_time": now - 400 * day},
        {"user_id": "1006", "nickname": "新人", "role": "member", "level": 1,
         "last_sent_time": 0, "join_time": now - 1 * day},
    ]


def test_select_candidates_respects_rules():
    members = _members()
    now = time.time()
    cands, stats = cleaner.select_candidates(
        members,
        now_ts=now,
        inactive_days=30,
        under_level=10,
        bot_id="3083452120",
        protected_ids=["1007"],
        protect_recent_join_days=7,
    )
    ids = [c["user_id"] for c in cands]
    assert ids == ["1001"], ids
    assert stats["total"] == 6
    assert stats["skip_role"] == 2
    assert stats["skip_recent_join"] == 1
    assert stats["skip_active"] == 2


def test_self_and_protected_never_selected():
    members = _members()
    members.append({"user_id": "3083452120", "nickname": "bot", "role": "member",
                    "level": 1, "last_sent_time": 0, "join_time": time.time() - 400 * 86400})
    members.append({"user_id": "1007", "nickname": "白名单", "role": "member",
                    "level": 1, "last_sent_time": 0, "join_time": time.time() - 400 * 86400})
    cands, _ = cleaner.select_candidates(
        members, now_ts=time.time(), inactive_days=30, under_level=10,
        bot_id="3083452120", protected_ids=["1007"], protect_recent_join_days=7,
    )
    ids = [c["user_id"] for c in cands]
    assert "3083452120" not in ids
    assert "1007" not in ids


def test_resolve_thresholds_override():
    cfg = {"inactive_days": 30, "under_level": 10, "group_overrides": [
        {"group_id": "123", "enabled": True, "inherit_global": False,
         "inactive_days": 7, "under_level": 20},
        {"group_id": "456", "enabled": False, "inherit_global": True,
         "inactive_days": -1, "under_level": -1},
    ]}
    assert cleaner.resolve_thresholds(cfg, "123") == {
        "enabled": True, "inactive_days": 7, "under_level": 20}
    assert cleaner.resolve_thresholds(cfg, "456")["enabled"] is False
    assert cleaner.resolve_thresholds(cfg, "789") == {
        "enabled": True, "inactive_days": 30, "under_level": 10}


def test_effective_groups():
    cfg = {"group_whitelist": ["111", "222"], "group_blacklist": ["222"],
           "run_in_all_groups": False}
    assert cleaner.effective_groups(cfg, ["111", "222", "333"]) == ["111"]
    cfg2 = {"group_whitelist": [], "group_blacklist": ["222"], "run_in_all_groups": True}
    assert cleaner.effective_groups(cfg2, ["111", "222"]) == ["111"]


def test_next_daily_run():
    # 固定时间点：2026-01-02 03:00 +08:00，下一次 04:00 应为当天 04:00
    import datetime

    from zoneinfo import ZoneInfo
    now = datetime.datetime(2026, 1, 2, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    nxt = cleaner.next_daily_run(now, "04:00", "Asia/Shanghai")
    assert nxt is not None
    got = datetime.datetime.fromtimestamp(nxt, ZoneInfo("Asia/Shanghai"))
    assert (got.hour, got.minute, got.day) == (4, 0, 2)
    # 已过时间 → 明天
    now2 = datetime.datetime(2026, 1, 2, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    nxt2 = cleaner.next_daily_run(now2, "04:00", "Asia/Shanghai")
    got2 = datetime.datetime.fromtimestamp(nxt2, ZoneInfo("Asia/Shanghai"))
    assert (got2.hour, got2.minute, got2.day) == (4, 0, 3)
    assert cleaner.next_daily_run(now, "99:99", "Asia/Shanghai") is None


def test_state_roundtrip(tmp_path):
    state = CleanerState(tmp_path / "state.json")
    state.load()
    assert state.data["jobs"] == {}
    state.put_job("1:2", {"stage": "reminded"})
    state.save()
    other = CleanerState(tmp_path / "state.json")
    other.load()
    assert other.get_job("1:2") == {"stage": "reminded"}


def _cand(uid, nickname="昵称"):
    return {"user_id": uid, "nickname": nickname, "level": 1,
            "last_sent_time": 0, "join_time": 0}


def test_reconcile_snapshot_is_authoritative():
    members = [
        {"user_id": "1001", "role": "member"},   # 快照里有，群里有 → 踢
        {"user_id": "1003", "role": "admin"},    # 快照里有但已升管理 → 豁免
        {"user_id": "1004", "role": "member"},
    ]
    to_kick, gone, exempted = cleaner.reconcile_execution(
        [_cand("1001"), _cand("1002"), _cand("1003")], members, bot_id="999"
    )
    assert to_kick == ["1001"]
    assert gone == ["1002"]
    assert exempted == ["1003"]


def test_reconcile_bot_self_exempted():
    members = [{"user_id": "999", "role": "member"}]
    to_kick, gone, exempted = cleaner.reconcile_execution(
        [_cand("999")], members, bot_id="999"
    )
    assert to_kick == []
    assert exempted == ["999"]


def test_build_reminder_segments_ats_only_kickable():
    cands = [_cand("1001", "张三"), _cand("1002", "李四")]
    segs = cleaner.build_reminder_segments(
        group_id="123", inactive_days=30, under_level=10,
        candidates=cands, max_list=60, remind_hours=24,
        confirm_keyword="确认清理", cancel_keyword="取消清理",
    )
    ats = [s["data"]["qq"] for s in segs if s.get("type") == "at"]
    assert ats == ["1001", "1002"]
    joined = "".join(
        s["data"].get("text", "") for s in segs if s.get("type") == "text"
    )
    assert "期间发言不影响结果" in joined
    assert "确认清理" in joined and "取消清理" in joined
