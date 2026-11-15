"""采集快照业务规则：状态流转、字段校验、批次一致性与筛选口径都收在这里。

生命周期单向推进：草稿 → 生成中 → 已完成 → 已过期；只有已过期允许在原地
重新生成（旧完成统计先归档到历史批次统计），已完成保持只读。

* 生成快照：草稿可以发起，已过期可以重新生成；生成前必须确定当前去重批次
  （已启用的引擎），并把该批次的字段组和统计一起固化，之后不再随引擎变化。
* 确认完成：只有生成中可以确认，统计只写一次；重复确认返回同一个完成版本。
* 标记过期：生成中可以取消，已完成可以归档；两种情况下已有统计都原样保留，
  与并发生成的冲突由先完成的请求决定，后到的请求不改写统计。
* 已完成的快照只读；需要新批次时用「重新生成快照」按当前引擎版本新建草稿，
  原快照的统计、字段组、状态历史都不改动。
"""
from __future__ import annotations

import copy
from typing import Any

from app.lifecycle import (
    LIFECYCLE_LOCK,
    now_iso,
    parse_time,
    request_in_disabled_window,
    same_groups,
    split_groups,
)
from app.services.dedupengine import DedupengineService
from app.store import store

MODULE = "snapshot"
REQUIRED_FIELDS = ["快照编号", "关联任务", "快照时间"]
STATUS_ORDER = ["草稿", "生成中", "已完成", "已过期"]

ACTION_SOURCES = {
    "生成快照": {"草稿", "已过期"},
    "确认完成": {"生成中"},
    "标记过期": {"生成中", "已完成"},
    "重新生成快照": {"已完成", "已过期"},
}

SNAPSHOT_STAT_FIELDS = ["采集行数", "去重后行数", "新增行数", "变更行数"]
BATCH_COUNT_FIELDS = ["采集行数", "去重后行数", "变更行数"]


