"""定时群友清理插件的 JSON 状态持久化。

只依赖标准库，方便单元测试。保存内容：
- next_run_at: 下一次定时扫描时间戳（秒），None 表示未安排
- jobs: 每个群最多一个待处理清理任务，key 为 "<bot_id>:<group_id>"
- runs: 最近的运行记录（扫描/提醒/执行/取消/过期），最多保留 20 条
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


STATE_VERSION = 1
MAX_RUN_HISTORY = 20


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "next_run_at": None,
        "jobs": {},
        "runs": [],
    }


class CleanerState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = default_state()
        self.corrupted: bool = False

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.data = default_state()
            return
        except OSError:
            self.data = default_state()
            self.corrupted = True
            return
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            self.data = default_state()
            self.corrupted = True
            return
        if not isinstance(parsed, dict):
            self.data = default_state()
            self.corrupted = True
            return
        merged = default_state()
        merged.update(parsed)
        if not isinstance(merged.get("jobs"), dict):
            merged["jobs"] = {}
        if not isinstance(merged.get("runs"), list):
            merged["runs"] = []
        self.data = merged
        self.corrupted = False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get_job(self, key: str) -> dict[str, Any] | None:
        job = self.data["jobs"].get(key)
        return dict(job) if isinstance(job, dict) else None

    def put_job(self, key: str, job: dict[str, Any]) -> None:
        self.data["jobs"][key] = dict(job)

    def remove_job(self, key: str) -> None:
        self.data["jobs"].pop(key, None)

    def expire_old_jobs(self, now_ts: float, retention_days: int) -> list[dict[str, Any]]:
        """清理执行时间已过 retention 很久的任务，返回被标记过期的任务。

        手动任务（execute_at 为空）按 expires_at 判定。
        """
        expired: list[dict[str, Any]] = []
        keep_seconds = max(0, int(retention_days)) * 86400
        for key in list(self.data["jobs"].keys()):
            job = self.data["jobs"].get(key)
            if not isinstance(job, dict):
                del self.data["jobs"][key]
                continue
            due = job.get("expires_at", job.get("execute_at"))
            if not isinstance(due, (int, float)):
                continue
            if now_ts - float(due) > keep_seconds:
                job["stage"] = "expired"
                expired.append(dict(job))
                del self.data["jobs"][key]
        return expired

    def add_run(self, record: dict[str, Any]) -> None:
        runs = self.data["runs"]
        runs.append(dict(record))
        while len(runs) > MAX_RUN_HISTORY:
            runs.pop(0)
