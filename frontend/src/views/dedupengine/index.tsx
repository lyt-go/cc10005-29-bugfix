import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dedupengine'
const columns = [
  "引擎编号",
  "去重字段组",
  "哈希算法",
  "布隆过滤器大小",
  "误判容忍率",
  "滑动窗口天",
  "跨批次去重",
  "相似度阈值"
]
const statuses = [
  "草稿",
  "引擎评估中",
  "已启用",
  "已停用"
]

// 和后端 ACTION_SOURCES 一致：已停用是终态，不再展示任何动作。
// 重复动作由后端幂等返回说明，前端按当前状态收敛入口。
const ACTION_SOURCES: Record<string, string[]> = {
  "提交引擎评估": ["草稿", "引擎评估中", "已停用"],
  "启用引擎": ["引擎评估中", "已启用"],
  "停用引擎": ["引擎评估中", "已启用", "已停用"]
}

type Row = Record<string, string | number | null>
type Notice = { kind: 'ok' | 'error'; text: string }

function visibleActions(status: string): string[] {
  return Object.entries(ACTION_SOURCES)
    .filter(([, sources]) => sources.includes(status))
    .map(([action]) => action)
}

export default function DedupengineView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [detail, setDetail] = useState<Row | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('去重引擎列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '采集去重列表读取失败' })
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
        throw new Error(payload?.detail ?? '去重引擎详情读取失败')
      }
      setDetail(payload)
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '去重引擎详情读取失败' })
    }
  }

  async function runAction(action: string, row: Row) {
    setNotice(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload?.detail ?? '采集去重动作未生效，请稍后重试')
      }
      setNotice({ kind: payload.ok ? 'ok' : 'error', text: payload.message ?? '动作已提交' })
      if (detail && String(detail.id) === String(row.id)) {
        setDetail(payload.entry ?? null)
      }
      await reload()
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '采集去重操作失败' })
      await reload()
    }
  }

  const stats = useMemo(
    () => [
      { label: "去重引擎总数", value: total },
      { label: "已启用引擎", value: rows.filter((row) => row.status === "已启用").length },
      { label: "评估中引擎", value: rows.filter((row) => row.status === "引擎评估中").length },
    ],
    [rows, total],
  )

  return (
    <section className="page" data-module="dedupengine">
      <header className="page-head">
        <div>
          <h2>采集去重管理</h2>
          <p className="page-desc">管理采集数据的去重引擎配置；引擎按草稿 → 引擎评估中 → 已启用 → 已停用推进，停用后需重新提交评估才能启用。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNotice({ kind: 'error', text: '去重引擎登记入口尚未接入审批流' })}>
            登记去重引擎
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集去重清单
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
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
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
            const actions = visibleActions(status)
            return (
              <tr key={String(row.id)}>
                <td>
                  <button className="link" type="button" onClick={() => void openDetail(row)}>
                    {row["引擎编号"] ?? row.id}
                  </button>
                </td>
                {columns.slice(1).map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  {status}
                  {row.abnormal ? <span className="tag warn">需关注</span> : null}
                </td>
                <td className="row-actions">
                  {actions.length === 0 && <span className="muted">当前状态无可用动作</span>}
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无采集去重数据，可先登记去重引擎</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条采集去重记录</span>
        {notice && <span className={notice.kind === 'error' ? 'error-text' : 'ok-text'}>{notice.text}</span>}
      </footer>

      {detail && (
        <aside className="detail-panel" data-testid="dedup-detail">
          <header className="detail-head">
            <h3>{detail["引擎编号"]} 详情</h3>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            <dt>状态</dt><dd>{String(detail.status ?? '—')}</dd>
            <dt>去重字段组</dt><dd>{String(detail["去重字段组"] ?? '—')}</dd>
            <dt>引擎版本</dt><dd>{String(detail["版本"] ?? '—')}</dd>
            <dt>哈希算法</dt><dd>{String(detail["哈希算法"] ?? '—')}</dd>
            <dt>当前批次</dt><dd>{String(detail["批次编号"] ?? '—')}</dd>
            <dt>批次统计</dt>
            <dd>
              {detail["批次统计"]
                ? Object.entries(detail["批次统计"] as unknown as Record<string, unknown>)
                    .map(([key, value]) => `${key} ${String(value)}`)
                    .join('；')
                : '尚无进行中的去重批次'}
            </dd>
            <dt>停用时间</dt><dd>{String(detail["停用时间"] ?? '—')}</dd>
            <dt>影响范围</dt><dd>{String(detail["影响范围"] || '—')}</dd>
            <dt>受影响快照</dt>
            <dd>
              {Array.isArray(detail["受影响快照"]) && detail["受影响快照"].length > 0
                ? (detail["受影响快照"] as unknown as string[]).join('、')
                : '—'}
            </dd>
            <dt>已完成快照数</dt><dd>{String(detail["已完成快照数"] ?? '0')}</dd>
            <dt>最近完成快照</dt><dd>{String(detail["最近完成快照"] ?? '—')}</dd>
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