class SnapshotService:
    def __init__(self, engines: DedupengineService | None = None) -> None:
        self.engines = engines or DedupengineService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with LIFECYCLE_LOCK:
            engine_code = values.get("引擎编号") or values.get("关联引擎")
            engine = self.engines.find_by_code(engine_code) if engine_code else None
            entry = self._new_draft(values)
            if engine is not None and engine.get("status") == "已启用":
                entry["关联引擎"] = engine.get("引擎编号")
                entry["去重字段组"] = engine.get("去重字段组")
            store.rows(MODULE).append(entry)
            return entry, []

    # ------------------------------------------------------------------ 动作

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据快照 {entry_id} 不存在或已归档"
        if action not in ACTION_SOURCES:
            return None, f"动作「{action}」不属于采集快照可执行范围"
        with LIFECYCLE_LOCK:
            if action == "生成快照":
                return self._generate(entry, values)
            if action == "确认完成":
                return self._confirm(entry, values)
            if action == "标记过期":
                return self._expire(entry)
            return self._regenerate(entry, values)

    def _generate(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status") or "")
        if current not in ACTION_SOURCES["生成快照"]:
            if current == "生成中":
                return None, f"快照 {entry.get('快照编号')} 正在生成中，等待当前版本完成或过期后再操作"
            return None, (
                f"快照 {entry.get('快照编号')} 已是「{current}」终态并只读保留，不能原地重新生成；"
                "如需新批次请使用「重新生成快照」选择当前引擎版本"
            )

        request_time = str(values.get("请求时间") or now_iso())
        code = values.get("引擎编号") or values.get("关联引擎") or values.get("引擎批次") or entry.get("关联引擎")

        # 1. 停用拦截：生成前先比较引擎停用时间和请求时间，晚到请求留在草稿。
        engine = self.engines.find_by_code(code) if code else None
        if code and engine is None:
            message = f"指定的去重引擎 {code} 不存在，请选择当前已启用的引擎版本"
            self._leave_draft(entry, message, request_time, groups=split_groups(entry.get("去重字段组")))
            return None, message
        if engine is not None and request_in_disabled_window(
            request_time, engine.get("停用时间"), engine.get("启用时间")
        ):
            message = (
                f"去重引擎 {engine.get('引擎编号')} 已于 {engine.get('停用时间')} 停用，"
                "本次生成请求晚于停用时间，快照留在草稿；旧字段组不再可用，请重新选择引擎版本"
            )
            self._leave_draft(entry, message, request_time, engine=engine)
            return None, message
        if engine is not None and engine.get("status") == "已停用":
            message = (
                f"去重引擎 {engine.get('引擎编号')} 已停用，不能生成新快照；"
                "旧去重字段组不可沿用，请选择当前已启用的引擎版本"
            )
            self._leave_draft(entry, message, request_time, engine=engine)
            return None, message
        if engine is None:
            engine, message = self._resolve_engine(entry, values)
            if engine is None:
                self._leave_draft(
                    entry,
                    message,
                    request_time,
                    groups=split_groups(entry.get("去重字段组")),
                )
                return None, message
        elif engine.get("status") != "已启用":
            message = (
                f"去重引擎 {engine.get('引擎编号')} 当前为「{engine.get('status')}」，"
                "只有已启用引擎可以生成快照"
            )
            self._leave_draft(entry, message, request_time, engine=engine)
            return None, message

        # 2. 生成前核对字段组：不接受旧引擎的字段组，只认当前批次。
        requested_groups = split_groups(values.get("去重字段组"))
        engine_groups = split_groups(engine.get("去重字段组"))
        if requested_groups and not same_groups(requested_groups, engine_groups):
            message = (
                f"请求的去重字段组与引擎 {engine.get('引擎编号')} 当前批次不一致，"
                "快照留在草稿，请按当前批次重新确认字段组"
            )
            self._leave_draft(entry, message, request_time, engine=engine, groups=requested_groups)
            return None, message

        # 3. 同一关联任务只允许一个正在生成的版本，并发生成保留先进入者。
        for other in store.rows(MODULE):
            if other is entry or other.get("status") != "生成中":
                continue
            if str(other.get("关联任务")) != str(entry.get("关联任务")):
                continue
            return None, (
                f"关联任务「{entry.get('关联任务')}」已有快照 {other.get('快照编号')} 正在生成，"
                "本次未生成，避免出现两个完成版本"
            )

        # 4. 固化和引擎同一批次的统计，作为确认时的核对基准。
        stats, stats_message = self._batch_stats(engine, values)
        if stats is None:
            self._leave_draft(entry, stats_message, request_time, engine=engine)
            return None, stats_message

        # 5. 已过期快照原地重新生成：先把上一版完成统计归档，再开始新批次，
        #    历史统计不会被新的新增行数覆盖。
        if current == "已过期" and isinstance(entry.get("批次统计"), dict):
            history = entry.setdefault("历史批次统计", [])
            history.append(
                {
                    "批次": entry.get("关联引擎"),
                    "字段组": entry.get("去重字段组"),
                    "统计": copy.deepcopy(entry["批次统计"]),
                    "确认时间": entry.get("确认时间"),
                    "过期时间": entry.get("过期时间"),
                }
            )
            entry["批次统计"] = None
            entry["基线快照"] = None
            entry["确认令牌"] = None
            for field in SNAPSHOT_STAT_FIELDS:
                entry[field] = None
            entry["预估统计"] = None

        self._append_history(entry, current, "生成中", "生成快照", f"批次 {engine.get('引擎编号')}")
        entry["status"] = "生成中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["关联引擎"] = engine.get("引擎编号")
        entry["引擎版本"] = engine.get("版本") or engine.get("引擎编号")
        entry["去重字段组"] = "、".join(engine_groups)
        entry["预估统计"] = copy.deepcopy(stats)
        entry["生成令牌"] = f"{entry.get('快照编号')}@{now_iso()}"
        entry["生成请求时间"] = request_time
        entry["受影响字段组"] = []
        entry["affected"] = False
        entry["影响说明"] = ""
        return entry, (
            f"数据快照已进入生成中，使用引擎 {engine.get('引擎编号')} 的当前去重批次："
            f"{entry['去重字段组']}"
        )

    def _confirm(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status") or "")
        if current == "已完成":
            return entry, f"快照 {entry.get('快照编号')} 已完成，重复确认不会覆盖现有统计"
        if current != "生成中":
            return None, f"快照 {entry.get('快照编号')} 当前为「{current}」，只有生成中可以确认完成"

        engine = self.engines.find_by_code(entry.get("关联引擎"))
        if engine is None:
            return None, f"快照 {entry.get('快照编号')} 关联的去重引擎不存在，无法完成确认"
        if engine.get("status") != "已启用":
            self._mark_affected(entry, engine, "生成中被引擎停用，快照保持生成中且统计未写入")
            return None, (
                f"去重引擎 {engine.get('引擎编号')} 已停用，快照保持生成中，统计未写入；"
                "需基于新引擎版本重新生成后再确认"
            )

        # 并发确认：统计只写一次，持有确认令牌的完成版本不可被覆盖。
        if entry.get("确认令牌"):
            return entry, f"快照 {entry.get('快照编号')} 已完成确认，本次未重复写入统计"
        token = str(values.get("生成令牌") or "")
        if token and token != str(entry.get("生成令牌")):
            return None, "提交的生成令牌与当前生成版本不一致，统计未写入，请刷新后重试"

        # 确认前重新取同一批次的统计，必须和生成时固化的基准一致。
        stats, stats_message = self._batch_stats(engine, values)
        if stats is None:
            return None, stats_message
        frozen = entry.get("预估统计")
        if isinstance(frozen, dict) and not self._same_batch(frozen, stats):
            self._mark_affected(entry, engine, "生成前后去重批次统计不一致，统计未写入")
            return None, "生成前后去重批次发生变化，统计未写入，请重新生成快照"

        baseline = self._increment_baseline(entry)
        increment = self._increment_stats(baseline, stats)
        final = {
            **stats,
            **increment,
            "批次": engine.get("引擎编号"),
            "字段组": split_groups(engine.get("去重字段组")),
        }
        if not self._stats_valid(final):
            return None, "批次统计不自洽（采集行数应不小于去重后行数、新增不应超过去重后行数），统计未写入"

        for field in SNAPSHOT_STAT_FIELDS:
            entry[field] = str(final[field])
        entry["批次统计"] = copy.deepcopy(final)
        entry["基线快照"] = (baseline or {}).get("快照编号")
        entry["确认令牌"] = f"{entry.get('快照编号')}@{now_iso()}"
        entry["确认时间"] = now_iso()
        entry["预估统计"] = None
        entry["受影响字段组"] = []
        entry["affected"] = False
        entry["影响说明"] = ""
        self._append_history(entry, current, "已完成", "确认完成", f"批次 {engine.get('引擎编号')}")
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False

        # 去重计数回写：只按已固化的完成版本统计，和快照写入同一批次同一数字。
        engine["已完成快照数"] = self._count_completed(engine.get("引擎编号"))
        engine["最近完成快照"] = entry.get("快照编号")
        engine["最近完成批次统计"] = {
            "快照编号": entry.get("快照编号"),
            "采集行数": final["采集行数"],
            "去重后行数": final["去重后行数"],
            "变更行数": final["变更行数"],
        }
        entry["计数回写"] = {
            "引擎编号": engine.get("引擎编号"),
            "已完成快照数": engine["已完成快照数"],
            "批次统计": copy.deepcopy(engine["最近完成批次统计"]),
        }
        return entry, (
            f"数据快照已完成，批次 {final['批次']}：采集 {final['采集行数']} 行、"
            f"去重后 {final['去重后行数']} 行、新增 {final['新增行数']} 行、"
            f"变更 {final['变更行数']} 行"
        )

    def _expire(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status") or "")
        if current == "已过期":
            return entry, f"快照 {entry.get('快照编号')} 已过期，历史统计保持不变"
        if current not in ACTION_SOURCES["标记过期"]:
            return None, f"快照 {entry.get('快照编号')} 当前为「{current}」，只有生成中或已完成可以标记过期"
        has_stats = isinstance(entry.get("批次统计"), dict)
        # 过期只改变生命周期状态：已完成版本的统计原样冻结，不回写、不清零。
        entry["过期时间"] = now_iso()
        self._append_history(
            entry,
            current,
            "已过期",
            "标记过期",
            "完成版本统计只读保留" if has_stats else "生成中取消，统计未写入",
        )
        entry["status"] = "已过期"
        entry["pending"] = False
        entry["abnormal"] = False
        if has_stats:
            return entry, (
                f"数据快照已标记过期，完成版本统计只读保留：采集 {entry.get('采集行数')} 行、"
                f"去重后 {entry.get('去重后行数')} 行"
            )
        return entry, f"数据快照已标记过期，生成中的版本统计未写入，可重新生成"

    def _regenerate(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """重新生成不改动原快照，只按新引擎版本新建一条草稿记录。"""
        current = str(entry.get("status") or "")
        if current not in ACTION_SOURCES["重新生成快照"]:
            return None, f"快照 {entry.get('快照编号')} 当前为「{current}」，只有已完成或已过期可以重新生成"

        request_time = str(values.get("请求时间") or now_iso())
        code = values.get("引擎编号") or values.get("关联引擎") or values.get("引擎批次")
        engine = self.engines.find_by_code(code) if code else None
        if code and engine is None:
            return None, f"指定的去重引擎 {code} 不存在，请选择当前已启用的引擎版本"
        if engine is not None and request_in_disabled_window(
            request_time, engine.get("停用时间"), engine.get("启用时间")
        ):
            return None, (
                f"去重引擎 {engine.get('引擎编号')} 已于 {engine.get('停用时间')} 停用，"
                "晚到的重新生成请求未创建新快照"
            )
        if engine is None:
            engine, message = self._resolve_engine(entry, values, allow_draft_engine=False)
            if engine is None:
                return None, message
        elif engine.get("status") != "已启用":
            return None, (
                f"去重引擎 {engine.get('引擎编号')} 当前为「{engine.get('status')}」，"
                "重新生成必须选择当前已启用的引擎版本"
            )

        groups = split_groups(values.get("去重字段组"))
        if groups and not same_groups(groups, engine.get("去重字段组")):
            return None, "选择的去重字段组与目标引擎版本不一致，未创建新快照"

        rows = store.rows(MODULE)
        code_new = self._next_code(rows)
        new_entry = self._new_draft(
            {
                "快照编号": code_new,
                "关联任务": entry.get("关联任务"),
                "快照时间": values.get("快照时间") or now_iso()[:10],
                "关联引擎": engine.get("引擎编号"),
                "去重字段组": "、".join(split_groups(engine.get("去重字段组"))),
            }
        )
        new_entry["重生成自"] = entry.get("快照编号")
        new_entry["重生成自状态"] = current
        new_entry["引擎版本"] = engine.get("版本") or engine.get("引擎编号")
        rows.append(new_entry)
        return new_entry, (
            f"已基于引擎 {engine.get('引擎编号')} 新建草稿 {code_new}，"
            f"原快照 {entry.get('快照编号')} 的「{current}」统计保持不变"
        )

    # ------------------------------------------------------------------ 内部

    def _resolve_engine(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        *,
        allow_draft_engine: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        """确定当前去重批次：只接受已启用引擎，已停用引擎直接拦截。"""
        code = values.get("引擎编号") or values.get("关联引擎") or values.get("引擎批次")
        if not code and allow_draft_engine:
            code = entry.get("关联引擎")
        if code:
            engine = self.engines.find_by_code(code)
            if engine is None:
                return None, f"指定的去重引擎 {code} 不存在，请选择当前已启用的引擎版本"
            status = engine.get("status")
            if status == "已停用":
                return None, (
                    f"去重引擎 {engine.get('引擎编号')} 已停用，不能生成新快照；"
                    "请选择当前已启用的引擎版本"
                )
            if status != "已启用":
                return None, f"去重引擎 {engine.get('引擎编号')} 当前为「{status}」，只有已启用引擎可以生成快照"
            return engine, ""
        actives = self.engines.list_active()
        if len(actives) == 1:
            return actives[0], ""
        if not actives:
            return None, "当前没有已启用的去重引擎，无法生成快照；请先启用引擎确定去重批次"
        codes = "、".join(str(row.get("引擎编号")) for row in actives)
        return None, f"存在多个已启用去重引擎（{codes}），生成前必须明确选择当前去重批次"

    def _leave_draft(
        self,
        entry: dict[str, Any],
        reason: str,
        request_time: str,
        *,
        engine: dict[str, Any] | None = None,
        groups: list[str] | None = None,
    ) -> None:
        """准入失败时快照留在原状态（草稿或已过期），只记录受影响字段组，不改动已有统计。"""
        current = str(entry.get("status") or "草稿")
        # 已过期是终态，准入失败时不能回退成草稿，否则生命周期就变成双向了。
        if current not in ("草稿", "已过期"):
            current = "草稿"
        entry["status"] = current
        entry["affected"] = True
        if current == "草稿":
            entry["pending"] = True
        entry["abnormal"] = True
        entry["受影响字段组"] = groups if groups is not None else split_groups((engine or {}).get("去重字段组"))
        entry["影响说明"] = reason
        entry["最近生成尝试"] = request_time

    def _mark_affected(self, entry: dict[str, Any], engine: dict[str, Any], reason: str) -> None:
        entry["affected"] = True
        entry["受影响字段组"] = split_groups(engine.get("去重字段组"))
        entry["影响说明"] = reason

    def _batch_stats(
        self,
        engine: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """统计只能来自引擎当前去重批次；请求带来的数字必须与批次一致。"""
        batch = engine.get("批次统计")
        if not isinstance(batch, dict):
            return None, f"去重引擎 {engine.get('引擎编号')} 尚未产生当前批次统计，不能生成快照"
        stats: dict[str, Any] = {}
        for field in BATCH_COUNT_FIELDS:
            number = self._as_int(batch.get(field))
            if number is None:
                return None, f"去重引擎 {engine.get('引擎编号')} 的批次统计缺少「{field}」"
            stats[field] = number

        provided = values.get("批次统计")
        if isinstance(provided, dict):
            for field in BATCH_COUNT_FIELDS:
                if field not in provided:
                    continue
                number = self._as_int(provided.get(field))
                if number is None or number != stats[field]:
                    return None, (
                        f"请求携带的「{field}」与引擎 {engine.get('引擎编号')} 当前批次不一致，"
                        "统计未写入"
                    )
        for field in BATCH_COUNT_FIELDS:
            if field in values and self._as_int(values.get(field)) != stats[field]:
                return None, (
                    f"请求携带的「{field}」与引擎 {engine.get('引擎编号')} 当前批次不一致，"
                    "统计未写入"
                )
        if stats["采集行数"] < stats["去重后行数"]:
            return None, f"去重引擎 {engine.get('引擎编号')} 的批次统计不自洽：采集行数小于去重后行数"
        stats["批次"] = engine.get("引擎编号")
        stats["字段组"] = split_groups(engine.get("去重字段组"))
        return stats, ""

    def _increment_baseline(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """同一关联任务、同一去重批次（引擎 + 字段组）的上一份完成版本，作为增量基线。"""
        baseline: dict[str, Any] | None = None
        baseline_time = None
        for row in store.rows(MODULE):
            if row is entry or row.get("status") != "已完成":
                continue
            if not isinstance(row.get("批次统计"), dict):
                continue
            if str(row.get("关联任务")) != str(entry.get("关联任务")):
                continue
            if str(row.get("关联引擎") or "") != str(entry.get("关联引擎") or ""):
                continue
            if not same_groups(row.get("去重字段组"), entry.get("去重字段组")):
                continue
            current_time = parse_time(row.get("确认时间") or row.get("快照时间"))
            if baseline is None or (current_time and (baseline_time is None or current_time > baseline_time)):
                baseline = row
                baseline_time = current_time
        return baseline

    def _increment_stats(
        self,
        baseline: dict[str, Any] | None,
        stats: dict[str, Any],
    ) -> dict[str, int]:
        if baseline is None:
            # 没有同批次基线时，去重后的行全部计入新增。
            return {"新增行数": stats["去重后行数"], "变更行数": stats["变更行数"]}
        previous = baseline.get("批次统计") or {}
        before = self._as_int(previous.get("去重后行数"))
        if before is None:
            before = self._as_int(baseline.get("去重后行数")) or 0
        return {
            "新增行数": max(stats["去重后行数"] - before, 0),
            "变更行数": stats["变更行数"],
        }

    def _count_completed(self, engine_code: Any) -> int:
        """按完成版本计数：当前完成统计 + 已归档的历史批次，过期不抹掉历史。"""
        code = str(engine_code or "")
        versions = 0
        for row in store.rows(MODULE):
            if str(row.get("关联引擎") or "") != code:
                continue
            if isinstance(row.get("批次统计"), dict):
                versions += 1
            history = row.get("历史批次统计")
            if isinstance(history, list):
                versions += len(history)
        return versions

    @staticmethod
    def _stats_valid(stats: dict[str, Any]) -> bool:
        collected = stats.get("采集行数")
        deduped = stats.get("去重后行数")
        added = stats.get("新增行数")
        changed = stats.get("变更行数")
        if None in (collected, deduped, added, changed):
            return False
        if min(collected, deduped, added, changed) < 0:
            return False
        return collected >= deduped >= added >= 0 and changed <= deduped

    @staticmethod
    def _same_batch(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for field in [*BATCH_COUNT_FIELDS, "批次"]:
            if left.get(field) != right.get(field):
                return False
        return same_groups(left.get("字段组"), right.get("字段组"))

    def _new_draft(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = "草稿"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["关联引擎"] = values.get("关联引擎")
        entry["引擎版本"] = None
        entry["去重字段组"] = values.get("去重字段组")
        for field in SNAPSHOT_STAT_FIELDS:
            entry[field] = None
        entry["批次统计"] = None
        entry["受影响字段组"] = []
        entry["affected"] = False
        entry["影响说明"] = ""
        entry["状态历史"] = []
        return entry

    @staticmethod
    def _next_code(rows: list[dict[str, Any]]) -> str:
        serial = 0
        for row in rows:
            text = str(row.get("快照编号") or "")
            digits = "".join(char for char in text if char.isdigit())
            if digits:
                serial = max(serial, int(digits))
        return f"SNAP-{serial + 1:04d}"

    @staticmethod
    def _append_history(entry: dict[str, Any], source: str, target: str, action: str, reason: str) -> None:
        entry.setdefault("状态历史", []).append(
            {"from": source, "to": target, "action": action, "at": now_iso(), "reason": reason}
        )

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
