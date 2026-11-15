import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/snapshot'
const columns = [
  "快照编号",
  "关联任务",
  "快照时间",
  "采集行数",
  "去重后行数",
  "新增行数",
  "变更行数",
  "快照文件路径"
]
// 和后端 ACTION_SOURCES 保持一致：入口只展示当前状态应有的动作，
// 避免点了不生效；后端仍然是最终裁决方。
const ACTION_SOURCES: Record<string, string[]> = {
  "生成快照": ["草稿", "已过期"],
  "确认完成": ["生成中"],
  "标记过期": ["生成中", "已完成"],
  "重新生成快照": ["已完成", "已过期"]
}
// 列表行上的紧凑入口：终态的「重新生成快照」只在详情面板出现，
// 避免和「生成快照」并排造成结果错位。
const ROW_ACTIONS: Record<string, string[]> = {
  "生成快照": ["草稿", "已过期"],
  "确认完成": ["生成中"],
  "标记过期": ["生成中", "已完成"]
}

type Row = Record<string, string | number | null>
type EngineOption = {
  "引擎编号": string
  "去重字段组": string
  "哈希算法": string
  "版本": string
  "批次统计": Record<string, number> | null
}
type Notice = { kind: 'ok' | 'error'; text: string }

function visibleActions(status: string, source: Record<string, string[]> = ACTION_SOURCES): string[] {
  return Object.entries(source)
    .filter(([, sources]) => sources.includes(status))
    .map(([action]) => action)
}

