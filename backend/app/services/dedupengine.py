"""采集去重业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.lifecycle import LIFECYCLE_LOCK, now_iso, split_groups
from app.store import store

MODULE = "dedupengine"
REQUIRED_FIELDS = ["引擎编号", "去重字段组", "哈希算法"]
STATUS_ORDER = ["草稿", "引擎评估中", "已启用", "已停用"]
ACTION_RULES = {"提交引擎评估": "引擎评估中", "启用引擎": "已启用", "停用引擎": "已停用"}
NEGATIVE_ACTIONS = ["停用引擎"]
# 每个动作只允许从这些状态发起，状态只能向前推进：草稿 → 评估中 → 启用 → 停用。
# 停用说明：
#   * 评估中停用是终止评估，之后不能再启用，避免绕过评估直接上线；
#   * 已启用的引擎停用后若要重新上线，必须重新走「提交引擎评估」，
#     用同一条记录的新评估轮次启用，并换用新批次统计。
ACTION_SOURCES = {
    "提交引擎评估": {"草稿", "引擎评估中", "已停用"},
    "启用引擎": {"引擎评估中", "已启用"},
    "停用引擎": {"引擎评估中", "已启用", "已停用"},
}
# 停用后受影响的范围：新快照被阻断，已经生成的快照只读保留。
DISABLED_IMPACT = "新快照生成已阻断；已完成快照只读保留；重新生成需选择新的引擎版本"


class DedupengineService:
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
            rows = [row for row in rows if keyword in str(row.get("引擎编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_active(self) -> list[dict[str, Any]]:
        """当前可用于生成快照的引擎批次：只有已启用且未停用的引擎。"""
        return [row for row in store.rows(MODULE) if row.get("status") == "已启用"]

    def find_by_code(self, code: Any) -> dict[str, Any] | None:
        text = str(code or "").strip()
        if not text:
            return None
        for row in store.rows(MODULE):
            if str(row.get("引擎编号", "")) == text:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with LIFECYCLE_LOCK:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["版本"] = f"{entry['引擎编号']}-v1"
            entry["批次编号"] = None
            entry["批次统计"] = None
            entry["停用时间"] = None
            entry["影响范围"] = ""
            entry["受影响快照"] = []
            entry["状态历史"] = []
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重引擎 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集去重可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with LIFECYCLE_LOCK:
            current = str(entry.get("status") or "")
            if current == target:
                if target == "已停用":
                    return entry, (
                        f"去重引擎已处于停用状态，重复停用不会改变影响范围；"
                        f"受影响快照 {len(entry.get('受影响快照') or [])} 条"
                    )
                return entry, f"去重引擎已处于「{current}」，重复操作不会改变现有状态"
            if current not in ACTION_SOURCES.get(action, set()):
                return None, (
                    f"动作「{action}」不允许从「{current}」发起，"
                    f"只允许 {'、'.join(sorted(ACTION_SOURCES[action]))} 状态执行"
                )
            entry["status"] = target
            entry["pending"] = target not in ("已启用", "已停用")
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry.setdefault("状态历史", []).append(
                {"from": current, "to": target, "action": action, "at": now_iso()}
            )
            if target == "已停用":
                entry["停用时间"] = now_iso()
                # 停用同时结束当前启用窗口：晚到判断只看“最近一次停用之后”，
                # 避免旧的启用时间把停用期间的请求误判成新批次请求。
                entry["启用时间"] = None
                entry["影响范围"] = DISABLED_IMPACT
                impacted = self._mark_impacted_snapshots(entry)
                return entry, f"去重引擎已{action}，{DISABLED_IMPACT}；影响 {impacted} 条快照"
            if target == "已启用":
                # 重新启用会开启新的批次窗口，但保留上一次停用时间：
                # 快照生成时要用它拦截在停用期间发出的晚到请求。
                entry["启用时间"] = now_iso()
                entry["影响范围"] = ""
                entry["受影响快照"] = []
                self._clear_impact(entry)
            return entry, f"去重引擎已{action}"

    def _clear_impact(self, engine: dict[str, Any]) -> None:
        """引擎重新启用后，之前挂在快照上的停用影响提示不再成立。"""
        engine_code = str(engine.get("引擎编号") or "")
        for row in store.rows("snapshot"):
            if str(row.get("关联引擎") or "") != engine_code:
                continue
            row["affected"] = False
            row["影响说明"] = ""

    def _mark_impacted_snapshots(self, engine: dict[str, Any]) -> int:
        """停用时给引用该引擎批次的快照打上影响标记；已完成快照的统计原样保留。"""
        engine_code = str(engine.get("引擎编号") or "")
        groups = split_groups(engine.get("去重字段组"))
        affected: list[str] = []
        for row in store.rows("snapshot"):
            if str(row.get("关联引擎") or "") != engine_code:
                continue
            affected.append(str(row.get("快照编号")))
            row["affected"] = True
            row["受影响字段组"] = groups
            if row.get("status") in ("已完成", "已过期"):
                row["影响说明"] = f"去重引擎 {engine_code} 已停用，快照统计只读保留"
            elif row.get("status") == "生成中":
                row["影响说明"] = f"去重引擎 {engine_code} 已停用，生成中的批次不可确认，需重新生成"
            else:
                row["影响说明"] = f"去重引擎 {engine_code} 已停用，字段组不再可用，需重新选择引擎版本"
        engine["受影响快照"] = affected
        return len(affected)
