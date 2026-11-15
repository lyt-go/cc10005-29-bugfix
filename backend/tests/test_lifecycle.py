"""去重批次与快照生命周期验证。

直接调用 service 层（和路由使用同一实例），覆盖题目要求的 DEDU-0002、
DEDU-0003、SNAP-0002、SNAP-0003 场景：停用拦截、状态回退、并发生成、
晚到请求、增量统计与去重计数回写的一致性。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import store  # noqa: E402
from app.services.dedupengine import DedupengineService  # noqa: E402
from app.services.snapshot import SnapshotService  # noqa: E402


def fresh() -> tuple[DedupengineService, SnapshotService]:
    store.reset()
    return DedupengineService(), SnapshotService()


def check(condition: bool, label: str) -> None:
    assert condition, label


def test_disabled_engine_blocks_snapshot() -> None:
    engines, snapshots = fresh()
    engine, message = engines.run_action(2, "停用引擎")
    check(engine is not None, f"停用 DEDU-0002 成功：{message}")
    check(engine["status"] == "已停用", "引擎状态推进为已停用")
    check(bool(engine["停用时间"]), "记录停用时间用于晚到请求比较")
    check("SNAP-0002" in engine["受影响快照"], "停用后标记受影响快照范围")

    snap, message = snapshots.run_action(2, "生成快照", {"引擎编号": "DEDU-0002"})
    check(snap is None, f"生成快照被拦截：{message}")
    entry = snapshots.get_entry(2)
    check(entry["status"] == "草稿", "被拦截的快照留在草稿，不会借用旧引擎进入生成中")
    check(entry["去重字段组"] == "url,发布时间", "旧字段组只作为受影响范围展示")
    check(entry["受影响字段组"] == ["url", "发布时间"], "受影响字段组已标出")
    check(entry["采集行数"] is None and entry["批次统计"] is None, "被拦截时不会写入统计")

    snap, message = snapshots.run_action(2, "确认完成", {})
    check(snap is None, f"草稿快照不能确认完成：{message}")
    check(snapshots.get_entry(2)["批次统计"] is None, "确认被拦截后仍未写入统计")


def test_late_request_stays_draft() -> None:
    engines, snapshots = fresh()
    engines.run_action(2, "停用引擎")
    disabled_at = engines.get_entry(2)["停用时间"]

    snap, message = snapshots.run_action(
        1,
        "生成快照",
        {"引擎编号": "DEDU-0002", "请求时间": "2099-01-01T00:00:00+00:00"},
    )
    check(snap is None, f"晚到请求被拒绝：{message}")
    draft = snapshots.get_entry(1)
    check(draft["status"] == "草稿", "晚到请求留在草稿")
    check(draft["受影响字段组"] == ["url", "发布时间"], "草稿显示受影响字段组")
    check("停用" in draft["影响说明"], "草稿给出停用说明")

    # 停用之前的请求时间不能“复活”已停用引擎：仍按当前状态拦截，但不会被当成晚到请求。
    snap, message = snapshots.run_action(
        1,
        "生成快照",
        {"引擎编号": "DEDU-0002", "请求时间": disabled_at[:-1] + "0+00:00"},
    )
    check(snap is None, f"请求时间早于停用时间仍按引擎当前状态拦截：{message}")
    check("晚于停用时间" not in message, "早于停用时间的请求不按晚到处理")
    check(snapshots.get_entry(1)["status"] == "草稿", "拦截后保持草稿")


def test_status_never_rolls_back() -> None:
    engines, snapshots = fresh()
    before = dict(snapshots.get_entry(3)["批次统计"])
    snap, message = snapshots.run_action(3, "生成快照", {})
    check(snap is None, f"已完成快照原地生成被拒绝：{message}")
    entry = snapshots.get_entry(3)
    check(entry["status"] == "已完成", "已完成状态未被回退")
    check(entry["批次统计"] == before, "只读快照的统计未被改写")

    # 已完成可以标记过期，统计冻结。
    snap, message = snapshots.run_action(3, "标记过期", {})
    check(snap is not None, f"已完成标记过期：{message}")
    expired = snapshots.get_entry(3)
    check(expired["status"] == "已过期", "状态单向推进到已过期")
    check(expired["批次统计"] == before, "过期后历史统计原样保留")
    check(
        [item["to"] for item in expired["状态历史"]] == ["生成中", "已完成", "已过期"],
        "状态历史按单向顺序累积",
    )

    # 已过期不能走「生成快照」以外的老动作，且过期→生成中要基于新引擎版本。
    for action in ("确认完成",):
        snap, message = snapshots.run_action(3, action, {})
        check(snap is None, f"过期后 {action} 被拒绝：{message}")


def test_expired_regeneration_keeps_history() -> None:
    engines, snapshots = fresh()
    snapshots.run_action(3, "标记过期", {})
    old_stats = dict(snapshots.get_entry(3)["批次统计"])

    snap, message = snapshots.run_action(3, "生成快照", {"引擎编号": "DEDU-0003"})
    check(snap is not None, f"过期快照在原地重新生成：{message}")
    entry = snapshots.get_entry(3)
    check(entry["status"] == "生成中", "重新生成进入生成中")
    check(entry["历史批次统计"][0]["统计"] == old_stats, "上一版完成统计归档到历史批次统计")

    snap, message = snapshots.run_action(3, "确认完成", {})
    check(snap is not None, f"重新生成后确认完成：{message}")
    done = snapshots.get_entry(3)
    check(done["批次统计"]["采集行数"] == 150, "新统计来自引擎当前批次")
    check(done["新增行数"] == "130", "没有同批次基线时新增按去重后行数计算")


def test_concurrent_generation_and_confirm() -> None:
    engines, snapshots = fresh()
    errors: list[str] = []
    results: list[str] = []

    def generate() -> None:
        try:
            service = SnapshotService()
            snap, message = service.run_action(1, "生成快照", {"引擎编号": "DEDU-0003"})
            results.append("生成成功" if snap is not None else message)
        except Exception as error:  # pragma: no cover - 并发路径保护
            errors.append(str(error))

    threads = [threading.Thread(target=generate) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check(not errors, f"并发生成没有异常：{errors}")
    check(results.count("生成成功") == 1, f"只有一个并发生成进入生成中：{results}")
    check(snapshots.get_entry(1)["status"] == "生成中", "先进入的请求保留生成中")

    confirmed: list[str] = []

    def confirm() -> None:
        service = SnapshotService()
        snap, message = service.run_action(1, "确认完成", {})
        confirmed.append("已确认" if snap is not None and snap.get("status") == "已完成" else message)

    threads = [threading.Thread(target=confirm) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entry = snapshots.get_entry(1)
    check(entry["status"] == "已完成", "重复确认后状态仍是已完成")
    check(len(entry["状态历史"]) == 2, "状态历史只记录一次生成和一次确认")
    check(snapshots.get_entry(1)["确认令牌"].count("@") == 1, "只保留一个完成版本令牌")
    check(engines.get_entry(3)["已完成快照数"] == 2, "去重计数按完成版本回写")


def test_expire_confirm_race_keeps_first() -> None:
    engines, snapshots = fresh()
    snapshots.run_action(1, "生成快照", {"引擎编号": "DEDU-0003"})
    outcomes: list[str] = []
    lock = threading.Lock()

    def act(action: str) -> None:
        service = SnapshotService()
        snap, _ = service.run_action(1, action, {})
        with lock:
            outcomes.append(f"{action}:{'成功' if snap is not None else '失败'}")

    threads = [threading.Thread(target=act, args=(action,)) for action in ("确认完成", "标记过期")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entry = snapshots.get_entry(1)
    history = [(item["from"], item["to"]) for item in entry["状态历史"]]
    check(history[0] == ("草稿", "生成中"), f"状态历史从生成开始：{history}")
    first_target = history[1][1]
    check(first_target in ("已完成", "已过期"), f"先完成的请求决定终态：{outcomes}")
    if first_target == "已完成":
        check(entry["批次统计"]["采集行数"] == 150, "确认先完成时完整统计保留")
        check(history[-1] == ("已完成", "已过期"), "后到的过期只改状态，保留完整统计")
        check(entry["采集行数"] == "150", "完成统计没有被过期清空")
    else:
        check(entry["批次统计"] is None, "过期先完成时不会写入半截统计")
        check(history == [("草稿", "生成中"), ("生成中", "已过期")], f"后到的确认不推进状态：{history}")
        # 已过期允许原地重新生成，重新生成后统计来自同一批次。
        snapshots.run_action(1, "生成快照", {"引擎编号": "DEDU-0003"})
        snap, message = snapshots.run_action(1, "确认完成", {})
        check(snap is not None, f"过期后可重新生成并确认：{message}")
        check(snapshots.get_entry(1)["批次统计"]["采集行数"] == 150, "重新生成后统计来自当前批次")


def test_reenabled_engine_opens_new_window() -> None:
    """停用 → 重新评估 → 启用后，只有停用窗口内的晚到请求被拦，新请求可正常生成。"""
    engines, snapshots = fresh()
    engines.run_action(3, "停用引擎")
    disabled_at = engines.get_entry(3)["停用时间"]
    snap, message = snapshots.run_action(2, "生成快照", {"引擎编号": "DEDU-0003", "请求时间": disabled_at})
    assert snap is None, message
    assert snapshots.get_entry(2)["status"] == "草稿"
    assert snapshots.get_entry(2)["受影响字段组"] == ["url", "正文"]

    engines.run_action(3, "提交引擎评估")
    engines.run_action(3, "启用引擎")
    enabled_at = engines.get_entry(3)["启用时间"]
    assert enabled_at > disabled_at

    # 停用窗口内的请求在重新启用后仍然是晚到的，不会被新批次接收。
    snap, message = snapshots.run_action(2, "生成快照", {"引擎编号": "DEDU-0003", "请求时间": disabled_at})
    assert snap is None, message
    # 启用之后发出的请求正常进入生成中。
    snap, message = snapshots.run_action(2, "生成快照", {"引擎编号": "DEDU-0003"})
    assert snap is not None, message
    assert snapshots.get_entry(2)["status"] == "生成中"


def test_increment_uses_same_batch() -> None:
    engines, snapshots = fresh()
    snapshots.run_action(1, "生成快照", {"引擎编号": "DEDU-0003"})
    snap, message = snapshots.run_action(1, "确认完成", {})
    check(snap is not None, f"首个快照完成：{message}")
    first = snapshots.get_entry(1)
    engine = engines.get_entry(3)
    check(first["批次统计"]["批次"] == "DEDU-0003", "统计记录批次编号")
    check(first["去重后行数"] == "130", "去重后行数取引擎批次")
    check(first["新增行数"] == "130", "首份快照新增等于去重后行数")
    check(engine["最近完成批次统计"]["去重后行数"] == 130, "去重计数回写与快照一致")

    # 引擎进入下一个批次后，同一任务、同一引擎再次生成，增量基于同批次基线。
    engine["批次统计"] = {"采集行数": 180, "去重后行数": 150, "变更行数": 6}
    snapshots.create_entry({"快照编号": "SNAP-0100", "关联任务": "TASK-001", "快照时间": "2026-09-10"})
    snap, message = snapshots.run_action(4, "生成快照", {"引擎编号": "DEDU-0003"})
    check(snap is not None, f"第二份快照进入生成中：{message}")
    snap, message = snapshots.run_action(4, "确认完成", {})
    check(snap is not None, f"第二份快照完成：{message}")
    second = snapshots.get_entry(4)
    check(second["基线快照"] == "SNAP-0001", "增量基线指向同批次上一份完成快照")
    check(second["采集行数"] == "180" and second["去重后行数"] == "150", "统计来自当前批次")
    check(second["新增行数"] == "20", "增量 = 当前批次去重后行数 - 基线去重后行数")
    check(second["变更行数"] == "6", "变更行数来自同一批次")
    engine = engines.get_entry(3)
    check(engine["最近完成批次统计"]["采集行数"] == 180, "计数回写跟随最新完成版本")
    check(engine["最近完成批次统计"]["去重后行数"] == 150, "计数回写去重后行数与快照一致")

    # 请求伪造统计会被拒绝，不会覆盖批次数字。
    snap, message = snapshots.run_action(
        1,
        "标记过期",
        {},
    )
    check(snap is not None, "已完成快照标记过期")
    snap, message = snapshots.run_action(1, "生成快照", {"引擎编号": "DEDU-0003", "批次统计": {"采集行数": 999}})
    check(snap is None, f"伪造批次统计被拒绝：{message}")




if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