export default function SnapshotView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [engines, setEngines] = useState<EngineOption[]>([])
  const [engineCode, setEngineCode] = useState('')
  const [detail, setDetail] = useState<Row | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, engineResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/engines`),
      ])
      if (!listResponse.ok) {
        throw new Error('数据快照列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (engineResponse.ok) {
        const enginePayload = await engineResponse.json()
        const items: EngineOption[] = enginePayload.items ?? []
        setEngines(items)
        setEngineCode((prev) => (items.some((item) => item["引擎编号"] === prev) ? prev : items[0]?.["引擎编号"] ?? ''))
      }
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '采集快照列表读取失败' })
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openDetail(row: Row) {
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? '快照详情读取失败')
      }
      setDetail(payload)
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '快照详情读取失败' })
    }
  }

  async function runAction(action: string, row: Row) {
    setNotice(null)
    const values: Record<string, unknown> = {
      action,
      // 请求时间用于后端比较引擎停用时间：晚到请求会被留在草稿。
      "请求时间": new Date().toISOString(),
    }
    if (action === "生成快照" || action === "重新生成快照") {
      if (!engineCode) {
        setNotice({ kind: 'error', text: '当前没有已启用的去重引擎，无法生成快照' })
        return
      }
      values["引擎编号"] = engineCode
    }
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload?.detail ?? '采集快照动作未生效，请稍后重试')
      }
      setNotice({ kind: payload.ok ? 'ok' : 'error', text: payload.message ?? '动作已提交' })
      // 重新生成会创建新记录，详情面板不能拿它冒充原快照，关闭后让列表承接结果。
      if (action === "重新生成快照") {
        setDetail(null)
      } else if (detail && String(detail.id) === String(row.id)) {
        setDetail(payload.entry ?? null)
      }
      await reload()
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '采集快照操作失败' })
      await reload()
    }
  }

  const stats = useMemo(() => {
    const completed = rows.filter((row) => row.status === "已完成" || row.status === "已过期")
    const collected = completed.reduce((sum, row) => sum + Number(row["采集行数"] ?? 0), 0)
    return [
      { label: "数据快照总数", value: total },
      { label: "已完成快照", value: rows.filter((row) => row.status === "已完成").length },
      { label: "本月采集量", value: collected },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="snapshot">
      <header className="page-head">
        <div>
          <h2>采集快照管理</h2>
          <p className="page-desc">快照按草稿 → 生成中 → 已完成 → 已过期的方向单向推进；生成前必须先确定当前去重批次。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNotice({ kind: 'error', text: '数据快照登记入口尚未接入审批流' })}>
            登记数据快照
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集快照清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>生成使用的去重批次</span>
          <select value={engineCode} onChange={(event) => setEngineCode(event.target.value)}>
            {engines.length === 0 && <option value="">暂无已启用引擎</option>}
            {engines.map((engine) => (
              <option key={engine["引擎编号"]} value={engine["引擎编号"]}>
                {engine["引擎编号"]}（{engine["去重字段组"]}）
              </option>
            ))}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const status = String(row.status ?? '')
            const actions = visibleActions(status, ROW_ACTIONS)
            return (
              <tr key={String(row.id)}>
                <td>
                  <button className="link" type="button" onClick={() => void openDetail(row)}>
                    {row["快照编号"] ?? row.id}
                  </button>
                </td>
                {columns.slice(1).map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  {status}
                  {row.affected ? <span className="tag warn">受引擎停用影响</span> : null}
                </td>
                <td className="row-actions">
                  {actions.length === 0 && <span className="muted">当前状态无可用动作</span>}
                  {actions.map((action) => (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={(action === "生成快照" || action === "重新生成快照") && !engineCode}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无采集快照数据，可先登记数据快照</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条采集快照记录</span>
        {notice && <span className={notice.kind === 'error' ? 'error-text' : 'ok-text'}>{notice.text}</span>}
      </footer>

      {detail && (
        <aside className="detail-panel" data-testid="snapshot-detail">
          <header className="detail-head">
            <h3>{detail["快照编号"]} 详情</h3>
            <div className="row-actions">
              {visibleActions(String(detail.status ?? ''))
                .filter((action) => action === "重新生成快照")
                .map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    disabled={!engineCode}
                    onClick={() => void runAction(action, detail)}
                  >
                    {action}
                  </button>
                ))}
              <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
            </div>
          </header>
          <dl className="detail-grid">
            <dt>状态</dt><dd>{String(detail.status ?? '—')}</dd>
            <dt>关联任务</dt><dd>{String(detail["关联任务"] ?? '—')}</dd>
            <dt>当前去重批次</dt><dd>{String(detail["关联引擎"] ?? '未确定')}</dd>
            <dt>引擎版本</dt><dd>{String(detail["引擎版本"] ?? '—')}</dd>
            <dt>去重字段组</dt><dd>{String(detail["去重字段组"] ?? '—')}</dd>
            <dt>批次统计</dt>
            <dd>
              {detail["批次统计"]
                ? Object.entries(detail["批次统计"] as unknown as Record<string, unknown>)
                    .filter(([key]) => key !== '字段组')
                    .map(([key, value]) => `${key} ${Array.isArray(value) ? value.join('、') : String(value)}`)
                    .join('；')
                : '未写入（未完成确认）'}
            </dd>
            <dt>受影响字段组</dt>
            <dd>{Array.isArray(detail["受影响字段组"]) && detail["受影响字段组"].length > 0 ? (detail["受影响字段组"] as unknown as string[]).join('、') : '—'}</dd>
            <dt>影响说明</dt><dd>{String(detail["影响说明"] || '—')}</dd>
            <dt>历史批次</dt>
            <dd>
              {Array.isArray(detail["历史批次统计"]) && detail["历史批次统计"].length > 0
                ? `${detail["历史批次统计"].length} 个已归档完成版本，统计只读保留`
                : '—'}
            </dd>
            <dt>状态历史</dt>
            <dd>
              {Array.isArray(detail["状态历史"]) && detail["状态历史"].length > 0
                ? (detail["状态历史"] as unknown as Record<string, string>[]).map((item) => `${item.from} → ${item.to}（${item.action}）`).join('；')
                : '—'}
            </dd>
          </dl>
        </aside>
      )}
    </section>
  )
}
