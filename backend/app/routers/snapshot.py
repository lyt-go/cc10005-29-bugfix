"""采集快照接口：维护数据快照，覆盖生成快照、确认完成、标记过期等动作。

注意路由声明顺序：`/engines`、`/export` 必须排在 `/{entry_id}` 之前，
否则会被路径参数吞掉，导出和批次选择会表现为“点了没反应”。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.snapshot import SnapshotService

router = APIRouter(prefix="/api/snapshot", tags=["采集快照"])

service = SnapshotService()

LIST_FIELDS = ["快照编号", "关联任务", "快照时间", "采集行数", "去重后行数", "新增行数", "变更行数", "快照文件路径"]
STATUSES = ["草稿", "生成中", "已完成", "已过期"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已完成、已过期"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤采集快照列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/engines")
def list_engines() -> dict[str, Any]:
    """生成快照前可选的当前去重批次：只返回已启用的引擎版本。"""
    engines = service.engines.list_active()
    return {
        "module": "snapshot",
        "items": [
            {
                "引擎编号": row.get("引擎编号"),
                "去重字段组": row.get("去重字段组"),
                "哈希算法": row.get("哈希算法"),
                "版本": row.get("版本") or row.get("引擎编号"),
                "批次统计": row.get("批次统计"),
            }
            for row in engines
        ],
        "total": len(engines),
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集快照清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据快照明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据快照 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据快照，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据快照已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据快照执行生成快照、确认完成、标记过期；状态准入失败会说明原因，统计不变。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
